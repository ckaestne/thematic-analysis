#!/usr/bin/env bash
# Build the ta-webview image locally, ship it to the remote host over SSH,
# sync the local ./data DBs, and (re)start the stack with docker compose.
set -euo pipefail

REMOTE="${REMOTE:-ckaestne@feature.isri.cmu.edu}"
REMOTE_DIR="${REMOTE_DIR:-ta-webview}"
IMAGE="ta-webview:latest"

cd "$(dirname "$0")"

# Reuse a single SSH connection for every ssh/scp/rsync below, so the
# password is only prompted for once.
SSH_CTL="$(mktemp -u "${TMPDIR:-/tmp}/ta-deploy-ssh.XXXXXX")"
SSH_OPTS=(-o "ControlMaster=auto" -o "ControlPath=$SSH_CTL" -o "ControlPersist=300")
cleanup() { ssh "${SSH_OPTS[@]}" -O exit "$REMOTE" 2>/dev/null || true; }
trap cleanup EXIT

echo ">> Opening SSH connection to $REMOTE (enter password once)"
ssh "${SSH_OPTS[@]}" "$REMOTE" "mkdir -p '$REMOTE_DIR/data'"

echo ">> Building $IMAGE locally"
docker compose build

echo ">> Shipping image to $REMOTE (docker save | ssh docker load)"
# Pick the fastest available compressor. pigz parallelizes gzip across all
# cores (big win on this ~15GB image); zstd is faster still but needs the
# same tool on the remote. Fall back to plain gzip.
if command -v pigz >/dev/null 2>&1; then
  COMPRESS=(pigz); DECOMPRESS='gunzip'
else
  COMPRESS=(gzip); DECOMPRESS='gunzip'
fi

# Put pv right after `docker save` so the bar tracks the real (uncompressed)
# image bytes against its known total — confirms data is actually flowing.
if command -v pv >/dev/null 2>&1; then
  size=$(docker image inspect "$IMAGE" --format '{{.Size}}' 2>/dev/null || echo 0)
  docker save "$IMAGE" \
    | pv -s "$size" -N image \
    | "${COMPRESS[@]}" \
    | ssh "${SSH_OPTS[@]}" "$REMOTE" "$DECOMPRESS | docker load"
else
  echo "   (install 'pv' for a progress bar)"
  docker save "$IMAGE" | "${COMPRESS[@]}" \
    | ssh "${SSH_OPTS[@]}" "$REMOTE" "$DECOMPRESS | docker load"
fi

echo ">> Copying docker-compose.yml"
scp "${SSH_OPTS[@]}" docker-compose.yml "$REMOTE:$REMOTE_DIR/docker-compose.yml"

echo ">> Syncing ./data/*.db to remote"
rsync -av --progress --delete-after \
  -e "ssh ${SSH_OPTS[*]}" \
  --include='*.db' --exclude='*' \
  ./data/ "$REMOTE:$REMOTE_DIR/data/"

echo ">> Restarting stack on remote"
ssh "${SSH_OPTS[@]}" "$REMOTE" "cd '$REMOTE_DIR' && docker compose up -d"

echo ">> Done. Served on $REMOTE port 8765."
