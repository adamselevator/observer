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

    def __init__(
        self,
        on_book_update=None,
        on_level_update=None,
        on_levels_batch=None,
        on_price_snap=None,
    ):
        """
        Args:
            on_book_update: callback(token_id, bids, asks)
                bids/asks are lists of {"price": float, "size": float}
                Called on full book snapshots.
            on_level_update: callback(token_id, side, price, size)
                Called on individual level changes (price_change events).
                side is "BUY" or "SELL", size 0 means remove.
                Only used if on_levels_batch is not set.
            on_levels_batch: callback(token_id, changes)
                changes is a list of (side, price, size) tuples.
                Called once per asset_id with all changes from one message
                batched together. Preferred over on_level_update for
                atomic timestamp handling.
            on_price_snap: callback(token_id, price)
                Called when a token snaps to 0 or 1 (resolution detection)
        """
        super().__init__(
            url=CLOB_WS_URL,
            name="clob_ws",
            staleness_timeout_s=CLOB_STALENESS_TIMEOUT_S,
        )
        self._on_book_update = on_book_update
        self._on_level_update = on_level_update
        self._on_levels_batch = on_levels_batch
        self._on_price_snap = on_price_snap
        self._subscribed_markets: set[str] = set()  # condition IDs / asset IDs
        self._pending_subscriptions: list[str] = []

    async def _on_connected(self, ws):
        """Resubscribe to all known markets on (re)connect."""
        if self._subscribed_markets:
            await self._send_subscription(list(self._subscribed_markets))

    async def _on_message(self, raw: str):
        """Parse CLOB message (book snapshot, price change, or trade)."""
        try:
            msg = json.loads(raw)
        except json.JSONDecodeError:
            return

        if not isinstance(msg, dict):
            return

        event_type = msg.get("event_type")

        if event_type == "book":
            self._handle_book(msg)
        elif event_type == "price_change" or "price_changes" in msg:
            self._handle_price_changes(msg)
        elif event_type == "last_trade_price":
            pass
        elif event_type == "tick_size_change":
            pass

    def _handle_book(self, msg: dict):
        """Process a full book snapshot."""
        asset_id = msg.get("asset_id", "")
        bids_raw = msg.get("bids", [])
        asks_raw = msg.get("asks", msg.get("sells", []))

        bids = [
            {"price": float(b["price"]), "size": float(b["size"])}
            for b in bids_raw
        ]
        asks = [
            {"price": float(a["price"]), "size": float(a["size"])}
            for a in asks_raw
        ]

        # Sort: bids descending (best/highest first), asks ascending (best/lowest first)
        bids.sort(key=lambda b: b["price"], reverse=True)
        asks.sort(key=lambda a: a["price"])

        if self._on_book_update:
            try:
                self._on_book_update(asset_id, bids, asks)
            except Exception as e:
                logger.error(f"[{self.name}] Book callback error: {e}")

        # Resolution detection from book snapshot
        if bids and self._on_price_snap:
            best_bid = bids[0]["price"]  # highest bid after sorting
            if best_bid >= 0.95 or best_bid < 0.01:
                try:
                    self._on_price_snap(asset_id, best_bid)
                except Exception as e:
                    logger.error(f"[{self.name}] Price snap callback error: {e}")

    def _handle_price_changes(self, msg: dict):
        """Process incremental level changes (price_changes array).

        Groups all changes by asset_id and applies them as a batch so that
        bid and ask side timestamps are set atomically when both sides
        arrive in the same message.
        """
        raw_changes = msg.get("price_changes", [])

        # Group parsed changes by asset_id
        batches: dict[str, list[tuple[str, float, float]]] = {}
        for change in raw_changes:
            asset_id = change.get("asset_id", "")
            side = change.get("side", "").upper()
            price_str = change.get("price", "")
            size_str = change.get("size", "0")

            if not (asset_id and side and price_str):
                continue

            try:
                price = float(price_str)
                size = float(size_str)
            except (ValueError, TypeError):
                continue

            batches.setdefault(asset_id, []).append((side, price, size))

        # Dispatch each asset's changes as a batch
        for asset_id, changes in batches.items():
            if self._on_levels_batch:
                try:
                    self._on_levels_batch(asset_id, changes)
                except Exception as e:
                    logger.error(f"[{self.name}] Levels batch callback error: {e}")
            elif self._on_level_update:
                for side, price, size in changes:
                    try:
                        self._on_level_update(asset_id, side, price, size)
                    except Exception as e:
                        logger.error(f"[{self.name}] Level update callback error: {e}")

            # Resolution detection from level updates
            if self._on_price_snap:
                for side, price, size in changes:
                    if side == "BUY" and (price >= 0.95 or price < 0.01):
                        try:
                            self._on_price_snap(asset_id, price)
                        except Exception as e:
                            logger.error(
                                f"[{self.name}] Price snap callback error: {e}"
                            )

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
