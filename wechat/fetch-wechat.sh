#!/bin/sh
# Download the official Linux WeChat package (8 parallel range requests) into the build context.
set -eu
cd "$(dirname "$0")"
URL=https://dldir1v6.qq.com/weixin/Universal/Linux/WeChatLinux_x86_64.deb
SIZE=$(curl -sIL "$URL" | awk 'tolower($1)=="content-length:"{v=$2} END{print v+0}')
[ "$SIZE" -gt 0 ]
N=8; PART=$(( (SIZE + N - 1) / N )); rm -rf .parts; mkdir .parts
i=0; while [ $i -lt $N ]; do
  s=$((i*PART)); e=$((s+PART-1)); [ $e -ge $SIZE ] && e=$((SIZE-1))
  curl -sfL --retry 3 -r $s-$e -o .parts/$i "$URL" &
  i=$((i+1))
done
wait
i=0; : > WeChatLinux_x86_64.deb.tmp
while [ $i -lt $N ]; do cat .parts/$i >> WeChatLinux_x86_64.deb.tmp; i=$((i+1)); done
[ "$(stat -c %s WeChatLinux_x86_64.deb.tmp)" -eq "$SIZE" ]
dpkg-deb --info WeChatLinux_x86_64.deb.tmp >/dev/null 2>&1 || { echo "not a valid .deb" >&2; exit 1; }
mv WeChatLinux_x86_64.deb.tmp WeChatLinux_x86_64.deb; rm -rf .parts
echo "ok $SIZE bytes"
