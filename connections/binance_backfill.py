"""
Binance REST backfill.
Fetches historical trade data to fill gaps when WebSocket connections drop.
"""

import asyncio
import logging

try:
    import aiohttp
except ImportError:
    aiohttp = None

from config import BINANCE_REST_URL, BACKFILL_MAX_GAP_S, SSL_CONTEXT

logger = logging.getLogger(__name__)


class BinanceBackfill:
    """Fetches historical Binance data to fill gaps."""

    def __init__(self):
        self._session: aiohttp.ClientSession | None = None

    async def start(self):
        self._session = aiohttp.ClientSession(
            connector=aiohttp.TCPConnector(ssl=SSL_CONTEXT)
        )

    async def stop(self):
        if self._session:
            await self._session.close()
            self._session = None

    async def fetch_klines(
        self,
        symbol: str,
        interval: str,
        start_time_ms: int,
        end_time_ms: int,
    ) -> list[dict]:
        """
        Fetch klines (candlestick data) from Binance REST API.

        Args:
            symbol: e.g., "BTCUSDT"
            interval: e.g., "1s", "1m"
            start_time_ms: Start time in milliseconds
            end_time_ms: End time in milliseconds

        Returns:
            List of {"timestamp_ms": int, "open": float, "close": float, ...}
        """
        if not self._session:
            return []

        gap_s = (end_time_ms - start_time_ms) / 1000
        if gap_s > BACKFILL_MAX_GAP_S:
            logger.warning(
                f"[backfill] Gap of {gap_s:.0f}s exceeds max {BACKFILL_MAX_GAP_S}s — skipping"
            )
            return []

        try:
            url = f"{BINANCE_REST_URL}/api/v3/klines"
            params = {
                "symbol": symbol.upper(),
                "interval": interval,
                "startTime": start_time_ms,
                "endTime": end_time_ms,
                "limit": 1000,
            }

            async with self._session.get(url, params=params, timeout=aiohttp.ClientTimeout(total=15)) as resp:
                if resp.status != 200:
                    logger.warning(f"[backfill] HTTP {resp.status} for {symbol}")
                    return []

                raw = await resp.json()

            results = []
            for k in raw:
                results.append({
                    "timestamp_ms": int(k[0]),
                    "open": float(k[1]),
                    "high": float(k[2]),
                    "low": float(k[3]),
                    "close": float(k[4]),
                    "volume": float(k[5]),
                    "close_time_ms": int(k[6]),
                })

            logger.info(
                f"[backfill] Fetched {len(results)} klines for {symbol} "
                f"({gap_s:.0f}s gap)"
            )
            return results

        except asyncio.TimeoutError:
            logger.warning(f"[backfill] Timeout fetching {symbol}")
            return []
        except Exception as e:
            logger.error(f"[backfill] Error fetching {symbol}: {e}")
            return []

    async def fetch_1s_prices(
        self,
        symbol: str,
        start_time_ms: int,
        end_time_ms: int,
    ) -> list[tuple[int, float]]:
        """
        Fetch 1-second resolution prices for backfill.
        Returns list of (timestamp_ms, price) tuples.
        """
        klines = await self.fetch_klines(
            symbol=symbol,
            interval="1s",
            start_time_ms=start_time_ms,
            end_time_ms=end_time_ms,
        )

        return [(k["timestamp_ms"], k["close"]) for k in klines]

    async def fetch_cold_start_vol(
        self,
        symbol: str,
        interval: str,
        lookback: int = 20,
    ) -> list[float]:
        """
        Fetch recent klines for cold-start volatility estimation.
        Returns list of (close - open) / open deltas.
        """
        import time

        end_ms = int(time.time() * 1000)
        # Estimate how far back to go
        interval_seconds = {"1m": 60, "5m": 300, "15m": 900, "1h": 3600}
        seconds = interval_seconds.get(interval, 900)
        start_ms = end_ms - (lookback + 5) * seconds * 1000

        klines = await self.fetch_klines(
            symbol=symbol,
            interval=interval,
            start_time_ms=start_ms,
            end_time_ms=end_ms,
        )

        deltas = []
        for k in klines[-lookback:]:
            if k["open"] != 0:
                delta = (k["close"] - k["open"]) / k["open"]
                deltas.append(delta)

        logger.info(
            f"[backfill] Cold-start vol: {len(deltas)} deltas for {symbol} @ {interval}"
        )
        return deltas
