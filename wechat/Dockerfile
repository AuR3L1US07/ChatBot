# Official Tencent WeChat for Linux running on a virtual screen (Xvfb).
# The screen is viewable through noVNC (port 6080, never published to the host);
# the admin proxy serves it at https://<server>:8443/wxscreen/ to logged-in admins.
#
# Run fetch-wechat.sh first: it downloads the official package into this directory.
FROM debian:bookworm-slim

ENV DEBIAN_FRONTEND=noninteractive \
    LANG=zh_CN.UTF-8 LC_ALL=zh_CN.UTF-8 TZ=Asia/Shanghai \
    QT_QPA_PLATFORM=xcb XDG_SESSION_TYPE=x11 \
    QT_ACCESSIBILITY=1 QT_LINUX_ACCESSIBILITY_ALWAYS_ON=1 GTK_MODULES=gail:atk-bridge

# Optional Debian mirror, e.g. --build-arg APT_MIRROR=https://mirrors.aliyun.com
ARG APT_MIRROR=http://deb.debian.org
RUN sed -i "s|http://deb.debian.org|${APT_MIRROR}|g" /etc/apt/sources.list.d/debian.sources \
 && apt-get update \
 && apt-get install -y --no-install-recommends \
      xvfb x11vnc novnc websockify openbox xdotool xclip dbus-x11 procps \
      fonts-wqy-microhei locales ca-certificates curl \
 && sed -i 's/^# *zh_CN.UTF-8/zh_CN.UTF-8/' /etc/locale.gen && locale-gen \
 && rm -rf /var/lib/apt/lists/*

# Official package from linux.weixin.qq.com (Tencent CDN), fetched by fetch-wechat.sh.
COPY WeChatLinux_x86_64.deb /tmp/wechat.deb
# Runtime libraries the WeChat binary links against but its package does not declare.
RUN apt-get update \
 && apt-get install -y --no-install-recommends /tmp/wechat.deb \
      libatomic1 libasound2 libatk1.0-0 libatk-bridge2.0-0 libatspi2.0-0 \
      libegl1 libgl1 libgbm1 libjack-jackd2-0 libnspr4 libnss3 libpulse0 \
      libxcomposite1 libxkbcommon-x11-0 libxcb-keysyms1 libxcb-icccm4 libxcb-image0 \
      libxcb-render-util0 libxcb-randr0 libxcb-xinerama0 libxcb-cursor0 libxcb-shape0 \
      libwayland-egl1 libwayland-client0 libwayland-cursor0 \
 && install -d -m 1777 /tmp/.X11-unix \
 && rm -rf /tmp/wechat.deb /var/lib/apt/lists/*

# Tools for reading chats without the database key: D-Bus (notifications),
# AT-SPI (accessibility tree) and Tesseract OCR (screenshots), plus gdb/sqlcipher.
RUN apt-get update \
 && apt-get install -y --no-install-recommends sqlcipher gdb \
      python3-dbus python3-gi python3-pil python3-numpy python3-cryptography python3-zstandard \
      tesseract-ocr tesseract-ocr-chi-sim \
      at-spi2-core python3-pyatspi gir1.2-atspi-2.0 x11-apps \
 && rm -rf /var/lib/apt/lists/*

RUN useradd -m -u 1000 wx
COPY --chmod=755 entrypoint.sh /entrypoint.sh
USER wx
WORKDIR /home/wx
ENTRYPOINT ["/entrypoint.sh"]
