"""Routing, time-distance matrix and walk-time isochrones from a Valhalla service.

Used as the `map_routing` capability: judging a "walk N minutes" claim needs the
route duration or the reachable area, never straight-line distance, which would
mark exaggerated walking times as true. A failed request raises instead of
returning a number, so callers can only conclude "not verified".
"""

import asyncio
from collections.abc import Sequence
import json
import math
import time
from urllib.parse import urlsplit

import httpx

from ..verification.capabilities import Coordinates, Costing, Isochrone, MatrixResult, RouteResult

# 服务方要求请求方自报身份；库的默认 UA 会被封，配置留空时用本应用的默认身份。
DEFAULT_USER_AGENT = "verify-app (+https://github.com/Catalyst259/Verify)"


class ValhallaError(RuntimeError):
    """A routing request did not yield usable results; no value is fabricated."""


def _point(coordinates: Coordinates) -> dict:
    return {"lat": coordinates.latitude, "lon": coordinates.longitude}


def _meters(value: float | None) -> float | None:
    """Valhalla reports kilometres; the capability reports metres."""
    return None if value is None else value * 1000


def _detail(response: httpx.Response) -> str:
    try:
        body = response.json()
    except ValueError:
        body = None
    error = body.get("error") if isinstance(body, dict) else None
    return f"Valhalla 返回 HTTP {response.status_code}：{error or '无错误说明'}"


def _rings(geometry: dict) -> tuple[tuple[Coordinates, ...], ...]:
    """Flatten Polygon and MultiPolygon exteriors into one ring list.

    Holes are skipped: reachability is tested with point-in-polygon over the
    exterior, and a hole small enough to contain the target is never produced.
    """
    if geometry.get("type") == "Polygon":
        polygons = [geometry["coordinates"]]
    elif geometry.get("type") == "MultiPolygon":
        polygons = geometry["coordinates"]
    else:
        raise ValhallaError("等时圈响应不是面几何，无法表达可达范围")
    exterior = []
    for polygon in polygons:
        if not polygon or not polygon[0]:
            raise ValhallaError("等时圈响应含无可达范围的面")
        exterior.append(tuple(Coordinates(latitude, longitude) for longitude, latitude in polygon[0]))
    return tuple(exterior)


