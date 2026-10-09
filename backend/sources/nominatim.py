"""Resolve a target place string into POI coordinates through a configurable geocoder.

Nominatim's usage policy shapes this source: at most one request per second from
this process, a User-Agent naming this application, and a Referer. A lookup that
fails or finds nothing returns None; callers treat that as "no place concept" and
must never invent coordinates from it. The service URL comes from configuration so
a production supplier can replace this class behind the place_resolver slot.
"""

import asyncio
from collections.abc import Mapping
from dataclasses import dataclass
import logging
import math
from time import monotonic
import unicodedata

import httpx

from backend.verification.models import PlaceReference

DEFAULT_BASE_URL = "https://nominatim.openstreetmap.org"
DEFAULT_USER_AGENT = "verify-app nominatim (+https://github.com/Catalyst259/Verify)"
SOURCE = "nominatim"
logger = logging.getLogger(__name__)
_MISSING = object()


def _identity_text(value: str) -> str:
    text = "".join(char for char in unicodedata.normalize("NFKC", value).casefold() if char.isalnum())
    for suffix in ("地铁站", "地鐵站"):
        if text.endswith(suffix):
            return text.removesuffix(suffix)
    return text


def _admin_aliases(value: str) -> set[str]:
    name = _identity_text(value)
    aliases = {name}
    for suffix in ("特别行政区", "自治区", "自治州", "省", "市", "区", "县"):
        if name.endswith(suffix):
            aliases.add(name.removesuffix(suffix))
    return set(filter(None, aliases))


@dataclass(frozen=True)
class _GeographicScope:
    prefix: str
    aliases: frozenset[str]
    country_code: str | None
    viewbox: str | None


def _scope_from_rows(prefix: str, rows: object) -> _GeographicScope | None:
    """Only an administrative entity, not a similarly named POI, can anchor a city."""
    if not isinstance(rows, list):
        return None
    scopes = []
    for row in rows:
        if not isinstance(row, Mapping):
            continue
        name = row.get("name")
        if not _matches_identity(prefix, row) and not (isinstance(name, str)
                                                      and _identity_text(prefix) in _admin_aliases(name)):
            continue
        if row.get("addresstype") not in {"city", "town", "municipality", "state", "province", "county"}:
            continue
        if row.get("category", row.get("class")) not in {"boundary", "place"}:
            continue
        names = [row.get("name"), prefix]
        details = row.get("namedetails")
        if isinstance(details, Mapping):
            names.extend(value for key, value in details.items() if isinstance(key, str) and key.startswith("name"))
        aliases = frozenset(alias for name in names if isinstance(name, str)
                            for alias in _admin_aliases(name))
        address = row.get("address", {})
        country = address.get("country_code") if isinstance(address, Mapping) else None
        viewbox = None
        try:
            south, north, west, east = map(float, row["boundingbox"])
            if -90 <= south < north <= 90 and -180 <= west < east <= 180:
                viewbox = f"{west},{north},{east},{south}"
        except (KeyError, TypeError, ValueError, OverflowError):
            pass
        scopes.append(_GeographicScope(prefix, aliases, country, viewbox))
    # Distinct administrative namesakes cannot be resolved by ranking alone.
    if len(scopes) != 1:
        return None
    return scopes[0]


def _matches_scope(row: Mapping, scope: _GeographicScope) -> bool:
    address = row.get("address")
    names = set()
    if isinstance(address, Mapping):
        country = address.get("country_code")
        if country and scope.country_code and country != scope.country_code:
            return False
        for key in ("state", "province", "region", "county", "city", "town", "municipality"):
            value = address.get(key)
            if isinstance(value, str):
                names.update(_admin_aliases(value))
    # Some Nominatim addresses put a district in `city`, while their display keeps the municipality.
    display = row.get("display_name")
    if isinstance(display, str):
        for part in display.split(",")[1:]:
            names.update(_admin_aliases(part.strip()))
    return bool(names & scope.aliases)


def _matches_identity(query: str, row: Mapping) -> bool:
    """实体名须完整一致；查询附带的行政限定只能由候选地址确认。"""
    names = [row.get("name")]
    details = row.get("namedetails")
    if isinstance(details, Mapping):
        names.extend(value for key, value in details.items() if isinstance(key, str) and key.split(":", 1)[0] in {
            "name", "alt_name", "short_name", "loc_name", "int_name", "official_name", "old_name",
        })
    if not any(isinstance(name, str) and name.strip() for name in names):
        display = row.get("display_name")
        if isinstance(display, str):
            names.append(display.split(",", 1)[0])
    entities = {_identity_text(alias) for name in names if isinstance(name, str)
                for alias in name.split(";") if alias.strip()}
    target = _identity_text(query)
    address = row.get("address")
    admins = set()
    if isinstance(address, Mapping):
        for key in ("country", "state", "province", "region", "county", "city", "town", "village",
                    "municipality", "district", "borough", "suburb"):
            value = address.get(key)
            if isinstance(value, str) and value.strip():
                name = _identity_text(value)
                admins.add(name)
                for suffix in ("特别行政区", "自治区", "省", "市", "区", "县"):
                    if name.endswith(suffix):
                        admins.add(name.removesuffix(suffix))
    for entity in entities:
        if not entity:
            continue
        if target == entity:
            return True
        if target.endswith(entity):
            scope = target[:-len(entity)]
            for admin in sorted(filter(None, admins), key=len, reverse=True):
                scope = scope.replace(admin, "")
            if not scope:
                return True
    return False


