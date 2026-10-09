"""地点解析产出的坐标必须来自服务响应；HTTP 用替身，不打公共服务。"""

import asyncio
from time import monotonic

import httpx
import pytest

from backend.extraction.models import ClaimExtractionResult
from backend.sources.nominatim import NominatimPlaceResolver
from backend.storage.repository import StorageRepository
from backend.verification.capabilities import VerificationCapabilities
from backend.verification.models import VerificationInput
from backend.verification.service import VerificationService


ROW = {"lat": "31.1440374", "lon": "121.6572943", "name": "上海迪士尼乐园",
       "display_name": "上海迪士尼乐园, 川沙新镇, 浦东新区, 上海市, 中国", "place_id": 566792571,
       "address": {"city": "上海市", "country_code": "cn"}}


def resolve(tmp_path, resolver, target_place="上海迪士尼乐园"):
    async def extract(*args):
        return ClaimExtractionResult(target_place=target_place, claims=[{
            "claim_id": "claim_001", "type": "FACT", "content": "免费开放。",
            "sources": [{"source_type": "TEXT", "source_ref": None, "source_text": "免费开放"}],
        }])

    async def skip_llm(prompt, task):
        return "[]"

    storage = StorageRepository(tmp_path)
    storage.initialize()
    service = VerificationService(storage, extract, capabilities=VerificationCapabilities(
        place_resolver=resolver, llm=skip_llm,
    ))
    return asyncio.run(service.run(VerificationInput(target_place=target_place, text="免费开放。")))


def resolver(handler, *, interval=0.01):
    return NominatimPlaceResolver(base_url="https://geocoder.test", referer="http://localhost:8000/",
                                  min_interval_seconds=interval, transport=httpx.MockTransport(handler))


def test_resolved_place_carries_coordinates_and_an_identifiable_source(tmp_path):
    sent = []

    def handler(request):
        sent.append(request)
        if request.url.params["q"] == "上海":
            return httpx.Response(200, json=[{"name": "上海市", "category": "boundary", "addresstype": "city",
                                             "address": {"city": "上海市", "country_code": "cn"}}])
        return httpx.Response(200, json=[ROW])

    run = resolve(tmp_path, resolver(handler))
    place = run.context.resolved_place
    assert (place.name, place.reference, place.latitude, place.longitude, place.source) == (
        "上海迪士尼乐园", "566792571", 31.1440374, 121.6572943, "nominatim")
    request = sent[0]
    assert request.url.path == "/search"
    assert dict(request.url.params) == {"q": "上海迪士尼乐园", "format": "jsonv2", "accept-language": "zh-CN",
                                        "namedetails": "1", "addressdetails": "1"}
    assert "Verify" in request.headers["user-agent"]
    assert request.headers["referer"] == "http://localhost:8000/"


@pytest.mark.parametrize("response", [
    httpx.Response(200, json=[]),
    httpx.Response(200, json=[{"lat": "not-a-number", "lon": "121.6572943", "name": "上海迪士尼乐园"}]),
    httpx.Response(500, text="upstream busy"),
], ids=["no-hit", "unusable-coordinates", "server-error"])
def test_failed_resolution_leaves_the_place_unknown(tmp_path, response):
    """无结果、坐标不可用或服务报错都只是「没有地点概念」，不能据此编造 POI。"""
    run = resolve(tmp_path, resolver(lambda request: response))
    assert run.context.resolved_place is None
    assert run.context.target_place == "上海迪士尼乐园"


def test_identical_scoped_queries_do_not_repeat_requests(tmp_path):
    sent = []

    def handler(request):
        sent.append(str(request.url))
        if request.url.params["q"] == "上海":
            return httpx.Response(200, json=[{"name": "上海市", "category": "boundary", "addresstype": "city",
                                             "address": {"city": "上海市", "country_code": "cn"}}])
        return httpx.Response(200, json=[ROW | {"name": "武康大楼", "lat": "31.20113", "lon": "121.43159",
            "display_name": "武康大楼, 徐汇区, 上海市, 中国"}])

    place_resolver = resolver(handler)

    async def exercise():
        return [await place_resolver("上海武康大楼") for _ in range(3)]

    places = asyncio.run(exercise())
    assert [place.latitude for place in places] == [31.20113] * 3
    assert len(sent) == 2 and len(set(sent)) == 2


