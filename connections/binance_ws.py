"""
Binance WebSocket connection.
Subscribes to @trade streams for all tracked assets via combined stream.
"""

import json
import logging

from connections.base_ws import BaseWebSocket
from config import BINANCE_WS_BASE, BINANCE_STALENESS_TIMEOUT_S

logger = logging.getLogger(__name__)


class BinanceWS(BaseWebSocket):
    """Connects to Binance combined trade stream for multiple assets."""

    def __init__(self, symbols: list[str], on_tick=None):
        """
        Args:
            symbols: Binance symbols like ["btcusdt", "ethusdt"]
            on_tick: callback(symbol, price, timestamp_ms)
        """
        streams = "/".join(f"{s}@trade" for s in symbols)
        url = f"{BINANCE_WS_BASE}/stream?streams={streams}"

        super().__init__(
            url=url,
            name="binance_ws",
            staleness_timeout_s=BINANCE_STALENESS_TIMEOUT_S,
        )
        self._symbols = set(symbols)
        self._on_tick = on_tick

    async def _on_connected(self, ws):
        """Binance combined streams auto-subscribe via URL. Nothing to send."""
        logger.info(
            f"[{self.name}] Connected to combined stream: {list(self._symbols)}"
        )

    async def _on_message(self, raw: str):
        """Parse Binance trade message and route to callback."""
        try:
            msg = json.loads(raw)
        except json.JSONDecodeError:
            logger.warning(f"[{self.name}] Invalid JSON: {raw[:100]}")
            return

        # Combined stream wraps data in {"stream": "...", "data": {...}}
        data = msg.get("data", msg)

        event_type = data.get("e")
        if event_type != "trade":
            return

        symbol = data.get("s", "").lower()
        price = data.get("p")  # trade price as string
        timestamp_ms = data.get("T")  # trade time in ms

        if symbol not in self._symbols:
            return

        if price is None or timestamp_ms is None:
            return

        if self._on_tick:
            try:
                self._on_tick(symbol, float(price), int(timestamp_ms))
            except Exception as e:
                logger.error(f"[{self.name}] Tick callback error: {e}")
