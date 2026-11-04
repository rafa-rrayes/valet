#!/bin/sh
# The control plane's container: a virtual display on $DISPLAY for headed Chrome, then the command (by default
# `valet-control serve`; the other subcommands, `valet-control set-password` and so on, work the same way). tini, PID 1,
# reaps what Chrome leaves behind.
set -e
if [ -n "$DISPLAY" ]; then
    n="${DISPLAY#:}"
    n="${n%%.*}"
    rm -f "/tmp/.X${n}-lock" "/tmp/.X11-unix/X${n}"      # left by a container that was stopped, then started again
    Xvfb "$DISPLAY" -screen 0 1280x900x24 -nolisten tcp >/tmp/xvfb.log 2>&1 &
    i=0
    while [ ! -S "/tmp/.X11-unix/X${n}" ]; do
        i=$((i + 1))
        if [ "$i" -gt 100 ]; then
            echo "Xvfb didn't start on $DISPLAY:" >&2
            cat /tmp/xvfb.log >&2
            exit 1
        fi
        sleep 0.1
    done
fi
exec "$@"
