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
       "display_name": "上海迪士尼乐园, 川沙新镇, 浦东新区, 上海市, 中国", "place_id": 566792571}


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


def test_identical_queries_share_one_request(tmp_path):
    sent = []

    def handler(request):
        sent.append(str(request.url))
        return httpx.Response(200, json=[ROW | {"name": "武康大楼", "lat": "31.20113", "lon": "121.43159",
            "display_name": "武康大楼, 徐汇区, 上海市, 中国"}])

    place_resolver = resolver(handler)

    async def exercise():
        return [await place_resolver("武康大楼") for _ in range(3)]

    places = asyncio.run(exercise())
    assert [place.latitude for place in places] == [31.20113] * 3
    assert len(sent) == 1


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
    assert len(sent) == 2
    assert sent[1] - sent[0] >= 0.2


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


def test_exact_cross_region_namesake_still_needs_independent_geographical_context():
    row = {"name": "杭州西湖", "lat": "22.7271967", "lon": "120.3230086",
           "address": {"city": "高雄市", "country_code": "tw"}}
    assert NominatimPlaceResolver._first_poi("杭州西湖", [row]) is not None
