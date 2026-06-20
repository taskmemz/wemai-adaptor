from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Callable

logger = logging.getLogger("wemai_adapter.ws_server")

MAX_MESSAGE_SIZE = 10 * 1024 * 1024
READ_TIMEOUT = 120.0  # 读取超时（秒）


class WsClient:
    def __init__(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter,
        read_timeout: float = READ_TIMEOUT,
    ) -> None:
        self.reader = reader
        self.writer = writer
        self.addr = writer.get_extra_info("peername", ("unknown", 0))
        self._read_timeout = read_timeout
        self._connected = True

    @property
    def connected(self) -> bool:
        return self._connected

    async def send_json(self, data: dict[str, Any], tag: str = "") -> None:
        if not self._connected:
            return
        try:
            raw = json.dumps(data, ensure_ascii=False)
            payload = raw.encode("utf-8")
            msg_type = data.get("type", "unknown")
            size_kb = len(payload) / 1024
            self.writer.write(len(payload).to_bytes(4, "big"))
            self.writer.write(payload)
            await self.writer.drain()
            if size_kb > 100:
                logger.info("已发送 %s 大消息 %.1f KB → %s", tag or msg_type, size_kb, self.addr)
        except Exception as e:
            logger.warning("发送 %s 到 %s 失败: %s", tag or data.get("type", "?"), self.addr, e)
            self._connected = False

    async def _read_exact(self, n: int) -> bytes:
        """带超时的精确读取"""
        try:
            return await asyncio.wait_for(
                self.reader.readexactly(n), timeout=self._read_timeout,
            )
        except asyncio.TimeoutError:
            logger.warning("⚠️ 读取 %d 字节超时（已等待 %.1fs），客户端疑似静默断开", n, self._read_timeout)
            raise ConnectionError(f"read {n} bytes timeout after {self._read_timeout}s")

    async def recv_json(self) -> dict[str, Any] | None:
        try:
            raw_len = await self._read_exact(4)
            length = int.from_bytes(raw_len, "big")
            if length < 0 or length > MAX_MESSAGE_SIZE:
                logger.warning("拒绝超大消息: %s bytes", length)
                self._connected = False
                return None
            payload = await self._read_exact(length)
            return json.loads(payload.decode("utf-8"))
        except asyncio.IncompleteReadError as e:
            logger.info("🔌 客户端 %s 断开: received %d bytes, expected %d",
                        self.addr, len(e.partial) if hasattr(e, 'partial') else 0,
                        e.expected if hasattr(e, 'expected') else '?')
            self._connected = False
            return None
        except (ConnectionError, OSError, asyncio.TimeoutError) as e:
            logger.info("⚠️ 客户端 %s 连接异常: %s: %s", self.addr, type(e).__name__, e)
            self._connected = False
            return None
        except json.JSONDecodeError as e:
            logger.warning("📦 客户端 %s 消息格式异常: %s", self.addr, e)
            self._connected = False
            return None

    def close(self) -> None:
        self._connected = False
        try:
            self.writer.close()
        except Exception:
            pass


HB_INTERVAL = 30.0  # 心跳间隔（秒）


class WemaiWsServer:
    def __init__(self, host: str, port: int, hb_interval: float = HB_INTERVAL) -> None:
        self._host = host
        self._port = port
        self._hb_interval = hb_interval
        self._server: asyncio.Server | None = None
        self._client: WsClient | None = None
        self._client_lock = asyncio.Lock()
        self._on_inbound: Callable[[dict[str, Any]], None] | None = None
        self._pending_outbound: list[dict[str, Any]] = []
        self._heartbeat_task: asyncio.Task | None = None

    def set_inbound_handler(self, handler: Callable[[dict[str, Any]], None]) -> None:
        self._on_inbound = handler

    async def start(self) -> None:
        self._server = await asyncio.start_server(
            self._on_connect,
            host=self._host,
            port=self._port,
        )
        addr = self._server.sockets[0].getsockname()
        self._start_heartbeat()
        logger.info("WebSocket 服务器已启动: %s:%s（心跳间隔 %.0fs）", addr[0], addr[1], self._hb_interval)

    async def stop(self) -> None:
        self._stop_heartbeat()
        self._pending_outbound.clear()
        async with self._client_lock:
            if self._client is not None:
                self._client.close()
                self._client = None
        if self._server is not None:
            self._server.close()
            try:
                await asyncio.wait_for(self._server.wait_closed(), timeout=3.0)
            except asyncio.TimeoutError:
                pass
            self._server = None
        logger.info("WebSocket 服务器已停止")

    async def send_outbound(self, data: dict[str, Any]) -> bool:
        async with self._client_lock:
            client = self._client
        if client is None or not client.connected:
            logger.info("客户端未连接，消息已排队等待发送")
            self._pending_outbound.append(data)
            return True
        await client.send_json(data, tag="outbound")
        return True

    async def _drain_pending(self) -> None:
        if not self._pending_outbound:
            return
        async with self._client_lock:
            client = self._client
        if client is None or not client.connected:
            logger.info("有 %d 条排队消息，但客户端未连接，暂不发送", len(self._pending_outbound))
            return
        batch = list(self._pending_outbound)
        self._pending_outbound.clear()
        for data in batch:
            await client.send_json(data, tag="queued")
        logger.info("已发送 %d 条排队消息", len(batch))

    # ─── 心跳 ──────────────────────────────────────────────

    def _start_heartbeat(self) -> None:
        self._stop_heartbeat()
        self._heartbeat_task = asyncio.create_task(self._heartbeat_loop())

    def _stop_heartbeat(self) -> None:
        if self._heartbeat_task is not None:
            self._heartbeat_task.cancel()
            self._heartbeat_task = None

    async def _heartbeat_loop(self) -> None:
        """定期发送心跳 ping，检测客户端存活并保活 NAT 映射"""
        try:
            while True:
                await asyncio.sleep(self._hb_interval)
                async with self._client_lock:
                    client = self._client
                if client is None or not client.connected:
                    continue
                await client.send_json({"type": "ping"}, tag="heartbeat")
        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.warning("心跳循环异常: %s", e)

    # ─── 连接管理 ──────────────────────────────────────────

    async def _on_connect(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        client = WsClient(reader, writer)
        client_addr = client.addr

        async with self._client_lock:
            old = self._client
            if old is not None and old.connected:
                logger.warning("已有客户端 %s 连接，拒绝新客户端 %s", old.addr, client_addr)
                client.close()
                return
            self._client = client

        logger.info("客户端已连接: %s", client_addr)
        await self._drain_pending()
        try:
            while client.connected:
                msg = await client.recv_json()
                if msg is None:
                    break

                # 心跳响应不触发业务处理
                if msg.get("type") == "pong":
                    continue

                if self._on_inbound is not None:
                    try:
                        result = await self._on_inbound(msg)
                        ack_sent = False
                        try:
                            await client.send_json({
                                "type": "ack",
                                "original_type": msg.get("type", ""),
                                "success": result is True or result is None,
                            }, tag="ack")
                            ack_sent = True
                        except Exception:
                            pass
                        if not ack_sent:
                            logger.warning("无法发送 ACK 给 %s（客户端可能已断开）", client_addr)
                    except Exception as e:
                        logger.error("处理入站消息异常: %s", e)
                        try:
                            await client.send_json({
                                "type": "ack",
                                "original_type": msg.get("type", ""),
                                "success": False,
                                "error": str(e),
                            }, tag="ack_error")
                        except Exception:
                            pass
        finally:
            logger.info("客户端已断开: %s", client_addr)
            async with self._client_lock:
                if self._client is client:
                    self._client = None
