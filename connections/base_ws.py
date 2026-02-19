"""
Base WebSocket connection with auto-reconnect and staleness detection.
All specific connections inherit from this.
"""

import asyncio
import logging
import time
from abc import ABC, abstractmethod

from config import WS_RECONNECT_BASE_S, WS_RECONNECT_MAX_S, SSL_CONTEXT

try:
    import websockets
    from websockets import State as WsState
    from websockets.asyncio.client import connect as ws_connect
except ImportError:
    websockets = None
    WsState = None
    ws_connect = None

logger = logging.getLogger(__name__)


class BaseWebSocket(ABC):
    """
    Abstract base for managed WebSocket connections.
    Handles: connect, reconnect with backoff, staleness watchdog, health tracking.
    """

    def __init__(self, url: str, name: str, staleness_timeout_s: float = 5.0):
        self.url = url
        self.name = name
        self.staleness_timeout_s = staleness_timeout_s

        self._ws = None
        self._running = False
        self._last_message_at: float = 0
        self._connect_count: int = 0
        self._reconnect_count: int = 0
        self._total_messages: int = 0

        # Health callback
        self._health_callback = None

    def set_health_callback(self, callback):
        """callback(name, event, details_dict)"""
        self._health_callback = callback

    def _emit_health(self, event: str, details: dict | None = None):
        if self._health_callback:
            try:
                self._health_callback(self.name, event, details or {})
            except Exception:
                pass

    @property
    def is_connected(self) -> bool:
        return self._ws is not None and self._ws.state is WsState.OPEN

    @property
    def last_message_age_ms(self) -> int:
        if self._last_message_at == 0:
            return -1
        return int((time.time() - self._last_message_at) * 1000)

    @property
    def stats(self) -> dict:
        return {
            "name": self.name,
            "connected": self.is_connected,
            "connect_count": self._connect_count,
            "reconnect_count": self._reconnect_count,
            "total_messages": self._total_messages,
            "last_message_age_ms": self.last_message_age_ms,
        }

    async def run(self):
        """Main loop: connect, receive, reconnect on failure."""
        self._running = True
        backoff = WS_RECONNECT_BASE_S

        while self._running:
            try:
                logger.info(f"[{self.name}] Connecting to {self.url}")
                self._emit_health("connecting", {"url": self.url})

                async with ws_connect(self.url, ping_interval=20, ping_timeout=10, ssl=SSL_CONTEXT) as ws:
                    self._ws = ws
                    self._connect_count += 1
                    backoff = WS_RECONNECT_BASE_S
                    logger.info(f"[{self.name}] Connected")
                    self._emit_health("connected")

                    await self._on_connected(ws)

                    # Receive loop with staleness watchdog
                    while self._running:
                        try:
                            msg = await asyncio.wait_for(
                                ws.recv(), timeout=self.staleness_timeout_s
                            )
                            self._last_message_at = time.time()
                            self._total_messages += 1
                            await self._on_message(msg)
                        except asyncio.TimeoutError:
                            # No message within staleness window
                            logger.warning(
                                f"[{self.name}] Stale — no message in "
                                f"{self.staleness_timeout_s}s"
                            )
                            self._emit_health(
                                "stale",
                                {"timeout_s": self.staleness_timeout_s},
                            )
                            break  # Force reconnect
                        except websockets.ConnectionClosed as e:
                            logger.warning(
                                f"[{self.name}] Connection closed: {e.code} {e.reason}"
                            )
                            self._emit_health(
                                "disconnected",
                                {"code": e.code, "reason": str(e.reason)},
                            )
                            break

            except (OSError, ConnectionRefusedError, Exception) as e:
                logger.error(f"[{self.name}] Connection error: {e}")
                self._emit_health("error", {"error": str(e)})

            self._ws = None

            if self._running:
                self._reconnect_count += 1
                logger.info(f"[{self.name}] Reconnecting in {backoff:.1f}s")
                self._emit_health("reconnecting", {"backoff_s": backoff})
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, WS_RECONNECT_MAX_S)

    async def stop(self):
        """Graceful shutdown."""
        self._running = False
        if self._ws:
            await self._ws.close()
            self._ws = None
        logger.info(f"[{self.name}] Stopped")

    @abstractmethod
    async def _on_connected(self, ws):
        """Called after connection is established. Send subscriptions here."""
        ...

    @abstractmethod
    async def _on_message(self, raw: str):
        """Called for each received message. Parse and route here."""
        ...
