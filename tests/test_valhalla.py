import asyncio
import json
import time

import httpx
import pytest

from backend.sources import valhalla
from backend.sources.valhalla import ValhallaError, ValhallaRouting
from backend.verification.capabilities import Coordinates, MatrixResult, RouteResult

ORIGIN = Coordinates(31.2304, 121.4737)
STATION = Coordinates(31.2365, 121.4801)


def respond(request):
    body = json.loads(request.url.params["json"])
    if request.url.path == "/route":
        return httpx.Response(200, json={"trip": {"summary": {"time": 312.4, "length": 0.42}}})
    if request.url.path == "/sources_to_targets":
        table = [[{"time": 0.0, "distance": 0.0}, {"time": 120.0, "distance": 0.15}],
                 [None, {"time": 240.0, "distance": 0.3}]]
        return httpx.Response(200, json={"sources_to_targets": table})
    if request.url.path == "/isochrone":
        if body["contours"][0]["time"] == 999:
            return httpx.Response(200, json={"features": []})
        return httpx.Response(200, json={"features": [
            {"geometry": {"type": "Polygon", "coordinates": [[[121.47, 31.23], [121.48, 31.23], [121.48, 31.24],
                                                              [121.47, 31.23]]]}}]})
    return httpx.Response(404, json={"error": "no endpoint"})


def routing(**kwargs):
    kwargs.setdefault("min_request_interval_seconds", 0)
    return ValhallaRouting("https://routing.test", user_agent="Verify/0.1 (+https://example.test/verify)",
                           client=httpx.AsyncClient(transport=httpx.MockTransport(respond)), **kwargs)


def test_route_reports_walking_duration_and_distance_in_meters():
    result = asyncio.run(routing().route(ORIGIN, STATION, "pedestrian"))
    assert result == RouteResult(costing="pedestrian", duration_seconds=312.4, distance_meters=420.0)


def test_requests_state_the_costing_and_default_kilometer_units():
    seen = []

    def capture(request):
        seen.append(json.loads(request.url.params["json"]))
        return respond(request)

    source = ValhallaRouting("https://routing.test", user_agent="Verify/0.1", min_request_interval_seconds=0,
                             client=httpx.AsyncClient(transport=httpx.MockTransport(capture)))
    asyncio.run(source.route(ORIGIN, STATION, "pedestrian"))
    asyncio.run(source.isochrone(ORIGIN, "pedestrian", 5))
    assert seen[0] == {"locations": [{"lat": 31.2304, "lon": 121.4737}, {"lat": 31.2365, "lon": 121.4801}],
                       "costing": "pedestrian"}
    assert seen[1] == {"locations": [{"lat": 31.2304, "lon": 121.4737}], "costing": "pedestrian",
                       "polygons": True, "contours": [{"time": 5}]}


def test_matrix_returns_each_source_target_pair_and_marks_unreachable_as_none():
    result = asyncio.run(routing().matrix([ORIGIN, STATION], [ORIGIN, STATION], "pedestrian"))
    assert result == MatrixResult(
        costing="pedestrian",
        durations_seconds=((0.0, 120.0), (None, 240.0)),
        distances_meters=((0.0, 150.0), (None, 300.0)),
    )


def test_isochrone_expresses_the_area_walkable_within_the_time_budget():
    result = asyncio.run(routing().isochrone(ORIGIN, "pedestrian", 5))
    assert result.costing == "pedestrian" and result.minutes == 5
    assert result.rings == ((Coordinates(31.23, 121.47), Coordinates(31.23, 121.48), Coordinates(31.24, 121.48),
                             Coordinates(31.23, 121.47)),)


def test_isochrone_without_reachable_area_surfaces_an_error():
    with pytest.raises(ValhallaError, match="可达范围"):
        asyncio.run(routing().isochrone(ORIGIN, "pedestrian", 999))


def test_http_error_surfaces_the_service_message_instead_of_a_duration():
    def fail(request):
        return httpx.Response(400, json={"error": "Failed to parse json request"})

    client = httpx.AsyncClient(transport=httpx.MockTransport(fail))
    source = ValhallaRouting("https://routing.test", user_agent="Verify/0.1", client=client,
                             min_request_interval_seconds=0)
    with pytest.raises(ValhallaError, match="Failed to parse json request"):
        asyncio.run(source.route(ORIGIN, STATION, "auto"))


def test_connection_failure_surfaces_an_error_instead_of_a_duration():
    def fail(request):
        raise httpx.ConnectError("connection refused")

    client = httpx.AsyncClient(transport=httpx.MockTransport(fail))
    source = ValhallaRouting("https://routing.test", user_agent="Verify/0.1", client=client,
                             min_request_interval_seconds=0)
    with pytest.raises(ValhallaError, match="ConnectError"):
        asyncio.run(source.isochrone(ORIGIN, "pedestrian", 5))


def test_repeated_identical_requests_are_served_from_cache():
    calls = []

    def count(request):
        calls.append(request.url.path)
        return respond(request)

    source = ValhallaRouting("https://routing.test", user_agent="Verify/0.1", min_request_interval_seconds=0,
                             client=httpx.AsyncClient(transport=httpx.MockTransport(count)))
    asyncio.run(source.route(ORIGIN, STATION, "bicycle"))
    asyncio.run(source.route(ORIGIN, STATION, "bicycle"))
    assert calls == ["/route"]


def test_distinct_requests_are_spaced_one_per_second_window():
    stamps = []

    def capture(request):
        stamps.append(time.monotonic())
        return respond(request)

    source = ValhallaRouting("https://routing.test", user_agent="Verify/0.1", min_request_interval_seconds=0.05,
                             client=httpx.AsyncClient(transport=httpx.MockTransport(capture)))

    async def both():
        await source.route(ORIGIN, STATION, "pedestrian")
        await source.route(STATION, ORIGIN, "pedestrian")

    asyncio.run(both())
    assert stamps[1] - stamps[0] >= 0.05


def test_request_carries_the_application_user_agent_to_the_configured_service(monkeypatch):
    seen = []
    real_client = httpx.AsyncClient

    def capture(request):
        seen.append(request)
        return respond(request)

    def client(**kwargs):
        return real_client(transport=httpx.MockTransport(capture), **kwargs)

    monkeypatch.setattr(valhalla.httpx, "AsyncClient", client)
    source = valhalla.ValhallaRouting.from_config({"valhalla": {
        "base_url": "https://valhalla.example.test/", "user_agent": "Verify/0.1 (+contact)",
        "referer": "http://localhost:8000/"}})
    try:
        asyncio.run(source.route(ORIGIN, STATION, "auto"))
    finally:
        asyncio.run(source.aclose())
    assert seen[0].headers["user-agent"] == "Verify/0.1 (+contact)"
    assert seen[0].headers["referer"] == "http://localhost:8000/"
    assert str(seen[0].url).startswith("https://valhalla.example.test/route?")


def test_default_user_agent_identifies_this_application():
    source = ValhallaRouting.from_config({"valhalla": {"base_url": "https://valhalla.example.test"}})
    assert "verify-app" in source.user_agent and "httpx" not in source.user_agent
