"""Multi-database dispatcher for `ta-web`.

Serves `http://<host>:<port>/<name>/...` by spawning one `ta-web` child
process per `<name>.db` file in `--data-dir` on demand, and reverse-proxying
requests to it. Children that go idle for longer than `--idle-timeout`
seconds are terminated and respawned on the next request.

No backend changes: each child is an unmodified `ta-web` bound to
`127.0.0.1:<random_port>`.
"""

from __future__ import annotations

import argparse
import asyncio
import atexit
import os
import re
import socket
import subprocess
import sys
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path

import httpx
from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import HTMLResponse, RedirectResponse


_NAME_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.-]*$")
_HOP_BY_HOP = {
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
    "te", "trailers", "transfer-encoding", "upgrade", "content-length",
    "host",
}
_READY_TIMEOUT = 30.0
_IDLE_SWEEP_INTERVAL = 300.0


@dataclass
class Child:
    name: str
    db_path: Path
    port: int
    proc: subprocess.Popen
    last_used: float = field(default_factory=time.monotonic)


def _pick_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _safe_name(name: str) -> bool:
    return bool(_NAME_RE.match(name)) and ".." not in name


def create_app(data_dir: Path, idle_timeout: float = 604800.0) -> FastAPI:
    data_dir = data_dir.resolve()
    children: dict[str, Child] = {}
    lock = asyncio.Lock()
    client = httpx.AsyncClient(timeout=None)

    async def _wait_ready(port: int) -> bool:
        deadline = time.monotonic() + _READY_TIMEOUT
        url = f"http://127.0.0.1:{port}/api/status"
        while time.monotonic() < deadline:
            try:
                r = await client.get(url, timeout=2.0)
                if r.status_code < 500:
                    return True
            except httpx.HTTPError:
                pass
            await asyncio.sleep(0.2)
        return False

    def _spawn(name: str, db_path: Path) -> Child:
        port = _pick_free_port()
        cmd = [
            sys.executable, "-m", "thematic_analysis_inc.web",
            "--db", str(db_path),
            "--host", "127.0.0.1",
            "--port", str(port),
        ]
        env = os.environ.copy()
        proc = subprocess.Popen(cmd, env=env)
        return Child(name=name, db_path=db_path, port=port, proc=proc)

    def _terminate(c: Child) -> None:
        if c.proc.poll() is None:
            try:
                c.proc.terminate()
                try:
                    c.proc.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    c.proc.kill()
                    c.proc.wait(timeout=5)
            except Exception:
                pass

    async def _get_child(name: str) -> Child:
        if not _safe_name(name):
            raise HTTPException(status_code=400, detail="invalid db name")
        db_path = data_dir / f"{name}.db"
        if not db_path.is_file():
            raise HTTPException(status_code=404, detail=f"{name}.db not found")
        async with lock:
            c = children.get(name)
            if c is not None and c.proc.poll() is not None:
                # Crashed; respawn.
                children.pop(name, None)
                c = None
            if c is None:
                c = _spawn(name, db_path)
                children[name] = c
                ok = await _wait_ready(c.port)
                if not ok:
                    _terminate(c)
                    children.pop(name, None)
                    raise HTTPException(
                        status_code=502,
                        detail=f"child for {name} failed to start",
                    )
            c.last_used = time.monotonic()
            return c

    async def _idle_sweeper() -> None:
        while True:
            await asyncio.sleep(_IDLE_SWEEP_INTERVAL)
            now = time.monotonic()
            stale: list[Child] = []
            async with lock:
                for name, c in list(children.items()):
                    if now - c.last_used > idle_timeout:
                        stale.append(c)
                        children.pop(name, None)
            for c in stale:
                _terminate(c)

    @asynccontextmanager
    async def _lifespan(_app: FastAPI):
        sweeper = asyncio.create_task(_idle_sweeper())
        try:
            yield
        finally:
            sweeper.cancel()
            for c in list(children.values()):
                _terminate(c)
            children.clear()
            await client.aclose()

    app = FastAPI(lifespan=_lifespan)

    def _cleanup_on_exit() -> None:
        for c in list(children.values()):
            _terminate(c)
    atexit.register(_cleanup_on_exit)

    @app.get("/", response_class=HTMLResponse)
    async def _index() -> HTMLResponse:
        # Deliberately does not enumerate the available databases: the
        # dispatcher must not reveal which DBs exist. Access a known DB
        # directly at /<name>/.
        body = (
            "<h1>thematic-analysis web viewer</h1>"
            "<p>Open a database directly at <code>/&lt;name&gt;/</code>.</p>"
        )
        return HTMLResponse(
            f"<!doctype html><html><head><meta charset='utf-8'>"
            f"<title>ta-web</title></head><body>{body}</body></html>"
        )

    async def _proxy(child: Child, rest: str, request: Request) -> Response:
        url = f"http://127.0.0.1:{child.port}/{rest}"
        if request.url.query:
            url += "?" + request.url.query
        headers = {
            k: v for k, v in request.headers.items()
            if k.lower() not in _HOP_BY_HOP
        }
        body = await request.body()
        try:
            upstream = await client.request(
                request.method, url, headers=headers, content=body,
                follow_redirects=False,
            )
        except httpx.HTTPError as e:
            raise HTTPException(status_code=502, detail=f"upstream error: {e}")
        resp_headers = {
            k: v for k, v in upstream.headers.items()
            if k.lower() not in _HOP_BY_HOP
        }
        content = upstream.content
        ctype = upstream.headers.get("content-type", "")
        if ctype.startswith("text/html"):
            # Force the SPA's <base href> to the per-DB mount point so
            # relative URLs (assets, /api fetches, router basename) resolve
            # correctly on deep-linked routes like /foo/segments/123.
            new_base = f'<base href="/{child.name}/" />'.encode()
            content = re.sub(
                rb'<base\s+href="[^"]*"\s*/?>', new_base, content, count=1,
            )
        return Response(
            content=content,
            status_code=upstream.status_code,
            headers=resp_headers,
            media_type=upstream.headers.get("content-type"),
        )

    @app.api_route(
        "/{name}",
        methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "HEAD"],
    )
    async def _bare(name: str):
        # Redirect /foo to /foo/ so relative URLs (e.g. <base href="./">)
        # resolve correctly in the browser.
        if not _safe_name(name):
            raise HTTPException(status_code=400, detail="invalid db name")
        return RedirectResponse(url=f"./{name}/", status_code=307)

    @app.api_route(
        "/{name}/{rest:path}",
        methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "HEAD"],
    )
    async def _dispatch(name: str, rest: str, request: Request) -> Response:
        child = await _get_child(name)
        return await _proxy(child, rest, request)

    return app


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="ta-web-multi",
        description=(
            "Serve multiple thematic-analysis SQLite DBs from a directory, "
            "each at /<stem>/. Spawns one ta-web child per DB on demand."
        ),
    )
    parser.add_argument("--data-dir", default="/data",
                        help="Directory containing *.db files.")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--idle-timeout", type=float, default=604800.0,
                        help="Seconds of inactivity before a child is killed.")
    args = parser.parse_args(argv)

    data_dir = Path(args.data_dir).resolve()
    if not data_dir.is_dir():
        print(f"warning: {data_dir} is not a directory", flush=True)

    import uvicorn

    app = create_app(data_dir, idle_timeout=args.idle_timeout)
    print(
        f"ta-web-multi: serving {data_dir} at "
        f"http://{args.host}:{args.port}",
        flush=True,
    )
    uvicorn.run(app, host=args.host, port=args.port)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
