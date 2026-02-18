"""
Observer main entry point.
Orchestrates WebSocket connections, state management, and data writing.

Usage:
    python observer.py                          # All assets, all timeframes
    python observer.py --asset btc,eth          # Specific assets
    python observer.py --timeframe 5m           # Specific timeframe
    python observer.py --asset btc --timeframe 5m  # Both filters
"""

import asyncio
import logging
import signal
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

# Local imports
from config import (
    ObserverConfig,
    parse_args,
    GAMMA_BOUNDARY_DELAY_S,
    HEALTH_LOG_INTERVAL_S,
    RESOLUTION_POLL_INTERVAL_S,
    SNAPSHOT_INTERVAL_MS,
)
from clock import get_current_interval_start, is_at_boundary
from connections.chainlink_ws import ChainlinkWS
from connections.binance_ws import BinanceWS
from connections.clob_ws import ClobWS
from connections.gamma_poller import GammaPoller
from connections.binance_backfill import BinanceBackfill
from state.market_state import MarketState, BookLevel
from state.interval_tracker import IntervalTracker
from writers.snapshot_writer import SnapshotWriter
from writers.interval_writer import IntervalWriter
from health import HealthMonitor

# ── Logging setup ──────────────────────────────────────────────────────────

def setup_logging(data_dir: Path):
    """Configure logging to console and file."""
    log_dir = data_dir / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)

    date_str = datetime.now(tz=timezone.utc).strftime("%Y-%m-%d")
    log_file = log_dir / f"observer_{date_str}.log"

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        handlers=[
            logging.StreamHandler(sys.stdout),
            logging.FileHandler(log_file, mode="a"),
        ],
    )


logger = logging.getLogger(__name__)


# ── Observer ───────────────────────────────────────────────────────────────

