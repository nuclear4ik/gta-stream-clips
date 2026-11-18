#!/bin/sh
set -eu
umask 077
mkdir -p -- "$STREAMCUT_DATA"
chmod 700 -- "$STREAMCUT_DATA"
exec python /app/control-room/studio.py --data "$STREAMCUT_DATA" --port 8765 "$@"
