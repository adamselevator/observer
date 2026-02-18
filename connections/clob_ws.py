"""
CLOB WebSocket connection.
Subscribes to order book updates for active market tokens.
Maintains subscription list as new markets are discovered.
"""

import json
import logging

from connections.base_ws import BaseWebSocket
from config import CLOB_WS_URL, CLOB_STALENESS_TIMEOUT_S

logger = logging.getLogger(__name__)


class ClobWS(BaseWebSocket):
    """Connects to Polymarket CLOB WebSocket for order book data."""

    def __init__(self, on_book_update=None, on_price_snap=None):
        """
        Args:
            on_book_update: callback(token_id, bids, asks)
                bids/asks are lists of {"price": str, "size": str}
            on_price_snap: callback(token_id, price)
                Called when a token snaps to 0 or 1 (resolution detection)
        """
        super().__init__(
            url=CLOB_WS_URL,
            name="clob_ws",
            staleness_timeout_s=CLOB_STALENESS_TIMEOUT_S,
        )
        self._on_book_update = on_book_update
        self._on_price_snap = on_price_snap
        self._subscribed_markets: set[str] = set()  # condition IDs / asset IDs
        self._pending_subscriptions: list[str] = []

    async def _on_connected(self, ws):
        """Resubscribe to all known markets on (re)connect."""
        if self._subscribed_markets:
            await self._send_subscription(list(self._subscribed_markets))

    async def _on_message(self, raw: str):
        """Parse CLOB book update."""
        try:
            msg = json.loads(raw)
        except json.JSONDecodeError:
            return

        event_type = msg.get("event_type")

        if event_type == "book":
            asset_id = msg.get("asset_id", "")
            bids_raw = msg.get("bids", [])
            asks_raw = msg.get("sells", msg.get("asks", []))

            bids = [
                {"price": float(b["price"]), "size": float(b["size"])}
                for b in bids_raw
            ]
            asks = [
                {"price": float(a["price"]), "size": float(a["size"])}
                for a in asks_raw
            ]

            if self._on_book_update:
                try:
                    self._on_book_update(asset_id, bids, asks)
                except Exception as e:
                    logger.error(f"[{self.name}] Book callback error: {e}")

            # Resolution detection: if best bid snaps to >= 0.99 or <= 0.01
            if bids and self._on_price_snap:
                best_bid = float(bids[0]["price"]) if bids else 0.5
                if best_bid >= 0.99 or best_bid <= 0.01:
                    try:
                        self._on_price_snap(asset_id, best_bid)
                    except Exception as e:
                        logger.error(f"[{self.name}] Price snap callback error: {e}")

        elif event_type == "tick_size_change":
            pass  # Ignore
        elif event_type == "last_trade_price":
            # Could track last trade price too
            pass

    async def subscribe_markets(self, asset_ids: list[str]):
        """Subscribe to book updates for new market tokens."""
        new_ids = [aid for aid in asset_ids if aid and aid not in self._subscribed_markets]
        if not new_ids:
            return

        self._subscribed_markets.update(new_ids)

        if self.is_connected:
            await self._send_subscription(new_ids)
        else:
            self._pending_subscriptions.extend(new_ids)

    async def _send_subscription(self, asset_ids: list[str]):
        """Send subscription message for given asset IDs."""
        if not self._ws or not asset_ids:
            return

        sub_msg = {
            "assets_ids": asset_ids,  # Note: Polymarket uses assets_ids (plural)
            "type": "market",
        }
        try:
            await self._ws.send(json.dumps(sub_msg))
            logger.info(
                f"[{self.name}] Subscribed to {len(asset_ids)} tokens"
            )
        except Exception as e:
            logger.error(f"[{self.name}] Subscription send error: {e}")

    def get_subscribed_count(self) -> int:
        return len(self._subscribed_markets)
