from __future__ import annotations

from typing import ClassVar

from maibot_sdk import Field, PluginConfigBase

from .constants import DEFAULT_WS_HOST, DEFAULT_WS_PORT, SUPPORTED_CONFIG_VERSION


class WemaiPluginOptions(PluginConfigBase):
    __ui_label__: ClassVar[str] = "插件设置"
    __ui_order__: ClassVar[int] = 0

    enabled: bool = Field(
        default=False,
        description="是否启用 WeMai 适配器。",
        json_schema_extra={
            "hint": "关闭后插件保持空闲，不启动 WebSocket 服务器。",
            "label": "启用适配器",
            "order": 0,
        },
    )
    config_version: str = Field(
        default=SUPPORTED_CONFIG_VERSION,
        description="当前配置结构版本。",
        json_schema_extra={"disabled": True, "hidden": True, "label": "配置版本", "order": 99},
    )
    admin: list[str] = Field(
        default_factory=list,
        description="管理员会话名列表。中枢会将好友请求、系统通知等发给这些会话。",
        json_schema_extra={
            "hint": "留空则不启用。填写微信联系人名字（可多个），bot 会将重要通知发至此列表中的会话。",
            "label": "管理员",
            "order": 10,
            "placeholder": "请输入管理员会话名",
        },
    )

    def should_connect(self) -> bool:
        return self.enabled


class WemaiServerConfig(PluginConfigBase):
    __ui_label__: ClassVar[str] = "WebSocket 服务器"
    __ui_order__: ClassVar[int] = 1

    host: str = Field(
        default=DEFAULT_WS_HOST,
        description="WebSocket 服务器监听地址。",
        json_schema_extra={
            "hint": "wemai 客户端将连接到此地址。云服务器上设为 0.0.0.0。",
            "label": "监听地址",
            "order": 0,
            "placeholder": "0.0.0.0",
        },
    )
    port: int = Field(
        default=DEFAULT_WS_PORT,
        description="WebSocket 服务器监听端口。",
        json_schema_extra={
            "hint": "wemai 客户端连接到此端口。",
            "label": "监听端口",
            "order": 1,
        },
    )

    def build_ws_url(self) -> str:
        return f"ws://{self.host}:{self.port}"


class WemaiDataSourceConfig(PluginConfigBase):
    __ui_label__: ClassVar[str] = "数据源"
    __ui_order__: ClassVar[int] = 2

    mode: str = Field(
        default="pyweixin",
        description="数据源模式。pyweixin 使用 UIA 自动化，weflow 使用 WeFlow HTTP API。",
        json_schema_extra={
            "hint": "pyweixin 需要安装 pyweixin 包并登录桌面微信。weflow 需要 WeFlow 在后台运行。",
            "label": "数据源模式",
            "order": 0,
            "enum": ["pyweixin", "weflow"],
        },
    )
    weflow_base_url: str = Field(
        default="http://127.0.0.1:5031",
        description="WeFlow API 基础地址。",
        json_schema_extra={
            "hint": "仅数据源模式为 weflow 时生效。",
            "label": "WeFlow 地址",
            "order": 1,
            "placeholder": "http://127.0.0.1:5031",
        },
    )
    weflow_api_token: str = Field(
        default="",
        description="WeFlow API Token。",
        json_schema_extra={
            "hint": "仅数据源模式为 weflow 时生效。留空则不使用 Token。",
            "label": "WeFlow Token",
            "order": 2,
            "placeholder": "请输入 API Token",
        },
    )
    weflow_poll_interval: float = Field(
        default=0.8,
        description="WeFlow SSE 轮询间隔（秒）。",
        json_schema_extra={
            "hint": "仅数据源模式为 weflow 时生效。",
            "label": "轮询间隔",
            "order": 3,
        },
    )


class WemaiChatConfig(PluginConfigBase):
    __ui_label__: ClassVar[str] = "聊天过滤"
    __ui_order__: ClassVar[int] = 3

    enable_chat_list_filter: bool = Field(
        default=False,
        description="是否启用群聊与私聊名单过滤。",
        json_schema_extra={
            "hint": "开启后仅处理名单内的聊天消息。WeFlow 模式下请填写 wxid，pyweixin 模式下请填写显示名称。",
            "label": "启用聊天名单过滤",
            "order": 0,
        },
    )
    group_list: list[str] = Field(
        default_factory=list,
        description="群聊白名单。WeFlow: 填群 wxid (如 xxx@chatroom)；pyweixin: 填群显示名称。",
        json_schema_extra={
            "hint": "留空表示接收所有群聊。支持同时匹配 wxid 和显示名称。",
            "label": "群聊名单",
            "order": 1,
            "placeholder": "WeFlow: xxx@chatroom / pyweixin: 群名称",
        },
    )
    private_list: list[str] = Field(
        default_factory=list,
        description="私聊白名单。WeFlow: 填 wxid (如 wxid_xxx)；pyweixin: 填昵称或备注。",
        json_schema_extra={
            "hint": "留空表示接收所有私聊。支持同时匹配 wxid 和显示名称。",
            "label": "私聊名单",
            "order": 2,
            "placeholder": "WeFlow: wxid_xxx / pyweixin: 好友名",
        },
    )


class WemaiClientConfig(PluginConfigBase):
    __ui_label__: ClassVar[str] = "客户端行为"
    __ui_order__: ClassVar[int] = 4

    send_delay: float = Field(
        default=0.2,
        description="发送消息间隔（秒）。",
        json_schema_extra={
            "hint": "每次发送消息后的等待时间，避免微信触发频率限制。",
            "label": "发送间隔",
            "order": 0,
        },
    )
    close_weixin: bool = Field(
        default=False,
        description="发送后是否关闭微信。",
        json_schema_extra={
            "hint": "仅 pyweixin 模式生效。开启后每次发送完消息会关闭微信窗口。",
            "label": "关闭微信",
            "order": 1,
        },
    )
    include_muted: bool = Field(
        default=False,
        description="是否包含免打扰会话。",
        json_schema_extra={
            "hint": "仅 pyweixin 模式生效。开启后全局扫描会包含免打扰的聊天。",
            "label": "包含免打扰",
            "order": 2,
        },
    )
    excluded: list[str] = Field(
        default_factory=lambda: ["文件传输助手", "微信团队", "微信支付"],
        description="排除的会话名列表。",
        json_schema_extra={
            "hint": "这些会话的消息不会被监听和发送。",
            "label": "排除会话",
            "order": 3,
            "placeholder": "请输入要排除的会话名",
        },
    )


class WemaiPluginSettings(PluginConfigBase):
    plugin: WemaiPluginOptions = Field(default_factory=WemaiPluginOptions)
    ws_server: WemaiServerConfig = Field(default_factory=WemaiServerConfig)
    data_source: WemaiDataSourceConfig = Field(default_factory=WemaiDataSourceConfig)
    chat: WemaiChatConfig = Field(default_factory=WemaiChatConfig)
    client: WemaiClientConfig = Field(default_factory=WemaiClientConfig)

    def should_connect(self) -> bool:
        return self.plugin.should_connect()

    def validate_runtime_config(self) -> bool:
        if not self.plugin.config_version:
            return False
        return True
