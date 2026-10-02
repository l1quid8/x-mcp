#!/bin/sh
set -eu
mkdir -p "$HOME"
Xvfb :99 -screen 0 1280x800x24 -nolisten tcp &
while [ ! -e /tmp/.X11-unix/X99 ]; do sleep 0.1; done
x11vnc -display :99 -forever -shared -localhost -nopw -rfbport 5900 -quiet &
websockify --web /usr/share/novnc 0.0.0.0:6080 127.0.0.1:5900 &
exec uvicorn worker:app --host 0.0.0.0 --port 8785 --no-access-log
