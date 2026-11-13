#!/bin/sh
# Virtual screen -> window manager -> VNC (localhost only) -> noVNC websocket on :6080 -> WeChat.
set -eu
export DISPLAY=:1
rm -f /tmp/.X1-lock
Xvfb :1 -screen 0 1280x800x24 -nolisten tcp >/dev/null 2>&1 &
sleep 1
openbox >/dev/null 2>&1 &
x11vnc -display :1 -localhost -forever -shared -rfbport 5900 -nopw -quiet >/dev/null 2>&1 &
websockify --web /usr/share/novnc 0.0.0.0:6080 127.0.0.1:5900 >/dev/null 2>&1 &
eval "$(dbus-launch --sh-syntax)"
python3 /opt/wxagent/wxagent.py &

# The launcher starts and supervises WeChat (and may read its database keys,
# which Ubuntu only allows for a parent process). Keep this shell as PID 1.
python3 /opt/wxagent/wxlauncher.py &
wait