class ValhallaRouting:
    """One owner of the shared HTTP connection; results are cached per run.

    The service is rate limited to one request per second. Only the request
    start times are gated, so a cache hit never burns another window.
    """

    def __init__(self, base_url: str, *, user_agent: str, referer: str = "", timeout_seconds: float = 30,
                 cache_size: int = 128, min_request_interval_seconds: float = 1,
                 client: httpx.AsyncClient | None = None):
        parsed = urlsplit(base_url)
        if parsed.scheme not in ("http", "https") or not parsed.netloc:
            raise ValueError("valhalla.base_url 必须是 http(s) 地址")
        if not user_agent.strip():
            raise ValueError("valhalla.user_agent 必须可识别本应用")
        if not math.isfinite(timeout_seconds) or timeout_seconds <= 0:
            raise ValueError("valhalla.timeout_seconds 必须大于 0")
        if isinstance(cache_size, bool) or not isinstance(cache_size, int) or cache_size < 1:
            raise ValueError("valhalla.cache_size 必须为正整数")
        if not math.isfinite(min_request_interval_seconds) or min_request_interval_seconds < 0:
            raise ValueError("valhalla.min_request_interval_seconds 不能为负数")
        self.base_url = base_url.rstrip("/")
        self.user_agent = user_agent
        self.referer = referer
        self.timeout_seconds = timeout_seconds
        self.cache_size = cache_size
        self.min_request_interval_seconds = min_request_interval_seconds
        # ponytail: fixed bounded FIFO; add eviction policy only if hit rates matter.
        self._cache: dict[str, dict] = {}
        self._client = client
        self._next_request_at = time.monotonic() + min_request_interval_seconds
        self._lock = asyncio.Lock()

    @classmethod
    def from_config(cls, config: dict[str, object]) -> "ValhallaRouting":
        settings = config.get("valhalla", {})
        if not isinstance(settings, dict):
            raise ValueError("valhalla 配置必须是表")
        return cls(
            str(settings.get("base_url") or "https://valhalla1.openstreetmap.de"),
            user_agent=str(settings.get("user_agent") or DEFAULT_USER_AGENT),
            referer=str(settings.get("referer") or ""),
            timeout_seconds=settings.get("timeout_seconds", 30),
            cache_size=settings.get("cache_size", 128),
            min_request_interval_seconds=settings.get("min_request_interval_seconds", 1),
        )

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()

    async def route(self, origin: Coordinates, destination: Coordinates, costing: Costing) -> RouteResult:
        body = await self._request("/route", {"locations": [_point(origin), _point(destination)],
                                              "costing": costing})
        summary = body.get("trip", {}).get("summary")
        if not isinstance(summary, dict):
            raise ValhallaError("路线响应缺少行程摘要")
        return RouteResult(costing=costing, duration_seconds=summary.get("time"),
                           distance_meters=_meters(summary.get("length")))

    async def matrix(self, sources: Sequence[Coordinates], targets: Sequence[Coordinates],
                     costing: Costing) -> MatrixResult:
        if not sources or not targets:
            raise ValueError("矩阵请求需要至少一个起点和终点")
        body = await self._request("/sources_to_targets",
                                   {"sources": [_point(source) for source in sources],
                                    "targets": [_point(target) for target in targets], "costing": costing})
        table = body.get("sources_to_targets")
        if table is None:
            raise ValhallaError("矩阵响应缺少 sources_to_targets 字段")
        return MatrixResult(
            costing=costing,
            durations_seconds=tuple(tuple(cell.get("time") if cell else None for cell in row) for row in table),
            distances_meters=tuple(tuple(_meters(cell.get("distance")) if cell else None for cell in row)
                                   for row in table),
        )

    async def isochrone(self, origin: Coordinates, costing: Costing, minutes: float) -> Isochrone:
        if not math.isfinite(minutes) or minutes <= 0:
            raise ValueError("等时圈时长必须大于 0 分钟")
        body = await self._request("/isochrone",
                                   {"locations": [_point(origin)], "costing": costing, "polygons": True,
                                    "contours": [{"time": minutes}]})
        features = body.get("features")
        if not features:
            raise ValhallaError("等时圈响应没有可达面，本时长内没有可达范围")
        return Isochrone(costing=costing, minutes=minutes,
                         rings=tuple(ring for feature in features for ring in _rings(feature.get("geometry") or {})))

    async def _request(self, path: str, payload: dict) -> dict:
        key = f"{path}|{json.dumps(payload, sort_keys=True)}"
        cached = self._cache.get(key)
        if cached is None:
            cached = await self._fetch(key, path, payload)
        return cached

    async def _fetch(self, key: str, path: str, payload: dict) -> dict:
        async with self._lock:
            cached = self._cache.get(key)
            if cached is None:
                cached = await self._call(path, payload)
                if len(self._cache) >= self.cache_size:
                    del self._cache[next(iter(self._cache))]
                self._cache[key] = cached
        return cached

    async def _call(self, path: str, payload: dict) -> dict:
        delay = self._next_request_at - time.monotonic()
        if delay > 0:
            await asyncio.sleep(delay)
        self._next_request_at = time.monotonic() + self.min_request_interval_seconds
        client = self._client
        temporary = client is None
        if temporary:
            client = httpx.AsyncClient(follow_redirects=True)
        try:
            headers = {"User-Agent": self.user_agent}
            if self.referer:
                # The service policy asks scripts to identify the calling application.
                headers["Referer"] = self.referer
            # The payload stays on the query string, the form the service documents.
            response = await client.get(self.base_url + path, params={"json": json.dumps(payload)},
                                        headers=headers, timeout=self.timeout_seconds)
        except httpx.HTTPError as error:
            raise ValhallaError(f"Valhalla 请求失败（{type(error).__name__}）") from None
        finally:
            if temporary:
                await client.aclose()
        if response.status_code >= 400:
            raise ValhallaError(_detail(response))
        try:
            body = response.json()
        except ValueError:
            raise ValhallaError("Valhalla 响应不是 JSON") from None
        if not isinstance(body, dict):
            raise ValhallaError("Valhalla 响应不是对象")
        if body.get("error"):
            raise ValhallaError(str(body["error"]))
        return body