def test_the_process_sends_at_most_one_request_per_interval():
    sent = []

    def handler(request):
        sent.append(monotonic())
        return httpx.Response(200, json=[ROW | {"name": request.url.params["q"]}])

    place_resolver = resolver(handler, interval=0.2)

    async def exercise():
        for query in ("武康大楼", "上海海昌海洋公园"):
            await place_resolver(query)

    asyncio.run(exercise())
    assert len(sent) >= 2
    assert all(later - earlier >= 0.2 for earlier, later in zip(sent, sent[1:]))


def test_rate_floor_starts_after_transport_dispatch_even_when_request_preparation_is_delayed():
    sent = []
    hook_count = 0

    async def delay_first(request):
        nonlocal hook_count
        hook_count += 1
        if hook_count == 1:
            await asyncio.sleep(0.08)

    def handler(request):
        sent.append(monotonic())
        return httpx.Response(200, json=[{"name": request.url.params["q"], "lat": "30", "lon": "120"}])

    async def exercise():
        current = NominatimPlaceResolver(base_url="https://geo.test", min_interval_seconds=0.05)
        current._client = httpx.AsyncClient(transport=httpx.MockTransport(handler),
                                          event_hooks={"request": [delay_first]})
        try:
            await current("甲地")
            await current("乙地")
        finally:
            await current.aclose()

    asyncio.run(exercise())
    assert len(sent) == 2 and sent[1] - sent[0] >= 0.05


def test_rate_floor_rechecks_monotonic_time_after_an_early_timer_wakeup(monkeypatch):
    import backend.sources.nominatim as module
    clock = [0.0]
    sleeps = []

    async def early_sleep(delay):
        sleeps.append(delay)
        clock[0] += delay / 2 if len(sleeps) == 1 else delay

    monkeypatch.setattr(module, "monotonic", lambda: clock[0])
    monkeypatch.setattr(module.asyncio, "sleep", early_sleep)
    current = resolver(lambda request: httpx.Response(200, json=[]), interval=1)
    current._last_request_at = 0.0
    asyncio.run(current._wait_for_slot())
    assert clock[0] >= 1 and sleeps == [1, 0.5]


@pytest.mark.parametrize("query,row", [
    ("龙翔桥地铁站", {"name": "地铁霞鸣街站（A·B）", "lat": "30.1461542", "lon": "120.0685247",
                    "display_name": "地铁霞鸣街站（A·B）, 象山路, 西湖区, 浙江省, 中国"}),
    ("西湖湖滨", {"name": "象湖滨江江滩公园", "lat": "28.6101510", "lon": "115.8304750",
                "display_name": "象湖滨江江滩公园, 西湖区, 南昌市, 江西省, 中国"}),
    ("西湖", {"name": "杭州西湖国宾馆", "lat": "30.2394137", "lon": "120.1291445"}),
    ("上海迪士尼乐园", {"name": "无关商店", "display_name": "无关商店, 上海迪士尼乐园路, 中国",
                      "lat": "31.1440374", "lon": "121.6572943"}),
])
def test_fuzzy_or_address_only_hits_cannot_supply_another_entity(query, row):
    assert NominatimPlaceResolver._first_poi(query, [row]) is None


def test_matching_entity_can_follow_unrelated_ranked_results():
    wrong = ROW | {"name": "无关商店"}
    place = NominatimPlaceResolver._first_poi("上海迪士尼乐园", [wrong, ROW])
    assert place.name == "上海迪士尼乐园" and place.reference == str(ROW["place_id"])


@pytest.mark.parametrize("query,row", [
    ("龙翔桥地铁站", {"name": "龙翔桥", "lat": "30.257", "lon": "120.163"}),
    ("龍翔橋地鐵站", {"name": "Longxiangqiao", "namedetails": {"name:zh-Hant": "龍翔橋"},
                    "lat": "30.257", "lon": "120.163"}),
    ("武康大楼", {"name": "Wukang Mansion", "namedetails": {"alt_name": "诺曼底公寓;武康大楼"},
                "lat": "31.20113", "lon": "121.43159"}),
    ("武康大楼", {"display_name": "武康大楼, 徐汇区, 上海市, 中国", "lat": "31.20113", "lon": "121.43159"}),
    ("CENTRAL PARK", {"name": "Central Park", "lat": "40.78", "lon": "-73.96"}),
])
def test_complete_names_and_provided_aliases_remain_eligible(query, row):
    assert NominatimPlaceResolver._first_poi(query, [row]) is not None