class Observer:
    """
    Main observer orchestrator.
    Manages connections, state, writing, and lifecycle.
    """

    def __init__(self, config: ObserverConfig):
        self.config = config
        self._running = False

        # ── State ──
        self.market_state = MarketState(config.assets)
        self.interval_tracker = IntervalTracker(config.asset_timeframe_pairs)

        # ── Writers ──
        self.snapshot_writer = SnapshotWriter(
            config.data_dir, config.asset_timeframe_pairs
        )
        self.interval_writer = IntervalWriter(config.data_dir)
        self.health = HealthMonitor(config.data_dir)

        # Register interval callbacks
        self.interval_tracker.on_interval_complete(
            self.interval_writer.write_summary
        )
        self.interval_tracker.on_resolution(
            self.interval_writer.write_resolution
        )

        # ── Connections ──
        self.chainlink_ws = ChainlinkWS(
            symbols=config.chainlink_symbols,
            on_tick=self._on_chainlink_tick,
        )
        self.binance_ws = BinanceWS(
            symbols=config.binance_symbols,
            on_tick=self._on_binance_tick,
        )
        self.clob_ws = ClobWS(
            on_book_update=self._on_book_update,
            on_price_snap=self._on_price_snap,
        )
        self.gamma = GammaPoller(
            assets=config.assets,
            timeframes=config.timeframes,
            on_market_found=self._on_market_found,
        )
        self.backfill = BinanceBackfill()

        # Health callbacks
        self.chainlink_ws.set_health_callback(self.health.log_event)
        self.binance_ws.set_health_callback(self.health.log_event)
        self.clob_ws.set_health_callback(self.health.log_event)

        # ── Backfill tracking ──
        self._binance_last_tick: dict[str, float] = {}  # asset -> last tick time

    # ── Tick handlers ──────────────────────────────────────────────────────

    def _on_chainlink_tick(self, symbol: str, price: float, timestamp_ms: int):
        """Handle incoming Chainlink price tick."""
        self.market_state.update_chainlink(symbol, price, timestamp_ms)

        # Route to interval tracker
        asset = symbol.split("/")[0].lower()
        self.interval_tracker.process_chainlink_tick(asset, price, timestamp_ms)

    def _on_binance_tick(self, symbol: str, price: float, timestamp_ms: int):
        """Handle incoming Binance trade tick."""
        self.market_state.update_binance(symbol, price, timestamp_ms)

        # Route to interval tracker
        asset = symbol.lower().replace("usdt", "")
        self.interval_tracker.process_binance_tick(asset, price)

        # Track for backfill gap detection
        self._binance_last_tick[asset] = time.time()

    def _on_book_update(self, token_id: str, bids: list[dict], asks: list[dict]):
        """Handle incoming order book update from CLOB."""
        bid_levels = [BookLevel(price=b["price"], size=b["size"]) for b in bids]
        ask_levels = [BookLevel(price=a["price"], size=a["size"]) for a in asks]
        self.market_state.update_book(token_id, bid_levels, ask_levels)

    def _on_price_snap(self, token_id: str, price: float):
        """Detect market resolution from token price snapping to 0 or 1."""
        # Find which interval this token belongs to
        for state in self.market_state.all_assets():
            for tf, tid in state.up_token_ids.items():
                if tid == token_id:
                    interval_id = None
                    record = self.interval_tracker.get_active(state.asset, tf)
                    if record and record.up_token_id == token_id:
                        interval_id = record.interval_id
                    else:
                        # Check pending
                        for iid, rec in self.interval_tracker._pending.items():
                            if rec.up_token_id == token_id:
                                interval_id = iid
                                break

                    if interval_id:
                        resolution = "up" if price >= 0.99 else "down"
                        self.interval_tracker.resolve_interval(
                            interval_id, resolution
                        )
                    return

            for tf, tid in state.down_token_ids.items():
                if tid == token_id:
                    interval_id = None
                    record = self.interval_tracker.get_active(state.asset, tf)
                    if record and record.down_token_id == token_id:
                        interval_id = record.interval_id
                    else:
                        for iid, rec in self.interval_tracker._pending.items():
                            if rec.down_token_id == token_id:
                                interval_id = iid
                                break

                    if interval_id:
                        resolution = "down" if price >= 0.99 else "up"
                        self.interval_tracker.resolve_interval(
                            interval_id, resolution
                        )
                    return

    def _on_market_found(self, market_info):
        """Handle new market discovered by Gamma poller."""
        # Update market state with token IDs
        state = self.market_state.get(market_info.asset)
        state.set_token_ids(
            market_info.timeframe,
            market_info.up_token_id,
            market_info.down_token_id,
        )

        # Update interval tracker
        self.interval_tracker.set_token_ids(
            market_info.asset,
            market_info.timeframe,
            market_info.up_token_id,
            market_info.down_token_id,
        )

        # Subscribe CLOB to new tokens (schedule as task since this is sync callback)
        asyncio.get_event_loop().create_task(
            self.clob_ws.subscribe_markets(
                [market_info.up_token_id, market_info.down_token_id]
            )
        )

    # ── Main loops ─────────────────────────────────────────────────────────

    async def _snapshot_loop(self):
        """Main 1-second snapshot loop."""
        logger.info("[main] Snapshot loop started")

        while self._running:
            try:
                now = time.time()

                # Check interval boundaries
                self.interval_tracker.check_boundaries(now)

                # Write snapshots
                self.snapshot_writer.write_snapshot(
                    self.market_state, self.interval_tracker, now
                )

                # Date rotation at midnight UTC
                date_str = datetime.fromtimestamp(now, tz=timezone.utc).strftime(
                    "%Y-%m-%d"
                )
                self.snapshot_writer.rotate_date(date_str)
                self.interval_writer.rotate_date(date_str)

            except Exception as e:
                logger.error(f"[main] Snapshot loop error: {e}", exc_info=True)

            # Sleep until next second boundary
            elapsed = time.time() - now
            sleep_time = max(0, (SNAPSHOT_INTERVAL_MS / 1000) - elapsed)
            await asyncio.sleep(sleep_time)

    async def _resolution_loop(self):
        """Periodically check pending intervals for resolution via Gamma."""
        logger.info("[main] Resolution loop started")

        while self._running:
            await asyncio.sleep(RESOLUTION_POLL_INTERVAL_S)

            try:
                # Clean up stale pending intervals
                self.interval_tracker.cleanup_stale_pending()

                # Check pending intervals via Gamma API
                pending = self.interval_tracker.get_pending_intervals()
                for record in pending:
                    slug = record.interval_id.replace(
                        f"{record.asset}-updown-{record.timeframe}-",
                        f"{record.asset}-updown-{record.timeframe}-",
                    )
                    # Try to check resolution
                    resolution = await self.gamma.check_resolution(slug)
                    if resolution:
                        self.interval_tracker.resolve_interval(
                            record.interval_id, resolution
                        )

            except Exception as e:
                logger.error(f"[main] Resolution loop error: {e}", exc_info=True)

    async def _health_loop(self):
        """Periodic health status logging."""
        logger.info("[main] Health loop started")

        while self._running:
            await asyncio.sleep(HEALTH_LOG_INTERVAL_S)

            try:
                # Log connection stats
                for conn in [self.chainlink_ws, self.binance_ws, self.clob_ws]:
                    stats = conn.stats
                    logger.info(
                        f"[health] {stats['name']}: "
                        f"connected={stats['connected']}, "
                        f"msgs={stats['total_messages']}, "
                        f"reconnects={stats['reconnect_count']}, "
                        f"last_msg_age={stats['last_message_age_ms']}ms"
                    )

                # Log interval tracker status
                active = self.interval_tracker.get_active_intervals()
                pending = self.interval_tracker.get_pending_intervals()
                logger.info(
                    f"[health] Intervals: {len(active)} active, "
                    f"{len(pending)} pending resolution"
                )

                # Log CLOB subscription count
                logger.info(
                    f"[health] CLOB subscriptions: "
                    f"{self.clob_ws.get_subscribed_count()} tokens"
                )

            except Exception as e:
                logger.error(f"[main] Health loop error: {e}", exc_info=True)

    async def _cold_start_vol(self):
        """Fetch initial volatility estimates via Binance klines."""
        logger.info("[main] Cold-start volatility estimation...")

        for asset, timeframe in self.config.asset_timeframe_pairs:
            binance_symbol = self.config.get_binance_symbol(asset)
            interval = timeframe  # "5m" or "15m" — matches Binance intervals

            deltas = await self.backfill.fetch_cold_start_vol(
                symbol=binance_symbol,
                interval=interval,
                lookback=20,
            )

            if deltas:
                # Seed the delta history in interval tracker
                key = (asset, timeframe)
                for d in deltas:
                    self.interval_tracker._delta_history[key].append(d)

                vol = self.interval_tracker._compute_vol(asset, timeframe)
                logger.info(
                    f"[main] Cold-start vol for {asset}/{timeframe}: "
                    f"{vol:.6f} ({len(deltas)} intervals)"
                )

    # ── Lifecycle ──────────────────────────────────────────────────────────

    async def start(self):
        """Start all components and run until stopped."""
        self._running = True

        logger.info("=" * 60)
        logger.info("Polymarket Observer starting")
        logger.info(f"  Assets: {self.config.assets}")
        logger.info(f"  Timeframes: {self.config.timeframes}")
        logger.info(f"  Pairs: {self.config.asset_timeframe_pairs}")
        logger.info(f"  Data dir: {self.config.data_dir}")
        logger.info("=" * 60)

        # Initialize backfill client
        await self.backfill.start()

        # Cold-start volatility
        await self._cold_start_vol()

        # Start all tasks
        tasks = [
            asyncio.create_task(self.chainlink_ws.run(), name="chainlink_ws"),
            asyncio.create_task(self.binance_ws.run(), name="binance_ws"),
            asyncio.create_task(self.clob_ws.run(), name="clob_ws"),
            asyncio.create_task(self.gamma.start(), name="gamma_poller"),
            asyncio.create_task(self._snapshot_loop(), name="snapshot_loop"),
            asyncio.create_task(self._resolution_loop(), name="resolution_loop"),
            asyncio.create_task(self._health_loop(), name="health_loop"),
        ]

        # Wait for any task to complete (which means it crashed)
        try:
            done, pending = await asyncio.wait(
                tasks, return_when=asyncio.FIRST_EXCEPTION
            )

            for task in done:
                if task.exception():
                    logger.error(
                        f"Task {task.get_name()} failed: {task.exception()}",
                        exc_info=task.exception(),
                    )

        except asyncio.CancelledError:
            logger.info("Observer cancelled")

        finally:
            await self.stop()

    async def stop(self):
        """Graceful shutdown of all components."""
        logger.info("Observer shutting down...")
        self._running = False

        # Stop connections
        await self.chainlink_ws.stop()
        await self.binance_ws.stop()
        await self.clob_ws.stop()
        await self.gamma.stop()
        await self.backfill.stop()

        # Flush and close writers
        self.snapshot_writer.close()
        self.interval_writer.close()
        self.health.close()

        logger.info("Observer stopped")


# ── Entry point ────────────────────────────────────────────────────────────

async def main():
    config = parse_args()
    setup_logging(config.data_dir)

    observer = Observer(config)

    # Handle signals for graceful shutdown
    loop = asyncio.get_running_loop()

    def handle_signal():
        logger.info("Signal received — shutting down")
        asyncio.create_task(observer.stop())

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, handle_signal)
        except NotImplementedError:
            # Windows doesn't support add_signal_handler
            pass

    await observer.start()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
