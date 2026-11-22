#!/usr/bin/env bash
# Starts the same container two ways and times both:
#
#   usual way  download the whole image, unpack it, then start the container
#   lazy way   mount the image (only its index is downloaded up front), start the container,
#              and let the file system download each file the first time it's opened
#
# The images are python:3.12-slim and python:3.11-slim, exported from docker. Each container
# runs Python with its own PID, mount, network, UTS and IPC namespaces, without root.
# Everything runs on localhost first, then over simulated links: 1 Gbit/s with 2 ms latency,
# and 100 Mbit/s with 10 ms latency.
#
# Needs docker (only to export the two images, once), FUSE 3, unprivileged user namespaces
# and the packages in requirements.txt. Run it from the repo root: ./demo.sh
set -euo pipefail
cd "$(dirname "$0")"

PY=${PY:-venv/bin/python}
PORT=${PORT:-8765}
SERVER=http://127.0.0.1:$PORT
WORK=demo-work
APP='import json, sqlite3, ssl, http.client, platform; print("  hello from python", platform.python_version())'
FAST_LINK=(--bandwidth 1000 --latency 2)
SLOW_LINK=(--bandwidth 100 --latency 10)

# A database and blob store of its own, so the demo never touches your data.
export IMAGESTORE_DATA_DIR=$WORK/data

run_container() {  # run_container <root folder>
    unshare --user --map-root-user --mount --pid --net --uts --ipc --fork \
        chroot "$1" /usr/local/bin/python3 -c "$APP"
}

now() { date +%s.%N; }
since() { awk -v start="$1" -v end="$(now)" 'BEGIN { printf "%.2f", end - start }'; }

usual_way() {  # usual_way <image> [link options]
    rm -rf "$WORK/rootfs"
    local start
    start=$(now)
    $PY -m imagestore --server "$SERVER" pull "$1" "$WORK/rootfs" "${@:2}" | sed 's/^/  /'
    run_container "$WORK/rootfs"
    TOOK=$(since "$start")
    echo "  ready in $TOOK s"
}

lazy_way() {  # lazy_way <image> <cache folder> [link options]
    mkdir -p "$WORK/mnt"
    local start
    start=$(now)
    $PY -m imagestore --server "$SERVER" mount "$1" "$WORK/mnt" --cache "$2" "${@:3}" > "$WORK/mount.log" 2>&1 &
    MOUNT_PID=$!
    until mountpoint -q "$WORK/mnt"; do
        kill -0 "$MOUNT_PID" 2> /dev/null || { cat "$WORK/mount.log"; exit 1; }
        sleep 0.01
    done
    run_container "$WORK/mnt"
    TOOK=$(since "$start")
    fusermount3 -u "$WORK/mnt"
    wait "$MOUNT_PID"
    echo "  ready in $TOOK s, $(tail -n 1 "$WORK/mount.log")"
}

export_image() {  # export_image <docker image> <tar>
    [ -f "$2" ] && return
    echo "exporting $1 with docker (only the first time)"
    local container
    container=$(docker create "$1")
    docker export "$container" -o "$2"
    docker rm "$container" > /dev/null
}

cleanup() {
    if mountpoint -q "$WORK/mnt" 2> /dev/null; then fusermount3 -u "$WORK/mnt"; fi
    if [ -n "${SERVER_PID:-}" ]; then kill "$SERVER_PID" 2> /dev/null || true; fi
}
trap cleanup EXIT

show_upload='import json, sys
data = json.load(sys.stdin)
image, stats = data["image"], data["stats"]
print("  %s:%s  %s files, %.1f MB, of which %.1f MB was new to the store" % (
    image["name"], image["tag"], format(stats["files"], ","), stats["total_bytes"] / 1e6,
    stats["new_bytes_stored"] / 1e6))'

mkdir -p "$WORK"
export_image python:3.12-slim "$WORK/python-3.12-slim.tar"
export_image python:3.11-slim "$WORK/python-3.11-slim.tar"
rm -rf "$WORK/data" "$WORK"/cache-*
mkdir -p "$WORK/data"
$PY manage.py migrate -v 0
$PY manage.py runserver "127.0.0.1:$PORT" --noreload > "$WORK/server.log" 2>&1 &
SERVER_PID=$!
until curl -s -o /dev/null "$SERVER/api/stats/"; do sleep 0.1; done

echo "== Uploading both images to the registry (each file is stored once, by its checksum)"
for tag in 3.12-slim 3.11-slim; do
    curl -s -F name=python -F tag="$tag" -F file=@"$WORK/python-$tag.tar" "$SERVER/api/images/" | $PY -c "$show_upload"
done

echo
echo "== python:3.12-slim on localhost"
echo "usual way: pull the whole image, unpack it, start the container"
usual_way python:3.12-slim
LOCAL_USUAL=$TOOK
echo "usual way, second start (the image is already unpacked)"
start=$(now)
run_container "$WORK/rootfs"
LOCAL_USUAL_AGAIN=$(since "$start")
echo "  ready in $LOCAL_USUAL_AGAIN s"
echo "lazy way: mount the image, start the container, fetch files as they're opened"
lazy_way python:3.12-slim "$WORK/cache-local"
LOCAL_LAZY=$TOOK
echo "lazy way, second start (the files it needs are in the local cache)"
lazy_way python:3.12-slim "$WORK/cache-local"
LOCAL_LAZY_AGAIN=$TOOK

echo
echo "== python:3.12-slim over a simulated 1 Gbit/s link with 2 ms latency"
echo "usual way"
usual_way python:3.12-slim "${FAST_LINK[@]}"
FAST_USUAL=$TOOK
echo "lazy way"
lazy_way python:3.12-slim "$WORK/cache-fast" "${FAST_LINK[@]}"
FAST_LAZY=$TOOK

echo
echo "== python:3.12-slim over a simulated 100 Mbit/s link with 10 ms latency"
echo "usual way"
usual_way python:3.12-slim "${SLOW_LINK[@]}"
SLOW_USUAL=$TOOK
echo "lazy way"
lazy_way python:3.12-slim "$WORK/cache-slow" "${SLOW_LINK[@]}"
SLOW_LAZY=$TOOK
echo "lazy way, python:3.11-slim next on the same machine (files it shares with 3.12 are cached)"
lazy_way python:3.11-slim "$WORK/cache-slow" "${SLOW_LINK[@]}"
SLOW_NEXT=$TOOK

echo
echo "== Seconds until the container printed its greeting"
printf "  %-40s %10s %10s\n" "" "usual way" "lazy way"
printf "  %-40s %10s %10s\n" "localhost, first start" "$LOCAL_USUAL" "$LOCAL_LAZY"
printf "  %-40s %10s %10s\n" "localhost, second start" "$LOCAL_USUAL_AGAIN" "$LOCAL_LAZY_AGAIN"
printf "  %-40s %10s %10s\n" "1 Gbit/s + 2 ms, first start" "$FAST_USUAL" "$FAST_LAZY"
printf "  %-40s %10s %10s\n" "100 Mbit/s + 10 ms, first start" "$SLOW_USUAL" "$SLOW_LAZY"
printf "  %-40s %10s %10s\n" "100 Mbit/s + 10 ms, then python:3.11" "-" "$SLOW_NEXT"
