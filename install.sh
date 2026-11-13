#!/bin/sh
# ChatBot one-command install. Run on a Linux server as root:
#   curl -fsSL <raw url of this file> | sh        (or: sh install.sh)
# Optional: sh install.sh <public-ip>
set -eu

REPO_URL="${REPO_URL:-https://github.com/AuR3L1US07/ChatBot.git}"   # set after publishing
IMAGE="${CHATBOT_IMAGE:-ghcr.io/aur3l1us07/chatbot:latest}"           # prebuilt image (GitHub Actions)
DIR="${CHATBOT_DIR:-/opt/chatbot}"

say() { printf '\033[1;32m==>\033[0m %s\n' "$*"; }
die() { printf '\033[1;31m错误:\033[0m %s\n' "$*" >&2; exit 1; }

[ "$(id -u)" -eq 0 ] || die "请用 root 运行（sudo sh install.sh）"
[ "$(uname -m)" = x86_64 ] || die "微信 Linux 版只支持 x86_64"

# 1. Docker
if ! command -v docker >/dev/null 2>&1; then
  say "安装 Docker…"
  curl -fsSL https://get.docker.com | sh || die "Docker 安装失败，请手动安装后重试"
  systemctl enable --now docker 2>/dev/null || true
fi
docker compose version >/dev/null 2>&1 || die "缺少 docker compose 插件"

# 2. Code (when piped from curl)
if [ ! -f wechat/docker-compose.yaml ]; then
  command -v git >/dev/null 2>&1 || { apt-get update -qq && apt-get install -y -qq git; } || die "请先安装 git"
  [ -d "$DIR/.git" ] && git -C "$DIR" pull -q || git clone -q --depth 1 "$REPO_URL" "$DIR"
  cd "$DIR"
fi

# 3. Public IP for the HTTPS certificate
IP="${1:-}"
[ -n "$IP" ] || IP=$(curl -fsS4 --max-time 5 https://api.ipify.org 2>/dev/null || true)
[ -n "$IP" ] || IP=$(hostname -I | awk '{print $1}')
say "服务器地址：$IP"

# 4. Image: pull the prebuilt one; build locally only if that fails
export CHATBOT_IMAGE="$IMAGE"
if docker pull "$IMAGE"; then
  say "已下载预构建镜像"
else
  say "没有预构建镜像，开始本地构建（约 5–10 分钟）"
  mem=$(awk '/MemTotal/{print int($2/1024)}' /proc/meminfo)
  if [ "$mem" -lt 1900 ] && ! swapon --show | grep -q .; then
    say "内存只有 ${mem}MB，临时加 2G swap 防止构建卡死"
    fallocate -l 2G /swapfile-chatbot && chmod 600 /swapfile-chatbot && mkswap -q /swapfile-chatbot && swapon /swapfile-chatbot
  fi
  [ -f wechat/WeChatLinux_x86_64.deb ] || sh wechat/fetch-wechat.sh
  export CHATBOT_IMAGE=wechat-linux:local
  docker compose -f wechat/docker-compose.yaml build
fi

# 5. Start
docker compose -f wechat/docker-compose.yaml up -d --no-build
sh admin-proxy/setup.sh "$IP"
docker compose -f admin-proxy/docker-compose.yaml up -d

say "等待启动…"
i=0; code=""
while [ $i -lt 30 ] && [ -z "$code" ]; do
  code=$(docker exec wechat_linux cat /home/wx/.wxbot/setup-code.txt 2>/dev/null || true); i=$((i+1)); sleep 2
done

cat <<MSG

  安装完成！
  1. 浏览器打开  https://$IP:8443   （提示“不安全”时点 高级 → 继续访问）
  2. 初始化码：  ${code:-用 docker logs wechat_linux 查看}
  3. 设置后台密码 → 微信扫码登录 → 选 AI 平台填 Key → 设人设
  记得在云服务器安全组里放行 TCP 8443。

MSG
