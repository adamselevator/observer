"""
Chainlink RTDS WebSocket connection.
Subscribes to crypto_prices_chainlink topic for all tracked assets.
"""

import json
import logging

from connections.base_ws import BaseWebSocket
from config import CHAINLINK_RTDS_URL, CHAINLINK_STALENESS_TIMEOUT_S

logger = logging.getLogger(__name__)


class ChainlinkWS(BaseWebSocket):
    """Connects to Polymarket RTDS for Chainlink price data."""

    def __init__(self, symbols: list[str], on_tick=None):
        """
        Args:
            symbols: Chainlink symbols like ["btc/usd", "eth/usd"]
            on_tick: callback(symbol, price, timestamp_ms)
        """
        super().__init__(
            url=CHAINLINK_RTDS_URL,
            name="chainlink_rtds",
            staleness_timeout_s=CHAINLINK_STALENESS_TIMEOUT_S,
        )
        self._symbols = symbols
        self._on_tick = on_tick

    async def _on_connected(self, ws):
        """Subscribe to Chainlink crypto prices."""
        sub_msg = {
            "action": "subscribe",
            "subscriptions": [
                {
                    "topic": "crypto_prices_chainlink",
                    "type": "*",
                    "filters": "",
                }
            ],
        }
        await ws.send(json.dumps(sub_msg))
        logger.info(f"[{self.name}] Subscribed to crypto_prices_chainlink")

    async def _on_message(self, raw: str):
        """Parse Chainlink price update and route to callback."""
        if not raw or not raw.strip():
            return

        try:
            msg = json.loads(raw)
        except json.JSONDecodeError:
            logger.warning(f"[{self.name}] Invalid JSON: {raw[:100]}")
            return

        topic = msg.get("topic")
        if topic != "crypto_prices_chainlink":
            return

        payload = msg.get("payload")
        if not payload:
            return

        symbol = payload.get("symbol", "").lower()
        price = payload.get("value")
        timestamp_ms = payload.get("timestamp")

        if symbol not in self._symbols:
            return

        if price is None or timestamp_ms is None:
            return

        if self._on_tick:
            try:
                self._on_tick(symbol, float(price), int(timestamp_ms))
            except Exception as e:
                logger.error(f"[{self.name}] Tick callback error: {e}")
