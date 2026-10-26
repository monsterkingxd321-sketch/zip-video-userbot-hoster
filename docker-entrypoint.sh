#!/bin/sh
set -eu

# Railway/Fly/Docker volumes are often mounted as root even when the image was
# built with a non-root user. Fix ownership before dropping privileges so the
# encrypted-session directory and temp workspace are writable.
if [ "$(id -u)" = "0" ]; then
    data_dir="${DATA_DIR:-/data}"
    mkdir -p "$data_dir/sessions" "$data_dir/tmp"
    chown -R appuser:appuser "$data_dir"
    exec gosu appuser "$@"
fi

exec "$@"