@pytest.mark.parametrize("query,row", [
    ("杭州龙翔桥地铁站", {"name": "龙翔桥", "lat": "30.257", "lon": "120.163",
                      "address": {"city": "杭州市", "state": "浙江省", "country": "中国"}}),
    ("香港佐敦地铁站", {"name": "Jordan", "namedetails": {"name:zh": "佐敦"},
                    "lat": "22.305", "lon": "114.171", "address": {"city": "香港", "country": "中国"}}),
    ("台北101", {"name": "台北101", "lat": "25.034", "lon": "121.565", "address": {"city": "臺北市", "country_code": "tw"}}),
    ("纽约中央公园", {"name": "Central Park", "namedetails": {"name:zh": "中央公园"},
                  "lat": "40.78", "lon": "-73.96", "address": {"city": "纽约", "country_code": "us"}}),
])
def test_explicit_scope_uses_candidate_administration_without_country_restriction(query, row):
    assert NominatimPlaceResolver._first_poi(query, [row]) is not None


@pytest.mark.parametrize("address", [{"city": "上海市"}, {}, {"country_code": "cn"}])
def test_an_explicit_city_is_not_guessed_when_missing_or_inconsistent(address):
    row = {"name": "龙翔桥", "lat": "30.257", "lon": "120.163", "address": address}
    assert NominatimPlaceResolver._first_poi("杭州龙翔桥地铁站", [row]) is None


def test_rejected_entity_is_cached_as_unknown_without_a_repeat_request():
    sent = []

    def handler(request):
        sent.append(request)
        return httpx.Response(200, json=[ROW | {"name": "无关商店"}])

    async def exercise():
        current = resolver(handler)
        try:
            assert await current("龙翔桥地铁站") is None
            assert await current("龙翔桥地铁站") is None
        finally:
            await current.aclose()

    asyncio.run(exercise())
    assert len(sent) == 1


HANGZHOU = {"name": "杭州市", "namedetails": {"name:zh": "杭州市", "name:en": "Hangzhou"},
            "category": "boundary", "type": "administrative", "addresstype": "city",
            "lat": "30.25", "lon": "120.15", "boundingbox": ["29.0", "30.6", "118.3", "120.8"],
            "address": {"city": "杭州市", "state": "浙江省", "country_code": "cn"}}
WEST_LAKE = {"name": "西湖", "lat": "30.2459837", "lon": "120.1431317", "place_id": 223838644,
             "display_name": "西湖, 西湖区, 杭州市, 浙江省, 中国",
             "address": {"city": "西湖区", "state": "浙江省", "country_code": "cn"}}
NAMESAKE = {"name": "杭州西湖", "lat": "22.7271967", "lon": "120.3230086",
            "address": {"city": "高雄市", "country_code": "tw"}}


def geographic_resolver(responses):
    sent = []

    def handler(request):
        sent.append(request)
        key = (request.url.params["q"], request.url.params.get("bounded") == "1")
        return httpx.Response(200, json=responses.get(key, []))

    return resolver(handler), sent


def test_target_exact_name_in_another_city_is_rejected_and_scoped_entity_is_used():
    current, sent = geographic_resolver({("杭州西湖", False): [NAMESAKE], ("杭州", False): [HANGZHOU],
                                        ("西湖", True): [NAMESAKE, WEST_LAKE]})

    async def exercise():
        try:
            return await current("杭州西湖")
        finally:
            await current.aclose()

    place = asyncio.run(exercise())
    assert (place.name, place.latitude, place.longitude) == ("西湖", 30.2459837, 120.1431317)
    assert [item.url.params["q"] for item in sent] == ["杭州西湖", "杭州", "西湖"]
    assert sent[-1].url.params["viewbox"] == "118.3,30.6,120.8,29.0"


