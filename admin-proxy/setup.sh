#!/bin/sh
# One-time setup: self-signed HTTPS certificate for the server IP.
set -eu
cd "$(dirname "$0")"
IP="${1:?usage: setup.sh <server-ip>}"
mkdir -p certs
if [ ! -f certs/server.crt ]; then
  OPENSSL=openssl
  command -v openssl >/dev/null 2>&1 || OPENSSL="docker run --rm -v $PWD/certs:/certs -w / alpine/openssl"
  $OPENSSL req -x509 -newkey rsa:2048 -nodes -days 3650 \
    -keyout certs/server.key -out certs/server.crt \
    -subj "/CN=$IP" -addext "subjectAltName=IP:$IP" 2>/dev/null
  chmod 644 certs/server.crt; chmod 600 certs/server.key
fi
