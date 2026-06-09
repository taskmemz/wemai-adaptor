# WeMai Adapter

> MaiBot 的微信适配器插件 —— 通过 WebSocket 桥接远端的微信客户端，让 LLM 收发微信消息、朋友圈、好友请求。

## 是什么

**WeMai Adapter** 是 MaiBot 的插件，部署在服务器上，开放一个 WebSocket 端口等待运行在 Windows 电脑上的 **WeMai Client** 连接。连接建立后，微信消息在这条隧道里双向流动：

```
微信 GUI ←→ WeMai Client (Windows) ←WS→ WeMai Adapter (服务器) ←→ MaiBot ←→ LLM
```

## 功能

| 能力 | 工具名 | 说明 |
|---|---|---|
| 收发文本/表情/图片/视频 | `@MessageGateway` | 自动处理，双向桥接 |
| 群聊 @ 检测 | `_build_segments_from_raw` | 自动从消息中提取 @ 成员 |
| 好友请求通知 | `hub_approve_friend` | LLM 在"系统"聊天流中接收好友请求通知，可批准或忽略 |
| 好友请求批准 | `hub_approve_friend` | 通知客户端通过好友申请 |
| 好友请求忽略 | `hub_dismiss_friend` | 通知客户端清除好友请求但不通过 |
| 通知管理员 | `hub_send_notification` | LLM 可主动向管理员发送通知 |
| 跨会话消息 | `hub_tell` | 中枢介入指定会话发言 |
| 读取朋友圈 | `read_wechat_moments` | 拉取最近朋友圈（字段：author/content/time/images） |
| 发朋友圈 | `post_wechat_moment` | 代表用户发布朋友圈文字 |
| 聊天过滤 | — | 配置群聊/私聊白名单 |
| 定时检查 | — | 内置中枢 tick 驱动 LLM 周期性巡检 |
| 消息去重 | — | 基于内容+时间的 MD5 去重 |
| 语音消息 | — | 客户端自动转文字后以 `[语音]xxx` 格式送达 LLM |

## 架构

```
┌──────────────────────────────────────────────────────────┐
│  服务器                                                    │
│  ┌─────────────────────────────────────────────────────┐  │
│  │  MaiBot                                              │  │
│  │  ┌────────────────────────────────────────────────┐  │  │
│  │  │  WemaiAdapterPlugin (plugin.py)                │  │  │
│  │  │  ├── @MessageGateway → 消息出入站双向桥接       │  │  │
│  │  │  ├── @Tool × 9 → LLM 可调用功能                │  │  │
│  │  │  │   hub_approve_friend     批准好友            │  │  │
│  │  │  │   hub_dismiss_friend     忽略好友            │  │  │
│  │  │  │   hub_tell               跨会话消息           │  │  │
│  │  │  │   hub_send_notification  系统通知            │  │  │
│  │  │  │   hub_check_chat_status  聊天状态检查         │  │  │
│  │  │  │   read_wechat_moments    读朋友圈            │  │  │
│  │  │  │   post_wechat_moment     发朋友圈            │  │  │
│  │  │  ├── 好友请求 → 注入中枢 → LLM 自主决策        │  │  │
│  │  │  ├── 中枢 tick → 定时巡检                      │  │  │
│  │  │  └── WemaiWsServer (raw TCP, length-prefix)  │──┼──┼── WS
│  │  └────────────────────────────────────────────────┘  │  │
│  └─────────────────────────────────────────────────────┘  │
└──────────────────────────────────────────────────────────┘
```

**协议**：基于 TCP，4 字节大端长度前缀 + UTF-8 JSON 消息体。Client 主动发起连接，Adapter 只监听不拨出。

## 好友请求处理流程

```
Client 检测到新好友请求
  → Adapter 收到 friend_request
    → 注入"系统"聊天流，附带操作说明
      → LLM 看到通知，自主决定：
        ├── hub_approve_friend → Client verify=True → 通过
        ├── hub_dismiss_friend → Client clear=True  → 忽略
        └── hub_tell → 通知管理员或其他会话
```

## 安装

将 `wemai-adaptor/` 放入 MaiBot 的 `plugins/` 目录，在 WebUI 中启用插件。

```
git clone https://github.com/taskmemz/wemai-adapter
cp -r wemai-adapter /path/to/MaiBot/plugins/
```

## 配置

所有配置在 MaiBot WebUI 的插件管理界面完成：

| 配置项 | 默认值 | 说明 |
|---|---|---|
| `enabled` | `false` | 是否启用 |
| `host` | `0.0.0.0` | WebSocket 监听地址 |
| `port` | `9721` | WebSocket 监听端口 |
| `admin` | `""` | 管理员微信名，同步到 Client 后在好友请求中展示给 LLM |
| `enable_chat_list_filter` | `false` | 是否按名单过滤聊天 |
| `group_list` | `[]` | 群聊白名单（空 = 全部） |
| `private_list` | `[]` | 私聊白名单（空 = 全部） |

## 依赖

- MaiBot ≥ 1.0.0
- maibot_sdk ≥ 2.0.0

## 常见问题

**Client 连不上？** 检查服务器防火墙是否放行了监控端口（默认 9721），以及 Client 的 `config.toml` 中 `server_host` 是否为服务器 IP。

**插件日志？** Adapter 的日志通过 MaiBot 的日志系统输出，logger 名为 `wemai_adapter`。

**WS 反复断连？** 将 Client 的 `reconnect_delay` 调大（如 15 秒），避免被中间网络设备限流。如果"0 bytes read"持续出现，检查服务端 MaiBot 是否有异常崩溃。
