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
    assert dict(request.url.params) == {"q": "上海迪士尼乐园", "format": "jsonv2", "accept-language": "zh-CN"}
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
        return httpx.Response(200, json=[ROW])

    place_resolver = resolver(handler)

    async def exercise():
        return [await place_resolver("武康大楼") for _ in range(3)]

    places = asyncio.run(exercise())
    assert [place.latitude for place in places] == [31.1440374] * 3
    assert len(sent) == 1


def test_the_process_sends_at_most_one_request_per_interval():
    sent = []

    def handler(request):
        sent.append(monotonic())
        return httpx.Response(200, json=[ROW])

    place_resolver = resolver(handler, interval=0.2)

    async def exercise():
        for query in ("武康大楼", "上海海昌海洋公园"):
            await place_resolver(query)

    asyncio.run(exercise())
    assert len(sent) == 2
    assert sent[1] - sent[0] >= 0.2
