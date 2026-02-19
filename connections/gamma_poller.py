"""
Gamma API poller.
Discovers active crypto markets and extracts token IDs.
Polls periodically and on interval boundaries.
"""

import asyncio
import json
import logging
import re
from typing import Optional

try:
    import aiohttp
except ImportError:
    aiohttp = None

from clock import get_current_interval_start
from config import GAMMA_REST_URL, GAMMA_POLL_INTERVAL_S, TIMEFRAME_SETTINGS, SSL_CONTEXT

logger = logging.getLogger(__name__)


class MarketInfo:
    """Parsed market metadata from Gamma API."""

    def __init__(
        self,
        market_id: str,
        slug: str,
        question: str,
        asset: str,
        timeframe: str,
        start_ts: int,
        up_token_id: str,
        down_token_id: str,
        active: bool,
        closed: bool,
    ):
        self.market_id = market_id
        self.slug = slug
        self.question = question
        self.asset = asset
        self.timeframe = timeframe
        self.start_ts = start_ts
        self.up_token_id = up_token_id
        self.down_token_id = down_token_id
        self.active = active
        self.closed = closed

    @property
    def interval_id(self) -> str:
        return f"{self.asset}-updown-{self.timeframe}-{self.start_ts}"


class GammaPoller:
    """Polls Gamma API for active crypto prediction markets."""

    # Pattern to match market slugs like "btc-updown-5m-1739836800"
    SLUG_PATTERN = re.compile(
        r"(?P<asset>btc|eth|sol|xrp)[- ](?:up(?:down)?|down)[- ]"
        r"(?P<timeframe>5m|15m)[- ](?P<ts>\d{10})",
        re.IGNORECASE,
    )

    def __init__(
        self,
        assets: list[str],
        timeframes: list[str],
        on_market_found=None,
    ):
        """
        Args:
            assets: Assets to track ["btc", "eth", ...]
            timeframes: Timeframes to track ["5m", "15m"]
            on_market_found: callback(MarketInfo)
        """
        self._assets = set(a.lower() for a in assets)
        self._timeframes = set(t.lower() for t in timeframes)
        self._on_market_found = on_market_found
        self._known_slugs: set[str] = set()
        self._session: Optional[aiohttp.ClientSession] = None
        self._running = False

    async def start(self):
        """Start polling loop."""
        self._running = True
        self._session = aiohttp.ClientSession(
            connector=aiohttp.TCPConnector(ssl=SSL_CONTEXT)
        )

        # Initial fetch
        await self._poll()

        # Polling loop
        while self._running:
            await asyncio.sleep(GAMMA_POLL_INTERVAL_S)
            if self._running:
                await self._poll()

    async def stop(self):
        self._running = False
        if self._session:
            await self._session.close()
            self._session = None

    async def poll_once(self):
        """One-shot poll, for boundary-triggered queries."""
        await self._poll()

    async def _poll(self):
        """Discover markets by constructing expected slugs and querying directly."""
        if not self._session:
            return

        for asset in self._assets:
            for timeframe in self._timeframes:
                duration = TIMEFRAME_SETTINGS[timeframe]["duration_s"]
                current_start = get_current_interval_start(timeframe)

                # Check current and next interval
                for start_ts in (current_start, current_start + duration):
                    slug = f"{asset}-updown-{timeframe}-{start_ts}"
                    if slug in self._known_slugs:
                        continue
                    await self._fetch_by_slug(slug)

    async def _fetch_by_slug(self, slug: str):
        """Query Gamma API for a single event by slug."""
        try:
            url = f"{GAMMA_REST_URL}/events"
            params = {"slug": slug}

            async with self._session.get(
                url, params=params, timeout=aiohttp.ClientTimeout(total=10)
            ) as resp:
                if resp.status != 200:
                    logger.warning(f"[gamma] HTTP {resp.status} querying {slug}")
                    return

                events = await resp.json()

            if not events:
                return

            for market in events[0].get("markets", []):
                self._process_market(market)

        except asyncio.TimeoutError:
            logger.warning(f"[gamma] Timeout querying {slug}")
        except Exception as e:
            logger.error(f"[gamma] Poll error for {slug}: {e}")

    def _process_market(self, market: dict):
        """Parse a market object and emit if it's new and relevant."""
        slug = market.get("slug", "")

        # Skip already-known markets
        if slug in self._known_slugs:
            return

        # Try to parse the slug
        parsed = self._parse_slug(slug)
        if not parsed:
            # Also try parsing from question field
            question = market.get("question", "")
            parsed = self._parse_question(question)
            if not parsed:
                return

        asset, timeframe, start_ts = parsed

        # Filter by configured assets and timeframes
        if asset not in self._assets:
            return
        if timeframe not in self._timeframes:
            return

        # Extract token IDs (both fields are JSON-encoded strings)
        clob_token_ids = market.get("clobTokenIds", "[]")
        if isinstance(clob_token_ids, str):
            try:
                clob_token_ids = json.loads(clob_token_ids)
            except (json.JSONDecodeError, TypeError):
                clob_token_ids = []

        outcomes = market.get("outcomes", "[]")
        if isinstance(outcomes, str):
            try:
                outcomes = json.loads(outcomes)
            except (json.JSONDecodeError, TypeError):
                outcomes = []

        # Map outcomes to token IDs
        up_token_id = ""
        down_token_id = ""

        for i, outcome in enumerate(outcomes):
            outcome_lower = outcome.lower() if isinstance(outcome, str) else ""
            if i < len(clob_token_ids):
                if "up" in outcome_lower or "yes" in outcome_lower:
                    up_token_id = clob_token_ids[i]
                elif "down" in outcome_lower or "no" in outcome_lower:
                    down_token_id = clob_token_ids[i]

        if not up_token_id or not down_token_id:
            # Fallback: assume first is Up, second is Down
            if len(clob_token_ids) >= 2:
                up_token_id = clob_token_ids[0]
                down_token_id = clob_token_ids[1]
            else:
                logger.warning(f"[gamma] Can't determine token IDs for {slug}")
                return

        self._known_slugs.add(slug)

        info = MarketInfo(
            market_id=market.get("id", ""),
            slug=slug,
            question=market.get("question", ""),
            asset=asset,
            timeframe=timeframe,
            start_ts=start_ts,
            up_token_id=up_token_id,
            down_token_id=down_token_id,
            active=market.get("active", False),
            closed=market.get("closed", False),
        )

        logger.info(
            f"[gamma] New market: {slug} | "
            f"Up: {up_token_id[:12]}... Down: {down_token_id[:12]}..."
        )

        if self._on_market_found:
            try:
                self._on_market_found(info)
            except Exception as e:
                logger.error(f"[gamma] Market found callback error: {e}")

    def _parse_slug(self, slug: str) -> tuple[str, str, int] | None:
        """Try to parse asset, timeframe, and timestamp from slug."""
        match = self.SLUG_PATTERN.search(slug)
        if match:
            return (
                match.group("asset").lower(),
                match.group("timeframe").lower(),
                int(match.group("ts")),
            )
        return None

    def _parse_question(self, question: str) -> tuple[str, str, int] | None:
        """Fallback: try to parse from question text."""
        match = self.SLUG_PATTERN.search(question.lower().replace(" ", "-"))
        if match:
            return (
                match.group("asset").lower(),
                match.group("timeframe").lower(),
                int(match.group("ts")),
            )
        return None

    async def check_resolution(self, market_slug: str) -> str | None:
        """Check if a market has resolved. Returns 'up', 'down', or None."""
        if not self._session:
            return None

        try:
            url = f"{GAMMA_REST_URL}/markets"
            params = {"slug": market_slug}

            async with self._session.get(url, params=params, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                if resp.status != 200:
                    return None

                markets = await resp.json()

            if not markets:
                return None

            market = markets[0] if isinstance(markets, list) else markets

            # Check various resolution indicators
            if market.get("closed"):
                outcome_prices = market.get("outcomePrices", "")
                if isinstance(outcome_prices, str):
                    try:
                        outcome_prices = json.loads(outcome_prices)
                    except (json.JSONDecodeError, TypeError):
                        return None

                if isinstance(outcome_prices, list) and len(outcome_prices) >= 2:
                    up_price = float(outcome_prices[0])
                    if up_price >= 0.99:
                        return "up"
                    elif up_price <= 0.01:
                        return "down"

            return None

        except Exception as e:
            logger.error(f"[gamma] Resolution check error for {market_slug}: {e}")
            return None