def test_confirmed_scope_without_matching_poi_is_unknown_instead_of_a_namesake():
    current, _ = geographic_resolver({("杭州西湖", False): [NAMESAKE], ("杭州", False): [HANGZHOU],
                                     ("西湖", True): [NAMESAKE]})

    async def exercise():
        try:
            assert await current("杭州西湖") is None
        finally:
            await current.aclose()

    asyncio.run(exercise())


def test_route_endpoints_use_target_scope_without_accepting_unrelated_ranked_hits():
    wrong = {"name": "断桥", "lat": "31.0", "lon": "119.0", "address": {"city": "南京市", "country_code": "cn"}}
    bridge = {"name": "断桥", "lat": "30.2609009", "lon": "120.1470304",
              "address": {"city": "杭州市", "country_code": "cn"}}
    current, sent = geographic_resolver({("杭州西湖", False): [NAMESAKE], ("杭州", False): [HANGZHOU],
                                        ("断桥", False): [wrong], ("断桥", True): [wrong, bridge]})

    async def exercise():
        try:
            first = await current.resolve_scoped("断桥", scope="杭州西湖")
            second = await current.resolve_scoped("断桥", scope="杭州西湖")
            assert first == second
            return first
        finally:
            await current.aclose()

    place = asyncio.run(exercise())
    assert place.latitude == 30.2609009
    assert sum(item.url.params["q"] == "断桥" for item in sent) == 2


def test_scoped_cache_does_not_reuse_an_endpoint_from_another_target_city():
    nanjing = HANGZHOU | {"name": "南京市", "namedetails": {}, "boundingbox": ["31.2", "32.8", "118.0", "119.5"],
                         "address": {"city": "南京市", "country_code": "cn"}}
    bridge = {"name": "断桥", "lat": "30.2609009", "lon": "120.1470304", "address": {"city": "杭州市", "country_code": "cn"}}
    other = bridge | {"lat": "32.1", "lon": "118.8", "address": {"city": "南京市", "country_code": "cn"}}
    current, _ = geographic_resolver({("杭州", False): [HANGZHOU], ("南京", False): [nanjing],
                                     ("断桥", False): [bridge, other]})

    async def exercise():
        try:
            return [await current.resolve_scoped("断桥", scope=city) for city in ("杭州", "南京")]
        finally:
            await current.aclose()

    places = asyncio.run(exercise())
    assert [place.latitude for place in places] == [30.2609009, 32.1]


def test_endpoint_explicit_city_takes_precedence_over_target_for_a_real_intercity_route():
    shanghai = HANGZHOU | {"name": "上海市", "namedetails": {}, "boundingbox": ["30.6", "31.8", "120.8", "122.2"],
                          "address": {"city": "上海市", "country_code": "cn"}}
    station = {"name": "虹桥站", "lat": "31.2", "lon": "121.3", "address": {"city": "上海市", "country_code": "cn"}}
    current, _ = geographic_resolver({("上海虹桥站", False): [station], ("上海", False): [shanghai],
                                     ("杭州", False): [HANGZHOU]})

    async def exercise():
        try:
            return await current.resolve_scoped("上海虹桥站", scope="杭州")
        finally:
            await current.aclose()

    assert asyncio.run(exercise()).latitude == 31.2


def test_scope_requires_a_unique_administrative_entity_not_a_named_hotel():
    hotel = HANGZHOU | {"category": "tourism", "type": "hotel"}
    current, sent = geographic_resolver({("杭州西湖", False): [NAMESAKE], ("杭州", False): [hotel]})

    async def exercise():
        try:
            assert await current.resolve_scoped("断桥", scope="杭州西湖") is None
        finally:
            await current.aclose()

    asyncio.run(exercise())
    assert not any(item.url.params.get("bounded") for item in sent)


def test_unconfirmed_target_city_never_falls_back_to_an_unscoped_endpoint():
    bridge = {"name": "断桥", "lat": "31.0", "lon": "119.0", "address": {"city": "南京市", "country_code": "cn"}}
    current, _ = geographic_resolver({("断桥", False): [bridge], ("不明景点", False): []})

    async def exercise():
        try:
            assert await current.resolve_scoped("断桥", scope="不明景点") is None
        finally:
            await current.aclose()

    asyncio.run(exercise())


