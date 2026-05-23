# WeMai Adapter — 微信 × MaiBot 桥梁 🚀

> **让 MaiBot 睁开眼睛，看见微信世界。**
>
> _Let MaiBot open its eyes and see the WeChat universe._

[中文](#中文) · [English](#english)

---

## 中文

### 📖 这是什么

**WeMai Adapter** 是 [MaiBot](https://github.com/MaiM-with-u/MaiBot) 的插件，部署在云服务器上。它开了一个 WebSocket 窗口（默认 `0.0.0.0:9721`），等待远方的 **WeMai Client**（运行在你的 Windows 电脑上）连进来。

当 Client 连上来后，这条隧道就成了：

```
微信消息 → Client(Windows) → WebSocket → Adapter(云) → MaiBot → LLM
微信朋友圈 ← Client(Windows) ← WebSocket ← Adapter(云) ← MaiBot ← LLM
```

你以为 MaiBot 只是在跟你聊天？不，它在看你的朋友圈、收你的表情包、还能帮你发朋友圈。

### ✨ 能干什么

| 功能 | 说明 |
|------|------|
| 💬 **收发消息** | 文本/表情包/图片/视频消息双向传递 |
| 😄 **表情包识别** | 友发的 GIF 动画表情，MaiBot 知道那是"[动画表情]" |
| @ **艾特人** | 群聊 @ 成员，自动在消息前加 `@昵称` |
| 📱 **朋友圈读** | MaiBot 可以用 `read_wechat_moments` 工具看你的朋友圈 |
| 📝 **朋友圈发** | MaiBot 可以用 `post_wechat_moment` 工具替你发朋友圈 |
| 🔒 **白名单过滤** | 只监听你指定的群和好友 |
| 🔄 **断线重连** | Client 断连后自动等待，重连即恢复 |

### 🧩 架构一览

```
┌─────────────────────────────────────────────────────────┐
│                      云服务器                            │
│  ┌──────────────────────────────────────┐               │
│  │            MaiBot                     │               │
│  │  ┌─────────────────────────────────┐  │               │
│  │  │  WeMai Adapter (plugin)         │  │               │
│  │  │  ├── @MessageGateway(wechat)    │  │               │
│  │  │  ├── @Tool: read_wechat_moments │  │               │
│  │  │  ├── @Tool: post_wechat_moment  │  │               │
│  │  │  └── WS Server (0.0.0.0:9721)  │──┼── WebSocket   │
│  │  └─────────────────────────────────┘  │               │
│  └──────────────────────────────────────┘               │
└──────────────────┬──────────────────────────────────────┘
                   │ WebSocket (length-prefixed JSON)
                   │
┌──────────────────▼──────────────────────────────────────┐
│                    Windows 电脑                          │
│  ┌──────────────────────────────────────┐               │
│  │  WeMai Client                        │               │
│  │  ├── wx_listener: 轮询微信窗口       │               │
│  │  ├── wx_sender: 向微信打字           │               │
│  │  ├── wx_moments: 朋友圈读写          │               │
│  │  └── ws_client: 连接 Adapter         │               │
│  └──────────────────────────────────────┘               │
└─────────────────────────────────────────────────────────┘
```

### ⚡ 安装

把 `wemai-adapter/` 整个目录塞进 MaiBot 的 `plugins/` 目录：

```bash
cp -r wemai-adapter /path/to/MaiBot/plugins/
```

在 **WebUI → 插件管理** 中启用插件，配置监听地址（默认 `0.0.0.0:9721`）。

### ⚙️ 配置

插件配置通过 WebUI 完成，无需手写 config.toml：

| 选项 | 默认 | 说明 |
|------|------|------|
| `enabled` | `false` | 启用适配器 |
| `host` | `0.0.0.0` | WS 监听地址 |
| `port` | `9721` | WS 监听端口 |
| `enable_chat_list_filter` | `false` | 启用聊天名单过滤 |
| `group_list` | `[]` | 群聊白名单（空=全部接收） |
| `private_list` | `[]` | 私聊白名单（空=全部接收） |

### 🤝 依赖

- MaiBot >= 1.0.0-pre.24
- maibot_sdk >= 2.0.0
- maim_message

### ⚠️ 建议

- 云服务器防火墙要放开 WS 端口
- Client 端需要能 `telnet 你服务器IP 9721` 通才行
- 日志走 Runner stderr，在 MaiBot 控制台能看到 `[wemai]` 前缀的输出

---

## English

### 📖 What Is This

**WeMai Adapter** is a [MaiBot](https://github.com/MaiM-with-u/MaiBot) plugin deployed on your cloud server. It opens a WebSocket window (default `0.0.0.0:9721`) and waits for the **WeMai Client** — running on your Windows PC — to connect.

Once connected, this tunnel comes alive:

```
WeChat message → Client(Windows) → WebSocket → Adapter(Cloud) → MaiBot → LLM
Moments ← Client(Windows) ← WebSocket ← Adapter(Cloud) ← MaiBot ← LLM
```

Think MaiBot is just chatting with you? Nope — it's reading your moments, receiving your GIF stickers, and can even post moments on your behalf.

### ✨ Features

| Feature | Description |
|---------|-------------|
| 💬 **Message relay** | Text, emoji, image, video — bidirectionally |
| 😄 **Sticker detection** | Knows when someone sends an animated GIF sticker |
| @ **Mentions** | Group chat @-mentions handled automatically |
| 📱 **Read moments** | MaiBot calls `read_wechat_moments` to browse your feed |
| 📝 **Post moments** | MaiBot calls `post_wechat_moment` to publish for you |
| 🔒 **Chat filter** | Only listen to specific groups or private chats |
| 🔄 **Auto-reconnect** | Client drops and reconnects — seamless resume |

### 🧩 Architecture

```
┌─────────────────────────────────────────────────────────┐
│                    Cloud Server                          │
│  ┌──────────────────────────────────────┐               │
│  │            MaiBot                     │               │
│  │  ┌─────────────────────────────────┐  │               │
│  │  │  WeMai Adapter (plugin)         │  │               │
│  │  │  ├── @MessageGateway(wechat)    │  │               │
│  │  │  ├── @Tool: read_wechat_moments │  │               │
│  │  │  ├── @Tool: post_wechat_moment  │  │               │
│  │  │  └── WS Server (0.0.0.0:9721)  │──┼── WebSocket   │
│  │  └─────────────────────────────────┘  │               │
│  └──────────────────────────────────────┘               │
└──────────────────┬──────────────────────────────────────┘
                   │ WebSocket (length-prefixed JSON)
                   │
┌──────────────────▼──────────────────────────────────────┐
│                    Windows PC                            │
│  ┌──────────────────────────────────────┐               │
│  │  WeMai Client                        │               │
│  │  ├── wx_listener: polls WeChat GUI   │               │
│  │  ├── wx_sender: types into WeChat    │               │
│  │  ├── wx_moments: read/write moments  │               │
│  │  └── ws_client: connects to Adapter  │               │
│  └──────────────────────────────────────┘               │
└─────────────────────────────────────────────────────────┘
```

### ⚡ Installation

Copy `wemai-adapter/` into MaiBot's `plugins/`:

```bash
cp -r wemai-adapter /path/to/MaiBot/plugins/
```

Enable the plugin via **WebUI → Plugin Manager**. Configure the WebSocket host/port (default `0.0.0.0:9721`).

### ⚙️ Configuration

All settings go through the WebUI — no manual config files needed:

| Option | Default | Description |
|--------|---------|-------------|
| `enabled` | `false` | Enable the adapter |
| `host` | `0.0.0.0` | WS listen address |
| `port` | `9721` | WS listen port |
| `enable_chat_list_filter` | `false` | Enable chat whitelist |
| `group_list` | `[]` | Group chat whitelist (empty=all) |
| `private_list` | `[]` | Private chat whitelist (empty=all) |

### 🤝 Dependencies

- MaiBot >= 1.0.0-pre.24
- maibot_sdk >= 2.0.0
- maim_message

### ⚠️ Notes

- Open the WS port in your cloud firewall
- Client needs TCP connectivity to your server
- Plugin logs appear with `[wemai]` prefix in MaiBot console

---

**WeMai Adapter** — 微信的入口，MaiBot 的出口。
_WeChat's doorway, MaiBot's outlet._

Built with ❤️ by the MaiBot community.
