from __future__ import annotations

from collections.abc import Mapping
from typing import Any, ClassVar, Dict, List, Optional

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

    def should_connect(self) -> bool:
        return self.enabled


class WemaiServerConfig(PluginConfigBase):
    __ui_label__: ClassVar[str] = "WebSocket 服务器"
    __ui_order__: ClassVar[int] = 1

    host: str = Field(
        default=DEFAULT_WS_HOST,
        description="WebSocket 服务器监听地址。",
        json_schema_extra={
            "hint": "pyweixin 客户端将连接到此地址。云服务器上设为 0.0.0.0。",
            "label": "监听地址",
            "order": 0,
            "placeholder": "0.0.0.0",
        },
    )
    port: int = Field(
        default=DEFAULT_WS_PORT,
        description="WebSocket 服务器监听端口。",
        json_schema_extra={
            "hint": "pyweixin 客户端连接到此端口。",
            "label": "监听端口",
            "order": 1,
        },
    )

    def build_ws_url(self) -> str:
        return f"ws://{self.host}:{self.port}"


class WemaiChatConfig(PluginConfigBase):
    __ui_label__: ClassVar[str] = "聊天过滤"
    __ui_order__: ClassVar[int] = 2

    enable_chat_list_filter: bool = Field(
        default=False,
        description="是否启用群聊与私聊名单过滤。",
        json_schema_extra={
            "hint": "开启后仅处理名单内的聊天消息。",
            "label": "启用聊天名单过滤",
            "order": 0,
        },
    )
    group_list: List[str] = Field(
        default_factory=list,
        description="群聊白名单（group_name 列表）。",
        json_schema_extra={
            "hint": "留空表示接收所有群聊。",
            "label": "群聊名单",
            "order": 1,
            "placeholder": "请输入群名",
        },
    )
    private_list: List[str] = Field(
        default_factory=list,
        description="私聊白名单（nickname 列表）。",
        json_schema_extra={
            "hint": "留空表示接收所有私聊。",
            "label": "私聊名单",
            "order": 2,
            "placeholder": "请输入好友名",
        },
    )


class WemaiPluginSettings(PluginConfigBase):
    plugin: WemaiPluginOptions = Field(default_factory=WemaiPluginOptions)
    ws_server: WemaiServerConfig = Field(default_factory=WemaiServerConfig)
    chat: WemaiChatConfig = Field(default_factory=WemaiChatConfig)
    admin: str = Field(
        default="",
        description="管理员用户名。中枢会将好友请求、系统通知等发给该用户，管理员可回应批准。",
        json_schema_extra={
            "hint": "留空则不启用管理员功能。填写微信联系人名字。",
            "label": "管理员",
            "order": 10,
            "placeholder": "某不知名的赵",
        },
    )

    def should_connect(self) -> bool:
        return self.plugin.should_connect()

    def validate_runtime_config(self) -> bool:
        if not self.plugin.config_version:
            return False
        return True
