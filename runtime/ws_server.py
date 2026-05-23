from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Callable, Optional

logger = logging.getLogger("wemai_adapter.ws_server")


class WsClient:
    def __init__(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self.reader = reader
        self.writer = writer
        self.addr = writer.get_extra_info("peername", ("unknown", 0))
        self._connected = True

    @property
    def connected(self) -> bool:
        return self._connected

    async def send_json(self, data: dict[str, Any]) -> None:
        if not self._connected:
            return
        try:
            raw = json.dumps(data, ensure_ascii=False)
            payload = raw.encode("utf-8")
            self.writer.write(len(payload).to_bytes(4, "big"))
            self.writer.write(payload)
            await self.writer.drain()
        except Exception as e:
            logger.warning("发送到客户端 %s 失败: %s", self.addr, e)
            self._connected = False

    async def recv_json(self) -> Optional[dict[str, Any]]:
        try:
            raw_len = await self.reader.readexactly(4)
            length = int.from_bytes(raw_len, "big")
            payload = await self.reader.readexactly(length)
            return json.loads(payload.decode("utf-8"))
        except (asyncio.IncompleteReadError, ConnectionError, json.JSONDecodeError) as e:
            logger.info("客户端 %s 断开: %s", self.addr, e)
            self._connected = False
            return None

    def close(self) -> None:
        self._connected = False
        try:
            self.writer.close()
        except Exception:
            pass


class WemaiWsServer:
    def __init__(self, host: str, port: int) -> None:
        self._host = host
        self._port = port
        self._server: Optional[asyncio.Server] = None
        self._client: Optional[WsClient] = None
        self._on_inbound: Optional[Callable[[dict[str, Any]], None]] = None

    def set_inbound_handler(self, handler: Callable[[dict[str, Any]], None]) -> None:
        self._on_inbound = handler

    async def start(self) -> None:
        self._server = await asyncio.start_server(
            self._on_connect,
            host=self._host,
            port=self._port,
        )
        addr = self._server.sockets[0].getsockname()
        logger.info("WebSocket 服务器已启动: %s:%s", addr[0], addr[1])

    async def stop(self) -> None:
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
        if self._client is None or not self._client.connected:
            logger.warning("客户端未连接，丢弃出站消息")
            return False
        await self._client.send_json(data)
        return True

    async def _on_connect(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        client = WsClient(reader, writer)
        if self._client is not None and self._client.connected:
            logger.warning("已有客户端连接，拒绝新连接: %s", client.addr)
            client.close()
            return
        self._client = client
        logger.info("客户端已连接: %s", client.addr)
        try:
            while client.connected:
                msg = await client.recv_json()
                if msg is None:
                    break
                if self._on_inbound is not None:
                    try:
                        result = await self._on_inbound(msg)
                        await client.send_json({
                            "type": "ack",
                            "original_type": msg.get("type", ""),
                            "success": result is True or result is None,
                        })
                    except Exception as e:
                        logger.error("处理入站消息异常: %s", e)
                        await client.send_json({
                            "type": "ack",
                            "original_type": msg.get("type", ""),
                            "success": False,
                            "error": str(e),
                        })
        finally:
            logger.info("客户端已断开: %s", client.addr)
            self._client = None
