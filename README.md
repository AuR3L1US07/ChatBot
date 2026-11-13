# ChatBot · 小克微信机器人

把一个微信小号变成 AI 聊天机器人：私聊自动回复，群里被 @ 或聊到相关话题时插话，会看图、偷表情包、学群聊黑话。自带网页后台，所有设置都在网页上点。

- 跑的是**官方 Linux 版微信**（运行在虚拟屏幕里），通过无障碍接口读消息、模拟键盘发消息。不用 hook，也不用破解协议。
- 支持任何 OpenAI 兼容接口，后台内置了预设：硅基流动、DeepSeek、阿里百炼、Kimi、智谱、火山方舟、OpenRouter、OpenAI、Anthropic、Ollama，也可以自定义。
- 网页后台功能：
  - 仪表盘
  - 扫码登录微信（远程桌面）
  - 每个对话单独设置
  - 表情包管理
  - 人设（名字、开发者、性格、说话方式）
  - AI 接口
  - 后台密码

## 需要什么

- 一台 x86_64 Linux 服务器，1 核 2G 起步（机器人本身占约 700MB 内存）
- 云服务器安全组放行 **TCP 8443**
- 一个微信小号（建议别用主号）
- 一个大模型 API Key

## 安装：一条命令

用 root 登录服务器，执行：

```bash
curl -fsSL https://raw.githubusercontent.com/AuR3L1US07/ChatBot/main/install.sh | sh
```

脚本会自动完成这些事：

1. 没有 Docker 就自动安装
2. 下载代码到 `/opt/chatbot`
3. 自动识别服务器公网 IP
4. 下载预构建镜像，里面已经装好微信 Linux 版、虚拟屏幕和远程桌面。如果拉取失败，就改成本地构建；内存不足时会自动加 swap
5. 生成 HTTPS 证书，启动服务
6. 最后打印后台地址和初始化码

装完按提示操作：

1. 打开 `https://<IP>:8443`
2. 输入初始化码
3. 设置后台密码
4. 微信扫码登录
5. 选择 AI 平台并填入 Key
6. 设置人设（名字要和小号的微信昵称一致）

「机器人设置」里默认开着「仅预览不发送」，观察一阵确认没问题再关掉。

想重新查看初始化码，执行 `docker logs wechat_linux`。

更新到新版本：重新运行同一条命令即可，数据不会丢。

## 目录

```
wechat/          微信 + 机器人容器
  hot/           机器人代码（挂载进容器，改完不用重建镜像）
    wxbot.py     回复逻辑
    wxmemes.py   表情包收集和挑选
    wxfriends.py 好友申请
    stickers/    内置表情（[表情: 名称]）
  wxagent.py     网页后台的服务端
  admin.html     网页后台
  wxlauncher.py  启动并守护微信
admin-proxy/     Caddy HTTPS 反向代理（8443）
```

数据都保存在 Docker 卷 `wechat_home` 的 `~/.wxbot/` 下，包括：
- 设置
- API Key
- 后台密码哈希
- 收集的表情包
- 学到的群聊记忆

## 常用命令

```bash
docker logs -f wechat_linux                                    # 看日志
docker restart wechat_linux                                    # 重启
docker exec wechat_linux pkill -f "^python3 /opt/wxhot/wxbot.py"   # 只重启机器人（改了 hot/ 里的代码后）
```

## 注意

- 微信对机器人行为有风控。别让它刷屏，别大量加好友。出现「你的账号被限制」之类的提示时，就把群聊活跃度调低。
- 本项目仅供学习交流，使用者自行承担账号风险，请遵守微信使用条款。
- 「微信」是腾讯的商标。安装包从腾讯官方地址下载，本仓库不分发微信本体。

## 许可证

MIT
