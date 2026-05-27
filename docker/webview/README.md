# ta-web in Docker (multi-DB)

Runs the `ta-web` UI over an external directory of SQLite databases. Each
`*.db` file in the mounted volume is served at `/<stem>/`.

## Usage

```bash
cd docker/webview
# drop your SQLite DB files into ./data/  (each `foo.db` will be served at /foo/)
docker compose up --build
```

Then open:

- <http://localhost:8765/> — index of available databases
- <http://localhost:8765/foo/> — UI for `data/foo.db`

## How it works

The container runs `ta-web-multi`, a tiny FastAPI dispatcher. On the first
request to `/<name>/...`, it spawns an unmodified `ta-web` child process
bound to a private localhost port for `data/<name>.db`, and reverse-proxies
the request to it. Subsequent requests reuse the same child. Children that
are idle for more than 60 minutes are terminated automatically (and
respawned on the next request).

## Configuration

The compose file mounts `./data` to `/data` in the container. Change the
host path to point at any directory of `.db` files.

To tune the idle timeout or bind address, edit the `ENTRYPOINT` in
`Dockerfile` (or override `command:` in `docker-compose.yml`):

```yaml
command:
  - ta-web-multi
  - --data-dir=/data
  - --host=0.0.0.0
  - --port=8765
  - --idle-timeout=3600
```
