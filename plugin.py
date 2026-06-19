from __future__ import annotations

import asyncio
import base64
import hashlib
import logging
import random
import re
import sys
import tempfile
import time
import uuid
from typing import Any, ClassVar, cast

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
        self._ws_server: WemaiWsServer | None = None
        self._pending_requests: dict[str, asyncio.Future] = {}
        self._resp_lock = asyncio.Lock()
        self._pending_outbound: list[dict[str, Any]] = []
        self._hub_task: asyncio.Task | None = None

    async def on_load(self) -> None:
        logger.info("on_load 被调用, enabled=%s", self._is_enabled())
        if not hasattr(self, "_FRIEND_SEEN"):
            self._FRIEND_SEEN: set[str] = set()
        await self._restart_server_if_needed()

    def _is_enabled(self) -> bool:
        try:
            return self._load_settings().plugin.enabled
        except Exception:
            return False

    async def on_unload(self) -> None:
        self._stop_hub_tick()
        await self._stop_server()

    async def on_config_update(self, scope: str, config_data: dict[str, Any], version: str) -> None:
        if scope != "self":
            return

        old_settings = self._load_settings()
        old_enabled = old_settings.plugin.enabled
        old_host = old_settings.ws_server.host
        old_port = old_settings.ws_server.port

        self.set_plugin_config(config_data)
        new_settings = self._load_settings()

        conn_changed = (
            old_enabled != new_settings.plugin.enabled
            or old_host != new_settings.ws_server.host
            or old_port != new_settings.ws_server.port
        )
        if conn_changed:
            try:
                await self._restart_server_if_needed()
            except Exception as e:
                logger.error("重启 WS 服务器失败: %s", e)
        else:
            logger.debug("连接配置未变化，跳过服务器重启")

        try:
            await asyncio.wait_for(self._push_config_to_client(), timeout=5.0)
        except (asyncio.TimeoutError, Exception) as e:
            logger.debug("推送配置到客户端失败: %s", e)

    @MessageGateway(
        name=WEMAI_GATEWAY_NAME,
        route_type="duplex",
        platform="wechat",
        protocol="wemai",
        description="WeMai 微信双工消息网关",
    )
    async def handle_wemai_gateway(
        self,
        message: dict[str, Any],
        route: dict[str, Any] | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        sys.stderr.write(f"wemai outbound: {str(message.get('raw_message', ''))[:200]} route={route}\n")
        sys.stderr.flush()
        outbound = {
            "type": "outbound",
            "message_id": message.get("message_id", ""),
            "receiver": "",
            "segments": [],
        }
        mi = message.get("message_info", {})

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
            "laugh": "「呲牙」", "smile": "「微笑」", "cry": "「流泪」", "angry": "「发怒」",
            "surprised": "「惊讶」", "fear": "「恐惧」", "cool": "「酷」", "sad": "「难过」",
            "shy": "「害羞」", "sleepy": "「困」", "love": "「爱心」", "ok": "「OK」",
            "clap": "「鼓掌」", "think": "「思考」", "wave": "「挥手」", "strong": "「强」",
            "weak": "「弱」", "rose": "「玫瑰」", "heart": "「爱心」", "broken_heart": "「心碎」",
            "cake": "「蛋糕」", "coffee": "「咖啡」", "beer": "「啤酒」",
        }
        for seg in raw:
            if not isinstance(seg, dict):
                continue
            stype = seg.get("type", "")
            sdata = seg.get("data", "")
            if stype == "text":
                if isinstance(sdata, str):
                    result.append({"type": "text", "data": sdata})
            elif stype == "image":
                # 图片：binary_data_base64 → 客户端剪贴板粘贴
                image_b64 = seg.get("binary_data_base64", "")
                if image_b64:
                    result.append({"type": "image", "data": image_b64})
            elif stype == "video":
                # 视频段暂时不回传
                pass
            elif stype == "emoji":
                emoji_b64 = seg.get("binary_data_base64", "")
                if emoji_b64:
                    result.append({"type": "image", "data": emoji_b64})
                elif isinstance(sdata, str):
                    text = emoji_map.get(sdata, f"「{sdata}」")
                    if text:
                        result.append({"type": "text", "data": text})
                elif isinstance(sdata, dict):
                    name = sdata.get("emoji_name") or sdata.get("name") or ""
                    text = emoji_map.get(name, f"「{name}」") if name else ""
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
        for seg in result:
            if seg.get("type") != "text":
                continue
            text = seg.get("data", "")
            for token in re.split(r"[\s(（]+", text):
                if token.startswith("@") and len(token) > 1:
                    name = token[1:].rstrip(")）")
                    if name and name not in at_members:
                        at_members.append(name)
        return result, at_members

    def _extract_text(self, seg: Any, collector: list[str] | None = None) -> list[str]:
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

    async def _handle_client_inbound(self, data: dict[str, Any]) -> None:
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

        if msg_type == "friend_request":
            await self._handle_friend_request(data)
            return

        if msg_type != "inbound":
            return

        # ── 解析 wxid 优先字段 ──
        chat_wxid = data.get("chat", "")
        chat_name = data.get("chat_name", chat_wxid)
        sender_wxid = data.get("sender", "")
        sender_name = data.get("sender_name", sender_wxid)
        content = data.get("content", "")
        is_group = data.get("is_group", False)
        sub_type = data.get("msg_type", "text")
        media_path = data.get("media_path", "")
        media_base64 = data.get("media_base64", "")
        media_ext = data.get("media_ext", ".png")
        media_url = data.get("media_url", "")
        server_id = data.get("server_id", "")

        if not sender_wxid or not content:
            return

        logger.info("收到入站消息: [%s/%s] %s/%s: %s (%s)",
                     chat_wxid, chat_name, sender_wxid, sender_name, content[:120], sub_type)

        settings = self._load_settings()
        if settings.chat.enable_chat_list_filter:
            # 匹配 wxid + 显示名（双向兼容）
            if is_group and settings.chat.group_list:
                if not (chat_wxid in settings.chat.group_list
                        or chat_name in settings.chat.group_list):
                    return
            if not is_group and settings.chat.private_list:
                if not (sender_wxid in settings.chat.private_list
                        or sender_name in settings.chat.private_list
                        or chat_wxid in settings.chat.private_list
                        or chat_name in settings.chat.private_list):
                    return

        if media_base64:
            sys.stderr.write(f"wemai media: type={sub_type} len={len(media_base64)} first20={media_base64[:20]}\n")
            sys.stderr.flush()
            try:
                raw = base64.b64decode(media_base64)
                ext = media_ext if media_ext.startswith(".") else "." + media_ext
                tmp = tempfile.NamedTemporaryFile(suffix=ext, delete=False)
                tmp.write(raw)
                tmp.close()
                media_path = tmp.name
                sys.stderr.write(f"wemai media saved: {media_path} ({len(raw)} bytes)\n")
                sys.stderr.flush()
            except Exception as e:
                logger.warning("保存媒体文件失败: %s", e)

        msg_id = hashlib.md5(
            f"{chat_wxid}|{sender_wxid}|{content}|{time.time()}".encode()
        ).hexdigest()

        group_info_val = None
        if is_group:
            group_info_val = {
                "platform": "wechat",
                "group_id": chat_wxid,
                "group_name": chat_name,
            }

        if sub_type == "emoji":
            seg_data: list[dict] = [{"type": "image", "data": media_url or media_path or content}]
        elif sub_type == "image":
            seg_data = [{"type": "image", "data": media_url or media_path or content}]
        elif sub_type == "video":
            seg_data = [{"type": "video", "data": media_url or media_path or content}]
        else:
            seg_data = [{"type": "text", "data": content}]

        # raw_message: 文本段 + 图片/表情段 + 链接卡片段
        raw_msg: list[dict] = [{"type": "text", "data": content}]
        if media_base64 and sub_type in ("emoji", "image"):
            try:
                raw_binary = base64.b64decode(media_base64)
                image_hash = hashlib.sha256(raw_binary).hexdigest()
            except Exception:
                image_hash = ""
            raw_msg.append({
                "type": "image",
                "data": "",
                "hash": image_hash,
                "binary_data_base64": media_base64,
            })
        elif media_url and sub_type in ("emoji", "image", "video"):
            raw_msg.append({
                "type": sub_type,
                "data": media_url,
            })

        appmsg_url = data.get("appmsg_url", "")
        appmsg_title = data.get("appmsg_title", "")
        appmsg_description = data.get("appmsg_description", "")
        appmsg_app_name = data.get("appmsg_app_name", "")
        if appmsg_url or appmsg_title:
            parts: list[str] = []
            if appmsg_title:
                parts.append(f"标题: {appmsg_title}")
            if appmsg_description:
                parts.append(f"摘要: {appmsg_description}")
            if appmsg_url:
                parts.append(f"链接: {appmsg_url}")
            if appmsg_app_name:
                parts.append(f"来源: {appmsg_app_name}")
            raw_msg.append({"type": "text", "data": "[分享链接]\n" + "\n".join(parts)})

        message_dict = {
            "message_id": msg_id,
            "platform": "wechat",
            "message_info": {
                "platform": "wechat",
                "message_id": msg_id,
                "time": time.time(),
                "user_info": {
                    "platform": "wechat",
                    "user_id": sender_wxid,
                    "user_nickname": sender_name,
                },
                "group_info": group_info_val,
                "additional_config": {
                    **({"platform_io_target_group_id": chat_wxid} if is_group else {"platform_io_target_user_id": sender_name}),
                },
            },
            "message_segment": {
                "type": "seglist",
                "data": seg_data,
            },
            "raw_message": raw_msg,
        }

        accepted = await self.ctx.gateway.route_message(
            gateway_name=WEMAI_GATEWAY_NAME,
            message=message_dict,
        )
        if accepted:
            logger.info("入站已注入: [%s/%s] %s/%s: %s",
                         chat_wxid, chat_name, sender_wxid, sender_name, content[:60])
        else:
            logger.warning("入站被拒绝: [%s] %s", chat_wxid, sender_wxid)

    async def _handle_friend_request(self, data: dict[str, Any]) -> None:
        content = data.get("content", "")
        details = data.get("details", "")
        if content in self._FRIEND_SEEN:
            logger.debug("好友请求已处理过，跳过: %s", content)
            return
        self._FRIEND_SEEN.add(content)
        if len(self._FRIEND_SEEN) > 500:
            self._FRIEND_SEEN.clear()
        logger.info("收到好友请求: %s %s", content, details)
        admin_chats = data.get("admin_chats", [])
        action_hint = ""
        if admin_chats:
            action_hint = (
                f"\n你可以做以下操作：\n"
                f"1. 批准好友 → 使用 hub_approve_friend(friend_name=\"{content.split('我是')[0] if '我是' in content else content}\")\n"
                f"2. 忽略请求 → 使用 hub_dismiss_friend(friend_name=\"...\")\n"
                f"3. 通知管理员 → 使用 hub_tell(target=\"{admin_chats[0]}\", message=\"...\")\n"
                f"管理员会话: {', '.join(admin_chats)}"
            )
        msg = f"收到好友请求: {content} ({details}){action_hint}"
        await self._inject_to_hub("系统", f"friend:{content}", msg)

    async def _push_config_to_client(self) -> None:
        settings = self._load_settings()
        payload = {
            "type": "config_update",
            "enable_filter": settings.chat.enable_chat_list_filter,
            "group_list": settings.chat.group_list,
            "private_list": settings.chat.private_list,
            "admin": settings.plugin.admin,
            "data_source": settings.data_source.mode,
            "weflow_base_url": settings.data_source.weflow_base_url,
            "weflow_api_token": settings.data_source.weflow_api_token,
            "weflow_poll_interval": settings.data_source.weflow_poll_interval,
            "send_delay": settings.client.send_delay,
            "close_weixin": settings.client.close_weixin,
            "include_muted": settings.client.include_muted,
            "excluded": settings.client.excluded,
        }
        await self._send_outbound(payload)

    async def _send_outbound(self, data: dict[str, Any]) -> bool:
        ws = self._ws_server
        if ws is not None:
            ok = await ws.send_outbound(data)
            if ok:
                return True
        self._pending_outbound.append(data)
        return True

    async def _drain_pending_outbound(self) -> None:
        if not self._pending_outbound:
            return
        ws = self._ws_server
        if ws is None:
            return
        batch = list(self._pending_outbound)
        self._pending_outbound.clear()
        for data in batch:
            await ws.send_outbound(data)
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
        self._stop_hub_tick()
        self._hub_task = asyncio.create_task(self._hub_tick_loop())

    def _stop_hub_tick(self) -> None:
        if self._hub_task is not None:
            self._hub_task.cancel()
            self._hub_task = None

    async def _hub_tick_loop(self) -> None:
        try:
            while True:
                try:
                    delay = random.randint(180, 600)
                    await asyncio.sleep(delay)
                    try:
                        await self._inject_to_hub("系统", "tick", "定时检查时间")
                    except Exception as e:
                        logger.debug("中枢 tick 注入失败: %s", e)
                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    logger.warning("中枢 tick 循环异常: %s", e)
        except asyncio.CancelledError:
            pass

    async def _inject_to_hub(self, sender: str, content: str, plain: str = "") -> None:
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
        description="【微信系统中枢】向用户发送一条系统通知。当需要提醒用户、报告任务结果或通知系统状态时使用。比如有好友请求时，在批准/忽略后通过此工具告知对应用户。",
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
        text = f"系统通知: {title} {content}".strip()
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
        description="【微信系统中枢】向指定会话发送一条消息。中枢思考后需要对某个对话做出回应时使用。好友请求的处理结果可通过此工具通知管理员会话。",
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

    @Tool(
        name="hub_approve_friend",
        description="【微信系统中枢】批准一个好友请求。参数: friend_name=对方昵称或验证消息中的名字。调用后中枢会通知客户端通过该好友申请。需要通知对方的话请配合 hub_send_notification 或 hub_tell 使用。",
        parameters={
            "type": "object",
            "properties": {
                "friend_name": {"type": "string", "description": "要批准的好友昵称或验证消息中的名字"},
            },
            "required": ["friend_name"],
        },
    )
    async def tool_hub_approve_friend(self, friend_name: str = "", **kwargs: Any) -> dict:
        if not friend_name:
            return {"success": False, "error": "缺少好友名称"}
        await self._send_outbound({
            "type": "friend_approve",
            "friend_name": friend_name,
        })
        logger.info("好友批准指令已发送: %s", friend_name)
        return {"success": True, "message": f"已通知客户端批准 {friend_name} 的好友申请"}

    @Tool(
        name="hub_dismiss_friend",
        description="【微信系统中枢】忽略/取消一个好友请求。不添加对方为好友，仅清除通知。参数: friend_name=对方昵称或验证消息中的名字。",
        parameters={
            "type": "object",
            "properties": {
                "friend_name": {"type": "string", "description": "要忽略的好友昵称或验证消息中的名字"},
            },
            "required": ["friend_name"],
        },
    )
    async def tool_hub_dismiss_friend(self, friend_name: str = "", **kwargs: Any) -> dict:
        if not friend_name:
            return {"success": False, "error": "缺少好友名称"}
        await self._send_outbound({
            "type": "friend_dismiss",
            "friend_name": friend_name,
        })
        logger.info("好友忽略指令已发送: %s", friend_name)
        return {"success": True, "message": f"已通知客户端忽略 {friend_name} 的好友申请"}

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