@pytest.mark.parametrize("failure", ["503", "timeout", "invalid-shape"])
def test_failed_city_confirmation_cannot_restore_the_exact_cross_region_namesake(failure, caplog):
    def handler(request):
        if request.url.params["q"] == "杭州西湖":
            return httpx.Response(200, json=[NAMESAKE])
        if failure == "timeout":
            raise httpx.ReadTimeout("provider failure with private raw details")
        return httpx.Response(503, text="provider busy") if failure == "503" else httpx.Response(200, json={"error": "bad response"})

    async def exercise():
        current = resolver(handler)
        try:
            assert await current("杭州西湖") is None
            assert await current.resolve_scoped("断桥", scope="杭州西湖") is None
        finally:
            await current.aclose()

    asyncio.run(exercise())
    assert "private raw details" not in caplog.text
    assert "杭州西湖" not in caplog.text


def test_candidate_city_label_cannot_override_coordinates_outside_confirmed_city_bounds():
    mislabeled = NAMESAKE | {"name": "西湖", "address": {"city": "杭州市", "country_code": "cn"}}
    current, _ = geographic_resolver({("杭州", False): [HANGZHOU], ("西湖", False): [mislabeled],
                                     ("西湖", True): [mislabeled]})

    async def exercise():
        try:
            assert await current.resolve_scoped("西湖", scope="杭州") is None
        finally:
            await current.aclose()

    asyncio.run(exercise())


@pytest.mark.parametrize("city_rows", [[], [HANGZHOU | {"category": "tourism", "type": "hotel"}],
                                     [HANGZHOU, HANGZHOU | {"address": {"city": "杭州市", "country_code": "tw"}}]],
                         ids=["empty", "non-administrative", "ambiguous-administrative"])
def test_successful_but_unconfirmed_city_lookup_cannot_accept_a_direct_namesake(city_rows):
    current, _ = geographic_resolver({("杭州西湖", False): [NAMESAKE], ("杭州", False): city_rows})

    async def exercise():
        try:
            assert await current("杭州西湖") is None
            assert await current("杭州西湖") is None
        finally:
            await current.aclose()

    asyncio.run(exercise())


def test_unqualified_long_endpoint_can_still_use_an_independently_confirmed_target_city():
    station = {"name": "龙翔桥", "lat": "30.257", "lon": "120.163",
               "address": {"city": "杭州市", "country_code": "cn"}}
    current, _ = geographic_resolver({("龙翔桥地铁站", False): [station], ("杭州", False): [HANGZHOU]})

    async def exercise():
        try:
            assert await current("龙翔桥地铁站") is None
            return await current.resolve_scoped("龙翔桥地铁站", scope="杭州")
        finally:
            await current.aclose()

    place = asyncio.run(exercise())
    assert place.latitude == 30.257 and place.longitude == 120.163


@pytest.mark.parametrize("coordinates", [(91, 120), (30, 181), (float("nan"), 120), (30, float("inf")), (10 ** 400, 120)])
def test_out_of_range_or_nonfinite_geocoder_coordinates_are_unknown(coordinates):
    row = ROW | {"lat": coordinates[0], "lon": coordinates[1]}
    assert NominatimPlaceResolver._first_poi("上海迪士尼乐园", [row]) is None


def test_missing_address_cannot_skip_city_confirmation_for_a_cross_region_target():
    current, _ = geographic_resolver({("杭州西湖", False): [NAMESAKE | {"address": None}]})

    async def exercise():
        try:
            assert await current("杭州西湖") is None
        finally:
            await current.aclose()

    asyncio.run(exercise())


def test_inherited_confirmed_city_can_retry_a_qualified_endpoint_as_its_exact_bare_entity():
    bridge = {"name": "断桥", "lat": "30.2609009", "lon": "120.1470304",
              "address": {"city": "杭州市", "country_code": "cn"}}
    current, sent = geographic_resolver({("杭州", False): [HANGZHOU], ("断桥", True): [bridge]})

    async def exercise():
        try:
            return await current.resolve_scoped("杭州断桥", scope="杭州")
        finally:
            await current.aclose()

    assert asyncio.run(exercise()).latitude == 30.2609009
    assert sent[-1].url.params["q"] == "断桥" and sent[-1].url.params["bounded"] == "1"
