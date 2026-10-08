"""Resolve a target place string into POI coordinates through a configurable geocoder.

Nominatim's usage policy shapes this source: at most one request per second from
this process, a User-Agent naming this application, and a Referer. A lookup that
fails or finds nothing returns None; callers treat that as "no place concept" and
must never invent coordinates from it. The service URL comes from configuration so
a production supplier can replace this class behind the place_resolver slot.
"""

import asyncio
import logging
import math
from collections.abc import Mapping
from time import monotonic

import httpx

from backend.verification.models import PlaceReference

DEFAULT_BASE_URL = "https://nominatim.openstreetmap.org"
DEFAULT_USER_AGENT = "verify-app nominatim (+https://github.com/Catalyst259/Verify)"
SOURCE = "nominatim"
logger = logging.getLogger(__name__)
_MISSING = object()


class NominatimPlaceResolver:
    """Resolve one place string per call, reusing results inside the process.

    Only the first result the service ranks is used; this build resolves a single
    POI and does not pick among same-named candidates. ``min_interval_seconds``
    exists for tests; production keeps it at the policy floor of one second.
    """

    def __init__(self, *, base_url: str = DEFAULT_BASE_URL, user_agent: str = DEFAULT_USER_AGENT,
                 referer: str = "", timeout_seconds: float = 10, min_interval_seconds: float = 1,
                 transport: httpx.AsyncBaseTransport | None = None):
        if not base_url.strip():
            raise ValueError("nominatim.base_url 不能为空")
        if not user_agent.strip():
            raise ValueError("nominatim.user_agent 不能为空；库默认 UA 无法识别本应用")
        if not math.isfinite(timeout_seconds) or timeout_seconds <= 0:
            raise ValueError("nominatim.timeout_seconds 必须大于 0")
        if not math.isfinite(min_interval_seconds) or min_interval_seconds <= 0:
            raise ValueError("nominatim.min_interval_seconds 必须大于 0")
        self.base_url = base_url.rstrip("/")
        self.user_agent = user_agent
        self.referer = referer
        self.timeout_seconds = timeout_seconds
        self.min_interval_seconds = min_interval_seconds
        self._transport = transport
        self._client: httpx.AsyncClient | None = None
        self._cache: dict[str, PlaceReference | None] = {}
        # 缓存与闸门属于整个进程：装配点只建一个实例，所有请求和运行共用这份请求预算。
        self._gate = asyncio.Lock()
        self._last_request_at = -math.inf

    @classmethod
    def from_config(cls, config: Mapping) -> "NominatimPlaceResolver":
        settings = config.get("nominatim", {})
        return cls(base_url=settings.get("base_url") or DEFAULT_BASE_URL,
                   user_agent=settings.get("user_agent") or DEFAULT_USER_AGENT,
                   referer=settings.get("referer", ""),
                   timeout_seconds=settings.get("timeout_seconds", 10))

    async def __call__(self, target_place: str) -> PlaceReference | None:
        query = target_place.strip()
        if not query:
            return None
        cached = self._cache.get(query, _MISSING)
        if cached is not _MISSING:
            return cached
        async with self._gate:
            cached = self._cache.get(query, _MISSING)
            if cached is not _MISSING:
                return cached
            place = await self._lookup(query)
            self._cache[query] = place
            return place

    async def _lookup(self, query: str) -> PlaceReference | None:
        await self._wait_for_slot()
        try:
            response = await self._http().get(f"{self.base_url}/search",
                                              params={"q": query, "format": "jsonv2", "accept-language": "zh-CN"},
                                              headers={"User-Agent": self.user_agent, "Referer": self.referer})
            response.raise_for_status()
            rows = response.json()
        except Exception as error:
            logger.warning("nominatim_lookup_failed %s", f"{query!r}: {type(error).__name__}")
            return None
        return self._first_poi(query, rows)

    async def _wait_for_slot(self):
        delay = self._last_request_at + self.min_interval_seconds - monotonic()
        if delay > 0:
            await asyncio.sleep(delay)
        self._last_request_at = monotonic()

    def _http(self) -> httpx.AsyncClient:
        # Created on first use: the app builds capabilities outside a running loop.
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=self.timeout_seconds, transport=self._transport)
        return self._client

    @staticmethod
    def _first_poi(query: str, rows: object) -> PlaceReference | None:
        """Take the service's top result; skip rows without usable coordinates."""
        if not isinstance(rows, list):
            return None
        for row in rows:
            try:
                latitude, longitude = float(row["lat"]), float(row["lon"])
            except (KeyError, TypeError, ValueError):
                continue
            if not (math.isfinite(latitude) and math.isfinite(longitude)):
                continue
            reference = row.get("place_id")
            return PlaceReference(
                name=str(row.get("name") or row.get("display_name") or query),
                reference=None if reference is None else str(reference),
                latitude=latitude, longitude=longitude, source=SOURCE,
            )
        return None

    async def aclose(self):
        client, self._client = self._client, None
        if client is not None:
            await client.aclose()
