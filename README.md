# WeMai Adapter

> MaiBot 的微信适配器插件 —— 通过 WebSocket 桥接远端的微信客户端，让 LLM 收发微信消息和朋友圈。

## 是什么

**WeMai Adapter** 是 MaiBot 的插件，部署在服务器上，开放一个 WebSocket 端口等待运行在 Windows 电脑上的 **WeMai Client** 连接。连接建立后，微信消息在这条隧道里双向流动：

```
微信 GUI  ←→  WeMai Client (Windows)  ←WS→  WeMai Adapter (服务器)  ←→  MaiBot  ←→  LLM
```

## 功能

| 能力 | 工具名 | 说明 |
|---|---|---|
| 收发文本/表情/图片/视频 | — | 自动处理，双向桥接 |
| 群聊 @ 检测 | — | 自动从消息中提取 @ 成员 |
| 读取朋友圈 | `read_wechat_moments` | 拉取最近 N 条朋友圈（最多 10 条） |
| 发朋友圈 | `post_wechat_moment` | 代表用户发布朋友圈文字 |
| 好友请求通知 | — | 检测到好友请求后通知 MaiBot，可批准 |
| 发送系统通知 | `hub_send_notification` | 向任意会话推送通知消息 |
| 跨会话消息 | `hub_tell` | 中枢介入指定会话发言 |
| 聊天过滤 | — | 配置群聊/私聊白名单 |
| 定时检查 | — | 内置中枢定时 tick，驱动周期性巡检 |

## 架构

```
┌─────────────────────────────────────────────────────┐
│  服务器                                              │
│  ┌───────────────────────────────────────────────┐  │
│  │  MaiBot                                        │  │
│  │  ┌──────────────────────────────────────────┐  │  │
│  │  │  WemaiAdapterPlugin (plugin.py)          │  │  │
│  │  │  ├── @MessageGateway → 消息出入站         │  │  │
│  │  │  ├── @Tool × 7 → LLM 可调用功能          │  │  │
│  │  │  ├── 中枢 tick → 定时巡检                │  │  │
│  │  │  └── WemaiWsServer (WS, length-prefix)  │──┼──┼── WebSocket
│  │  └──────────────────────────────────────────┘  │  │
│  └───────────────────────────────────────────────┘  │
└─────────────────────────────────────────────────────┘
```

**协议**：基于 TCP，4 字节大端长度前缀 + UTF-8 JSON 消息体。Client 主动发起连接，Adapter 只监听不拨出。

## 安装

将 `wemai-adaptor/` 放入 MaiBot 的 `plugins/` 目录，在 WebUI 中启用插件。

```bash
git clone https://github.com/Mai-with-u/wemai-adapter
cp -r wemai-adapter /path/to/MaiBot/plugins/
```

## 配置

所有配置在 MaiBot WebUI 的插件管理界面完成：

| 配置项 | 默认值 | 说明 |
|---|---|---|
| `enabled` | `false` | 是否启用 |
| `host` | `0.0.0.0` | WebSocket 监听地址 |
| `port` | `9721` | WebSocket 监听端口 |
| `admin` | `""` | 管理员微信名，用于接收好友请求等系统通知 |
| `enable_chat_list_filter` | `false` | 是否按名单过滤聊天 |
| `group_list` | `[]` | 群聊白名单（空 = 全部） |
| `private_list` | `[]` | 私聊白名单（空 = 全部） |

## 依赖

- MaiBot ≥ 1.0.0
- maibot_sdk ≥ 2.0.0

## 常见问题

**Client 连不上？** 检查服务器防火墙是否放行了 WebSocket 端口（默认 9721），以及 Client 的 `config.toml` 中 `server_host` 是否为服务器 IP。

**插件日志？** Adapter 的日志通过 MaiBot 的日志系统输出，logger 名为 `wemai_adapter`。