class NominatimPlaceResolver:
    """Resolve one place string per call, reusing results inside the process.

    Administrative prefixes are confirmed independently before selecting a POI;
    scoped endpoint queries cannot use cross-region namesakes. ``min_interval_seconds``
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
        self._cache: dict[tuple[str, str], PlaceReference | None] = {}
        self._rows: dict[tuple[str, str], object] = {}
        self._scopes: dict[str, _GeographicScope | None] = {}
        self._failed_queries: set[tuple[str, str]] = set()
        self._failed_scopes: set[str] = set()
        self._unconfirmed_scopes: set[str] = set()
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
        return await self.resolve_scoped(target_place)

    async def resolve_scoped(self, target_place: str, *, scope: str = "") -> PlaceReference | None:
        """Resolve within the target's confirmed city, unless the endpoint explicitly names another city."""
        query = target_place.strip()
        if not query:
            return None
        key = (query, scope.strip())
        cached = self._cache.get(key, _MISSING)
        if cached is not _MISSING:
            return cached
        async with self._gate:
            cached = self._cache.get(key, _MISSING)
            if cached is not _MISSING:
                return cached
            rows = await self._lookup_rows(query)
            own_scope = await self._query_scope(query, rows)
            if query in self._failed_scopes:
                self._cache[key] = None
                return None
            inherited = None
            if own_scope is None and scope.strip():
                scope_rows = await self._lookup_rows(scope.strip())
                inherited = await self._query_scope(scope.strip(), scope_rows)
                if inherited is None:
                    self._cache[key] = None
                    return None
            elif own_scope is None and query in self._unconfirmed_scopes:
                # A successful but empty/ambiguous city lookup does not confirm the original namesake.
                self._cache[key] = None
                return None
            geographic_scope = own_scope or inherited
            place = self._first_poi(query, rows, scope=geographic_scope)
            if place is None and geographic_scope is not None:
                # Remove only a provider-confirmed administrative prefix; the POI identity stays exact.
                bare = query.removeprefix(geographic_scope.prefix).strip(" ,，")
                rows = await self._lookup_rows(bare, geographic_scope)
                place = self._first_poi(bare, rows, scope=geographic_scope)
            self._cache[key] = place
            return place

    async def _query_scope(self, query: str, rows: object) -> _GeographicScope | None:
        if query in self._scopes:
            return self._scopes[query]
        found = _scope_from_rows(query, rows)
        matching = ([row for row in rows if isinstance(row, Mapping) and _matches_identity(query, row)]
                    if isinstance(rows, list) else [])
        # A city name without 市 is common in Chinese submissions. Confirm short prefixes with the provider.
        prefixes = [query[:size] for size in range(2, min(4, len(query) - 1) + 1)
                    if all("\u4e00" <= char <= "\u9fff" for char in query[:size])]
        if not prefixes and " " in query:
            prefixes = [query.split(" ", 1)[0]]
        if found is None and not any(isinstance(row.get("address"), Mapping) for row in matching):
            if matching and prefixes:
                self._unconfirmed_scopes.add(query)
            self._scopes[query] = None
            return None
        for prefix in prefixes if found is None else ():
            prefix_rows = await self._lookup_rows(prefix)
            if (prefix, "") in self._failed_queries:
                self._failed_scopes.add(query)
                break
            found = _scope_from_rows(prefix, prefix_rows)
            if found is not None:
                break
        if found is None and prefixes:
            self._unconfirmed_scopes.add(query)
        self._scopes[query] = found
        return found

    async def _lookup_rows(self, query: str, scope: _GeographicScope | None = None) -> object:
        key = (query, scope.prefix if scope else "")
        cached = self._rows.get(key, _MISSING)
        if cached is not _MISSING:
            return cached
        await self._wait_for_slot()
        params = {"q": query, "format": "jsonv2", "accept-language": "zh-CN",
                  "namedetails": 1, "addressdetails": 1}
        if scope and scope.viewbox:
            params.update(viewbox=scope.viewbox, bounded=1)
        try:
            response = await self._http().get(f"{self.base_url}/search",
                                              params=params,
                                              headers={"User-Agent": self.user_agent, "Referer": self.referer})
            response.raise_for_status()
            rows = response.json()
            if not isinstance(rows, list):
                raise ValueError("地点供应商响应不是候选数组")
        except Exception as error:
            logger.warning("nominatim_lookup_failed error_type=%s", type(error).__name__)
            self._failed_queries.add(key)
            rows = []
        finally:
            # Start the next interval after completion; delayed request preparation must not consume the spacing.
            self._last_request_at = monotonic()
        self._rows[key] = rows
        return rows

    async def _wait_for_slot(self):
        while (delay := self._last_request_at + self.min_interval_seconds - monotonic()) > 0:
            await asyncio.sleep(delay)

    def _http(self) -> httpx.AsyncClient:
        # Created on first use: the app builds capabilities outside a running loop.
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=self.timeout_seconds, transport=self._transport)
        return self._client

    @staticmethod
    def _first_poi(query: str, rows: object, *, scope: _GeographicScope | None = None) -> PlaceReference | None:
        """Take a matching entity with usable coordinates; unrelated ranked hits are unknown."""
        if not isinstance(rows, list):
            return None
        for row in rows:
            if not isinstance(row, Mapping):
                continue
            identity_query = query.removeprefix(scope.prefix).strip(" ,，") if scope else query
            if not _matches_identity(identity_query or query, row) and not _matches_identity(query, row):
                continue
            if scope is not None and not _matches_scope(row, scope):
                continue
            try:
                latitude, longitude = float(row["lat"]), float(row["lon"])
            except (KeyError, TypeError, ValueError, OverflowError):
                continue
            if not (-90 <= latitude <= 90 and -180 <= longitude <= 180):
                continue
            if scope is not None and scope.viewbox:
                west, north, east, south = map(float, scope.viewbox.split(","))
                if not (south <= latitude <= north and west <= longitude <= east):
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
