from __future__ import annotations

import asyncio
import base64
import hashlib
import logging
import os
import re
import time
import uuid
from typing import Any, ClassVar, Dict, Optional, cast

from maibot_sdk import MaiBotPlugin, MessageGateway, PluginConfigBase, Tool

from .config import WemaiPluginSettings
from .constants import WEMAI_GATEWAY_NAME
from .runtime import WemaiWsServer

logger = logging.getLogger("wemai_adapter")


class WemaiAdapterPlugin(MaiBotPlugin):
    config_model: ClassVar[type[PluginConfigBase] | None] = WemaiPluginSettings

    HUB_SESSION_NAME = "微信系统"

    def __init__(self) -> None:
        super().__init__()
        self._ws_server: Optional[WemaiWsServer] = None
        self._pending_requests: dict[str, asyncio.Future] = {}
        self._resp_lock = asyncio.Lock()
        # 出站消息缓冲（ws_server 不可用或客户端未连接时排队）
        self._pending_outbound: list[dict[str, Any]] = []
        # 中枢后台任务
        self._hub_task: Optional[asyncio.Task] = None

    async def on_load(self) -> None:
        logger.info("on_load 被调用, enabled=%s", self._is_enabled())
        await self._restart_server_if_needed()
        # 定时轮询已关闭，由工具调用触发中枢思考

    def _is_enabled(self) -> bool:
        try:
            return self._load_settings().plugin.enabled
        except Exception:
            return False

    async def on_unload(self) -> None:
        self._stop_hub_tick()
        await self._stop_server()

    async def on_config_update(self, scope: str, config_data: Dict[str, Any], version: str) -> None:
        if scope != "self":
            return
        self.set_plugin_config(config_data)
        try:
            await self._restart_server_if_needed()
        except Exception as e:
            logger.error("重启 WS 服务器失败: %s", e)
        try:
            await asyncio.wait_for(self._push_config_to_client(), timeout=5.0)
        except (asyncio.TimeoutError, Exception) as e:
            logger.debug("推送配置到客户端失败（可能未连接）: %s", e)

    @MessageGateway(
        name=WEMAI_GATEWAY_NAME,
        route_type="duplex",
        platform="wechat",
        protocol="wemai",
        description="WeMai 微信双工消息网关",
    )
    async def handle_wemai_gateway(
        self,
        message: Dict[str, Any],
        route: Optional[Dict[str, Any]] = None,
        metadata: Optional[Dict[str, Any]] = None,
        **kwargs: Any,
    ) -> Dict[str, Any]:
        import sys
        sys.stderr.write(f"[wemai] outbound message={message} route={route}\n")
        sys.stderr.flush()
        outbound = {
            "type": "outbound",
            "message_id": message.get("message_id", ""),
            "receiver": "",
            "segments": [],
        }
        mi = message.get("message_info", {})

        # 接收者：优先取 additional_config.platform_io_target_user_id
        additional = mi.get("additional_config") or {}
        outbound["receiver"] = (
            additional.get("platform_io_target_user_id")
            or additional.get("target_user_id")
            or ""
        )
        if not outbound["receiver"]:
            group_info = mi.get("group_info") or {}
            user_info = mi.get("user_info") or {}
            outbound["receiver"] = (
                group_info.get("group_name")
                or user_info.get("user_nickname")
                or ""
            )

        # 提取文本：raw_message 已经是 list[Seg]，直接遍历
        raw_msg = message.get("raw_message", [])
        if raw_msg:
            segments, at_members = self._build_segments_from_raw(raw_msg)
        else:
            seg = message.get("message_segment", {})
            texts = self._extract_text(seg)
            segments = [{"type": "text", "data": t} for t in texts if t]
            at_members = []

        outbound["segments"] = segments
        outbound["at_members"] = at_members

        if outbound["receiver"] and segments:
            # 中枢消息不回传给微信客户端
            if outbound["receiver"] in (self.HUB_SESSION_NAME, "系统"):
                logger.debug("中枢消息已拦截: %s", outbound["segments"])
                return {"success": True}
            ok = await self._send_outbound(outbound)
            return {
                "success": ok,
                "external_message_id": outbound.get("message_id"),
            }
        return {"success": True}

    @staticmethod
    def _build_segments_from_raw(raw: list[Any]) -> tuple[list[dict], list[str]]:
        result: list[dict] = []
        at_members: list[str] = []
        emoji_map = {
            "laugh": "[呲牙]", "smile": "[微笑]", "cry": "[流泪]", "angry": "[发怒]",
            "surprised": "[惊讶]", "fear": "[恐惧]", "cool": "[酷]", "sad": "[难过]",
            "shy": "[害羞]", "sleepy": "[困]", "love": "[爱心]", "ok": "[OK]",
            "clap": "[鼓掌]", "think": "[思考]", "wave": "[挥手]", "strong": "[强]",
            "weak": "[弱]", "rose": "[玫瑰]", "heart": "[爱心]", "broken_heart": "[心碎]",
            "cake": "[蛋糕]", "coffee": "[咖啡]", "beer": "[啤酒]",
        }
        for seg in raw:
            if not isinstance(seg, dict):
                continue
            stype = seg.get("type", "")
            sdata = seg.get("data", "")
            if stype == "text":
                if isinstance(sdata, str):
                    result.append({"type": "text", "data": sdata})
                # dict data is reply etc, skip
            elif stype == "emoji":
                if isinstance(sdata, str):
                    text = emoji_map.get(sdata, f"[{sdata}]")
                    result.append({"type": "text", "data": text})
                elif isinstance(sdata, dict):
                    name = sdata.get("emoji_name") or sdata.get("name") or ""
                    text = emoji_map.get(name, f"[{name}]") if name else ""
                    if text:
                        result.append({"type": "text", "data": text})
            elif stype == "at":
                if isinstance(sdata, str):
                    at_members.append(sdata)
                    result.append({"type": "text", "data": f"@{sdata} "})
                elif isinstance(sdata, dict):
                    name = (
                        sdata.get("user_nickname")
                        or sdata.get("name")
                        or sdata.get("target_user_nickname")
                        or sdata.get("target_user_id")
                        or ""
                    )
                    if name:
                        at_members.append(name)
                        result.append({"type": "text", "data": f"@{name} "})
            elif stype == "seglist" and isinstance(sdata, (list, tuple)):
                sub_segs, sub_ats = WemaiAdapterPlugin._build_segments_from_raw(list(sdata))
                result.extend(sub_segs)
                at_members.extend(sub_ats)
        # 从文本段中提取 @某人，追加到 at_members（MaiBot 可能生成 text 而非 at 类型）
        for seg in result:
            if seg.get("type") != "text":
                continue
            text = seg.get("data", "")
            for token in re.split(r'[\s(（]+', text):
                if token.startswith("@") and len(token) > 1:
                    name = token[1:].rstrip(")）")
                    if name and name not in at_members:
                        at_members.append(name)
        return result, at_members

    def _extract_text(self, seg: Any, collector: Optional[list[str]] = None) -> list[str]:
        if collector is None:
            collector = []
        if isinstance(seg, dict):
            stype = seg.get("type", "")
            sdata = seg.get("data", "")
            if stype == "text" and isinstance(sdata, str):
                collector.append(sdata)
            elif stype == "seglist" and isinstance(sdata, (list, tuple)):
                for s in sdata:
                    self._extract_text(s, collector)
        return collector

    @staticmethod
    def _extract_text_from_raw(raw: list[Any]) -> list[str]:
        result: list[str] = []
        for seg in raw:
            if isinstance(seg, dict):
                stype = seg.get("type", "")
                sdata = seg.get("data", "")
                if stype == "text":
                    if isinstance(sdata, str):
                        result.append(sdata)
                    elif isinstance(sdata, dict):
                        # e.g. reply seg: {"type":"reply","data":{...}} 
                        pass
                elif stype == "seglist" and isinstance(sdata, (list, tuple)):
                    result.extend(WemaiAdapterPlugin._extract_text_from_raw(list(sdata)))
        return result

    async def _handle_client_inbound(self, data: Dict[str, Any]) -> None:
        msg_type = data.get("type", "")

        if msg_type == "sync_config":
            await self._push_config_to_client()
            return

        if msg_type == "moment_response":
            req_id = data.get("request_id", "")
            if req_id:
                async with self._resp_lock:
                    future = self._pending_requests.pop(req_id, None)
                if future and not future.done():
                    future.set_result(data)
            return

        if msg_type != "inbound":
            return

        chat = data.get("chat", "")
        sender = data.get("sender", "")
        content = data.get("content", "")
        is_group = data.get("is_group", False)
        sub_type = data.get("msg_type", "text")  # text | emoji | image | video
        media_path = data.get("media_path", "")   # 媒体文件路径
        media_base64 = data.get("media_base64", "")  # base64 编码的媒体文件内容
        media_ext = data.get("media_ext", ".png")  # 文件扩展名

        if not sender or not content:
            return

        logger.info("收到入站消息: [%s] %s: %s (%s)", chat, sender, content[:120], sub_type)

        settings = self._load_settings()
        if settings.chat.enable_chat_list_filter:
            if is_group and settings.chat.group_list and chat not in settings.chat.group_list:
                return
            if not is_group and settings.chat.private_list and chat not in settings.chat.private_list:
                return

        # 如果客户端传了 base64 图片数据，解码保存到临时文件
        if media_base64:
            try:
                import tempfile
                raw = base64.b64decode(media_base64)
                ext = media_ext if media_ext.startswith(".") else f".{media_ext}"
                tmp = tempfile.NamedTemporaryFile(suffix=ext, delete=False)
                tmp.write(raw)
                tmp.close()
                media_path = tmp.name
                logger.info("已保存媒体文件: %s (%d bytes)", media_path, len(raw))
            except Exception as e:
                logger.warning("保存媒体文件失败: %s", e)

        msg_id = hashlib.md5(
            f"{chat}|{sender}|{content}|{time.time()}".encode()
        ).hexdigest()

        group_info_val = None
        if is_group:
            group_info_val = {"platform": "wechat", "group_id": chat, "group_name": chat}

        # 根据消息子类型构造 segment
        if sub_type == "emoji":
            seg_data: list[dict] = [{"type": "emoji", "data": {"emoji_name": "animated_sticker"}}]
            if media_path:
                seg_data.append({"type": "image", "data": media_path})
        elif sub_type == "image":
            seg_data = [{"type": "image", "data": media_path or content}]
        elif sub_type == "video":
            seg_data = [{"type": "video", "data": media_path or content}]
        else:
            seg_data = [{"type": "text", "data": content}]

        message_dict = {
            "message_id": msg_id,
            "platform": "wechat",
            "message_info": {
                "platform": "wechat",
                "message_id": msg_id,
                "time": time.time(),
                "user_info": {
                    "platform": "wechat",
                    "user_id": sender,
                    "user_nickname": sender,
                },
                "group_info": group_info_val,
            },
            "message_segment": {
                "type": "seglist",
                "data": seg_data,
            },
            "raw_message": [{"type": "text", "data": content}],
        }

        accepted = await self.ctx.gateway.route_message(
            gateway_name=WEMAI_GATEWAY_NAME,
            message=message_dict,
        )
        if accepted:
            logger.info("入站已注入: [%s] %s: %s", chat, sender, content[:60])
        else:
            logger.warning("入站被拒绝: [%s] %s", chat, sender)

    async def _push_config_to_client(self) -> None:
        settings = self._load_settings()
        payload = {
            "type": "config_update",
            "enable_filter": settings.chat.enable_chat_list_filter,
            "group_list": settings.chat.group_list,
            "private_list": settings.chat.private_list,
        }
        await self._send_outbound(payload)

    async def _send_outbound(self, data: Dict[str, Any]) -> bool:
        if self._ws_server is not None:
            ok = await self._ws_server.send_outbound(data)
            if ok:
                return True
            # ws_server 返回 False → 客户端断开且队列满了等极端情况
        # 插件层本地排队，保证永不丢失
        self._pending_outbound.append(data)
        return True

    async def _drain_pending_outbound(self) -> None:
        if not self._pending_outbound:
            return
        if self._ws_server is None:
            return
        batch = list(self._pending_outbound)
        self._pending_outbound.clear()
        for data in batch:
            await self._ws_server.send_outbound(data)
        if batch:
            logger.info("已发送 %d 条排队出站消息", len(batch))

    async def _send_request(self, req_type: str, params: dict, timeout: float = 15.0) -> dict:
        req_id = str(uuid.uuid4())[:8]
        future: asyncio.Future = asyncio.get_event_loop().create_future()
        async with self._resp_lock:
            self._pending_requests[req_id] = future
        await self._send_outbound({"type": req_type, "request_id": req_id, **params})
        try:
            return await asyncio.wait_for(future, timeout=timeout)
        except asyncio.TimeoutError:
            async with self._resp_lock:
                self._pending_requests.pop(req_id, None)
            return {"error": "timeout", "success": False}

    @Tool(
        name="read_wechat_moments",
        description="读取微信朋友圈的最新动态,返回最近发布的朋友圈内容列表。每次最多读取10条。",
        parameters={
            "type": "object",
            "properties": {
                "limit": {"type": "integer", "description": "读取条数，最多10条", "default": 5}
            },
        },
    )
    async def tool_read_moments(self, limit: int = 5, **kwargs: Any) -> dict:
        result = await self._send_request("moment_read", {"limit": min(limit, 10)})
        moments = result.get("moments", [])
        count = result.get("count", len(moments))
        return {"success": True, "count": count, "moments": moments[:limit]}

    @Tool(
        name="post_wechat_moment",
        description="发布一条微信朋友圈,可以带文字内容。发布成功后返回发布结果。",
        parameters={
            "type": "object",
            "properties": {
                "text": {"type": "string", "description": "朋友圈的文字内容"}
            },
            "required": ["text"],
        },
    )
    async def tool_post_moment(self, text: str, **kwargs: Any) -> dict:
        result = await self._send_request("moment_post", {"text": text})
        ok = result.get("success", False)
        return {"success": ok, "text": text, "message": "朋友圈已发布" if ok else "发布失败"}

    # ─── 微信系统中枢 ──────────────────────────────

    def _start_hub_tick(self) -> None:
        """启动中枢定时思考"""
        self._stop_hub_tick()
        self._hub_task = asyncio.create_task(self._hub_tick_loop())

    def _stop_hub_tick(self) -> None:
        if self._hub_task is not None:
            self._hub_task.cancel()
            self._hub_task = None

    async def _hub_tick_loop(self) -> None:
        """随机间隔（3-10分钟）向中枢注入 tick，驱动自动思考"""
        import random as _random
        try:
            while True:
                delay = _random.randint(180, 600)
                await asyncio.sleep(delay)
                try:
                    await self._inject_to_hub("系统", "tick", "定时检查时间")
                except Exception as e:
                    logger.debug("中枢 tick 注入失败: %s", e)
        except asyncio.CancelledError:
            pass

    async def _inject_to_hub(self, sender: str, content: str, plain: str = "") -> None:
        """向「微信系统」中枢会话注入一条消息，触发 HeartFlow 思考"""
        msg_id = hashlib.md5(f"hub|{sender}|{content}|{time.time()}".encode()).hexdigest()
        msg = {
            "message_id": msg_id,
            "platform": "wechat",
            "message_info": {
                "platform": "wechat",
                "message_id": msg_id,
                "time": time.time(),
                "user_info": {
                    "platform": "wechat",
                    "user_id": sender,
                    "user_nickname": sender,
                },
                "group_info": None,
            },
            "message_segment": {
                "type": "seglist",
                "data": [{"type": "text", "data": content}],
            },
            "raw_message": [{"type": "text", "data": plain or content}],
        }
        ok = await self.ctx.gateway.route_message(
            gateway_name=WEMAI_GATEWAY_NAME,
            message=msg,
        )
        if ok:
            logger.debug("中枢消息已注入: [%s] %s", sender, content[:40])

    @Tool(
        name="hub_send_notification",
        description="【微信系统中枢】向用户发送一条系统通知。当需要提醒用户、报告任务结果或通知系统状态时使用。",
        parameters={
            "type": "object",
            "properties": {
                "title": {"type": "string", "description": "通知标题"},
                "content": {"type": "string", "description": "通知内容"},
            },
            "required": ["content"],
        },
    )
    async def tool_hub_send_notification(self, title: str = "", content: str = "", **kwargs: Any) -> dict:
        if not content:
            return {"success": False, "error": "缺少通知内容"}
        text = f"[系统通知] {title} {content}".strip()
        await self._send_outbound({
            "type": "outbound",
            "receiver": self.HUB_SESSION_NAME,
            "segments": [{"type": "text", "data": text}],
            "at_members": [],
        })
        logger.info("中枢通知: %s", text[:60])
        asyncio.create_task(self._inject_to_hub("系统", f"notice:{title}", f"已发送通知: {content[:40]}"))
        return {"success": True, "message": f"通知已发送: {text[:40]}"}

    @Tool(
        name="hub_check_chat_status",
        description="【微信系统中枢】检查当前所有监控聊天的状态摘要。适合定期巡检，查看各聊天活跃度和待处理事项。",
    )
    async def tool_hub_check_chat_status(self, **kwargs: Any) -> dict:
        return {
            "success": True,
            "message": "当前所有聊天流运行正常，等待进一步指令。",
        }

    @Tool(
        name="hub_delayed_task",
        description="【微信系统中枢】延迟执行一个任务。在指定分钟后向中枢发回提醒。",
        parameters={
            "type": "object",
            "properties": {
                "task_desc": {"type": "string", "description": "任务描述"},
                "delay_minutes": {"type": "integer", "description": "延迟分钟数", "default": 5},
            },
            "required": ["task_desc"],
        },
    )
    async def tool_hub_delayed_task(self, task_desc: str = "", delay_minutes: int = 5, **kwargs: Any) -> dict:
        if not task_desc:
            return {"success": False, "error": "缺少任务描述"}
        asyncio.create_task(self._hub_delayed_reminder(task_desc, delay_minutes))
        logger.info("中枢延迟任务: %s (%d分钟后)", task_desc[:40], delay_minutes)
        return {"success": True, "message": f"已安排任务「{task_desc[:30]}」，{delay_minutes} 分钟后提醒"}

    async def _hub_delayed_reminder(self, task_desc: str, delay_minutes: int) -> None:
        try:
            await asyncio.sleep(delay_minutes * 60)
            await self._inject_to_hub("系统", "reminder", f"提醒：{task_desc}")
        except asyncio.CancelledError:
            pass

    async def _inject_to_session(self, chat_name: str, sender: str, content: str, plain: str = "") -> bool:
        """向另一个会话注入消息（跨会话通信）"""
        msg_id = hashlib.md5(f"cross|{chat_name}|{sender}|{content}|{time.time()}".encode()).hexdigest()
        msg = {
            "message_id": msg_id,
            "platform": "wechat",
            "message_info": {
                "platform": "wechat",
                "message_id": msg_id,
                "time": time.time(),
                "user_info": {"platform": "wechat", "user_id": sender, "user_nickname": sender},
                "group_info": {"platform": "wechat", "group_id": chat_name, "group_name": chat_name},
            },
            "message_segment": {"type": "seglist", "data": [{"type": "text", "data": content}]},
            "raw_message": [{"type": "text", "data": plain or content}],
        }
        ok = await self.ctx.gateway.route_message(gateway_name=WEMAI_GATEWAY_NAME, message=msg)
        if ok:
            logger.info("跨会话消息已注入: [%s] %s: %s", chat_name, sender, content[:40])
        return ok

    @Tool(
        name="hub_tell",
        description="【微信系统中枢】向指定会话发送一条消息。中枢思考后需要对某个对话做出回应时使用。",
        parameters={
            "type": "object",
            "properties": {
                "target": {"type": "string", "description": "目标会话名称"},
                "message": {"type": "string", "description": "消息内容"},
            },
            "required": ["target", "message"],
        },
    )
    async def tool_hub_tell(self, target: str = "", message: str = "", **kwargs: Any) -> dict:
        if not target or not message:
            return {"success": False, "error": "缺少目标或消息"}
        ok = await self._inject_to_session(target, "系统", message)
        return {"success": ok, "message": f"已向 {target} 发送消息"}

    async def _restart_server_if_needed(self) -> None:
        await self._stop_server()
        settings = self._load_settings()
        if not settings.should_connect():
            return
        if not settings.validate_runtime_config():
            return

        self._ensure_server()
        if self._ws_server is not None:
            self._ws_server.set_inbound_handler(self._handle_client_inbound)
            await self._ws_server.start()
            # 发送启动前排队的所有出站消息
            await self._drain_pending_outbound()
            await self.ctx.gateway.update_state(
                gateway_name=WEMAI_GATEWAY_NAME,
                ready=True,
                platform="wechat",
                metadata={"server": settings.ws_server.build_ws_url()},
            )
            logger.info(
                "WeMai 适配器已启动: %s",
                settings.ws_server.build_ws_url(),
            )

    async def _stop_server(self) -> None:
        if self._ws_server is not None:
            await self._ws_server.stop()
            self._ws_server = None
        try:
            await self.ctx.gateway.update_state(
                gateway_name=WEMAI_GATEWAY_NAME,
                ready=False,
            )
        except Exception:
            pass

    def _ensure_server(self) -> None:
        if self._ws_server is None:
            settings = self._load_settings()
            self._ws_server = WemaiWsServer(
                host=settings.ws_server.host,
                port=settings.ws_server.port,
            )

    def _load_settings(self) -> WemaiPluginSettings:
        return cast(WemaiPluginSettings, self.config)


def create_plugin() -> WemaiAdapterPlugin:
    return WemaiAdapterPlugin()
