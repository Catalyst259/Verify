"""Use the native multimodal adapter with local model responses; no external requests."""

import asyncio
from copy import deepcopy
import json
import os
import traceback
from types import SimpleNamespace

import browser_use
import httpx
from openai import AsyncOpenAI
import pytest

from backend.common.errors import ExtractionFailed, ExtractionSourceMismatch
from backend.extraction import agent
from backend.extraction.materials import LinkMaterial
from backend.extraction.models import ClaimExtractionResult
from backend.storage.models import StoredImage


LINK = "https://www.xiaohongshu.com/explore/" + "a" * 24 + "?xsec_token=test-only"
IMAGES = [StoredImage("file-red", "image/png", b"red"), StoredImage("file-blue", "image/png", b"blue")]
MATERIAL = LinkMaterial(LINK, LINK.split("?", 1)[0], "公园", "有饮水机", (b"note-photo",))


def result(sources=None):
    return {"target_place": "公园", "claims": [
        {"claim_id": "original-a", "type": "FACT", "content": "标牌注明免费开放。", "sources": sources or [
            {"source_type": "IMAGE", "source_ref": "file-red", "source_text": "免费开放"},
            {"source_type": "IMAGE", "source_ref": "photo_2", "source_text": "蓝色标牌"}]},
        {"claim_id": "original-b", "type": "CROWD", "content": "工作日上午游客少。", "sources": [
            {"source_type": "TEXT", "source_ref": "ignored-text-label", "source_text": "工作日上午人少"}]},
        {"claim_id": "original-c", "type": "FACT", "content": "有饮水机。", "sources": [
            {"source_type": "LINK", "source_ref": MATERIAL.canonical_url, "source_text": "有饮水机"}]},
    ]}


def repairs():
    return {"corrections": [
        {"claim_index": 0, "source_index": 1, "source_type": "IMAGE", "source_ref": "file-blue"},
        {"claim_index": 2, "source_index": 0, "source_type": "LINK", "source_ref": LINK},
    ]}


def stub_native(monkeypatch, responses, *, model="deepseek-flash", timeout=10, initial_delay=0,
                repair_delay=0, provider_status=200):
    calls, agents, browsers = [], [], []

    async def respond(request):
        body = json.loads(request.content)
        calls.append(body)
        if len(calls) == 2 and repair_delay:
            await asyncio.sleep(repair_delay)
        if len(calls) == 2 and provider_status != 200:
            return httpx.Response(provider_status, json={"error": {"message": "secret-provider-body"}})
        content = responses[len(calls) - 1]
        return httpx.Response(200, json={"id": "test", "object": "chat.completion", "created": 0,
            "model": model, "choices": [{"index": 0, "finish_reason": "stop", "message": {
                "role": "assistant", "content": json.dumps(content, ensure_ascii=False) if not isinstance(content, str) else content}}]})

    def client(self):
        if len(calls) == 1:
            assert self.max_retries == 0
        return AsyncOpenAI(**self._get_client_params(),
                           http_client=httpx.AsyncClient(transport=httpx.MockTransport(respond)))

    class Browser:
        def __init__(self, **kwargs):
            self.closed = False
            browsers.append(self)

        async def kill(self):
            self.closed = True

    class Agent:
        def __init__(self, *, llm, task, **kwargs):
            self.llm, self.task = llm, task
            self.settings = SimpleNamespace(use_vision=False)
            agents.append(self)

        async def run(self, **kwargs):
            from browser_use.llm.messages import UserMessage
            if initial_delay:
                await asyncio.sleep(initial_delay)
            response = await self.llm.ainvoke([UserMessage(content=self.task)], output_format=ClaimExtractionResult)
            return SimpleNamespace(is_successful=lambda: True, final_result=response.completion.model_dump_json)

    monkeypatch.setattr(browser_use, "Browser", Browser)
    monkeypatch.setattr(browser_use, "Agent", Agent)
    monkeypatch.setattr(browser_use.ChatOpenAI, "get_client", client)
    monkeypatch.setattr(agent, "load_config", lambda: {
        "api_key": "test-only", "base_url": "https://model.test/v1", "model": model,
        "browser_executable_path": "", "timeout_seconds": timeout, "max_steps": 2})
    return calls, agents, browsers


def extract():
    return agent.extract_claims("公园", "工作日上午人少", [LINK], IMAGES, link_materials=(MATERIAL,))


@pytest.mark.parametrize("model", ["deepseek-flash", "gpt-4.1"])
def test_native_source_repair_preserves_claims_and_uses_the_original_multimodal_materials(monkeypatch, model):
    original = result()
    calls, agents, browsers = stub_native(monkeypatch, [original, repairs()], model=model)
    output = asyncio.run(extract())
    expected = deepcopy(original)
    expected["claims"][0]["sources"][1]["source_ref"] = "file-blue"
    expected["claims"][1]["sources"][0]["source_ref"] = None
    expected["claims"][2]["sources"][0]["source_ref"] = LINK
    assert output.model_dump() == expected
    assert len(calls) == 2 and len(agents) == len(browsers) == 1 and browsers[0].closed
    assert json.loads(agents[0].task)["allowed_source_refs"] == {
        "TEXT": [None], "IMAGE": ["file-red", "file-blue"], "LINK": [LINK]}
    feedback = json.loads(calls[1]["messages"][2]["content"])["source_repair_feedback"]
    assert [(item["claim_index"], item["source_index"]) for item in feedback] == [(0, 1), (2, 0)]
    assert "photo_2" not in json.dumps(calls[1]["messages"], ensure_ascii=False)
    for body in calls:
        image_parts = [part for message in body["messages"] if isinstance(message["content"], list)
                       for part in message["content"] if part["type"] == "image_url"]
        assert len(image_parts) == 3
        assert "有饮水机" in json.dumps(body["messages"], ensure_ascii=False)
    if model == "deepseek-flash":
        assert calls[1]["response_format"] == {"type": "json_object"}
        assert calls[1]["thinking"] == {"type": "disabled"}
    else:
        assert calls[1]["response_format"]["json_schema"]["schema"]["properties"].keys() == {"corrections"}


def test_legal_sources_and_known_note_photos_do_not_call_a_repair_model(monkeypatch):
    original = result([
        {"source_type": "IMAGE", "source_ref": "file-red", "source_text": "免费开放"},
        {"source_type": "IMAGE", "source_ref": LINK, "source_text": "笔记配图"}])
    original["claims"][2]["sources"][0]["source_ref"] = LINK
    calls, agents, browsers = stub_native(monkeypatch, [original])
    output = asyncio.run(extract())
    assert output.claims[0].sources[1].source_type == "LINK"
    assert output.claims[0].sources[1].source_ref == LINK
    assert output.claims[1].sources[0].source_ref is None
    assert len(calls) == 1 and browsers[0].closed


@pytest.mark.parametrize("mode", ["unknown_ref", "missing", "duplicate", "unknown_position", "modify_claim", "invalid_json"])
def test_native_repair_cannot_bypass_source_identity_or_frozen_fields_and_has_one_attempt(monkeypatch, mode):
    correction = repairs()
    if mode == "unknown_ref":
        correction["corrections"][0]["source_ref"] = "invented-photo"
    elif mode == "missing":
        correction["corrections"].pop()
    elif mode == "duplicate":
        correction["corrections"].append(correction["corrections"][0])
    elif mode == "unknown_position":
        correction["corrections"][0]["claim_index"] = 1
    elif mode == "modify_claim":
        correction["corrections"][0]["claim_content"] = "tampered"
    else:
        correction = "{invalid"
    calls, agents, browsers = stub_native(monkeypatch, [result(), correction])
    with pytest.raises(ExtractionFailed) as error:
        asyncio.run(extract())
    assert len(calls) == 2 and browsers[0].closed
    assert "invented-photo" not in str(error.value) and "photo_2" not in str(error.value)
    assert "tampered" not in str(error.value) and "invalid" not in str(error.value)


def test_repair_provider_failure_is_safe_and_not_retried(monkeypatch):
    calls, agents, browsers = stub_native(monkeypatch, [result(), repairs()], provider_status=502)
    with pytest.raises(ExtractionFailed, match="来源纠正失败") as error:
        asyncio.run(extract())
    assert len(calls) == 2 and browsers[0].closed
    assert "secret-provider-body" not in str(error.value) and error.value.__cause__ is None


def test_correction_shares_the_original_deadline_and_cleanup_still_runs(monkeypatch):
    calls, agents, browsers = stub_native(monkeypatch, [result(), repairs()], timeout=0.3,
                                        initial_delay=0.2, repair_delay=0.2)
    with pytest.raises(TimeoutError):
        asyncio.run(extract())
    assert len(calls) == 2 and browsers[0].closed


def test_single_image_does_not_get_guessed_when_the_only_correction_is_still_wrong(monkeypatch):
    original = result([{ "source_type": "IMAGE", "source_ref": "picture-name", "source_text": "免费开放"}])
    original["claims"] = original["claims"][:1]
    calls, agents, browsers = stub_native(monkeypatch, [original, {"corrections": [{
        "claim_index": 0, "source_index": 0, "source_type": "IMAGE", "source_ref": "still-wrong"}]}])
    with pytest.raises(ExtractionSourceMismatch):
        asyncio.run(agent.extract_claims("公园", None, [], IMAGES[:1]))
    assert len(calls) == 2 and browsers[0].closed
    assert json.loads(agents[0].task)["allowed_source_refs"] == {
        "TEXT": [], "IMAGE": ["file-red"], "LINK": []}


def test_final_history_schema_failure_does_not_expose_raw_model_fields(monkeypatch):
    calls, agents, browsers = stub_native(monkeypatch, [])
    base_agent = browser_use.Agent

    class InvalidHistoryAgent(base_agent):
        async def run(self, **kwargs):
            return SimpleNamespace(is_successful=lambda: True, final_result=lambda: json.dumps({
                "target_place": "公园", "claims": [{"claim_id": "bad", "type": "RAW_SECRET_TYPE",
                    "content": "不得记入日志", "sources": [{"source_type": "IMAGE",
                        "source_ref": "RAW_SECRET_REF", "source_text": None}]}]}))

    monkeypatch.setattr(browser_use, "Agent", InvalidHistoryAgent)
    with pytest.raises(ExtractionFailed, match="无效的提取结果") as error:
        asyncio.run(extract())
    trace = "".join(traceback.format_exception(error.value))
    assert "RAW_SECRET_TYPE" not in trace and "RAW_SECRET_REF" not in trace
    assert not calls and browsers[0].closed


def test_cleanup_keeps_a_separate_bounded_three_second_limit(monkeypatch):
    original = result([{ "source_type": "IMAGE", "source_ref": "file-red", "source_text": "免费开放"}])
    original["claims"][2]["sources"][0]["source_ref"] = LINK
    calls, agents, browsers = stub_native(monkeypatch, [original])
    base_browser = browser_use.Browser

    class SlowCleanupBrowser(base_browser):
        async def kill(self):
            try:
                await asyncio.Event().wait()
            finally:
                self.closed = True

    monkeypatch.setattr(browser_use, "Browser", SlowCleanupBrowser)
    with pytest.warns(RuntimeWarning, match="清理未在限定时间内完成"):
        output = asyncio.run(extract())
    assert output.claims and len(calls) == 1 and browsers[0].closed


@pytest.mark.skipif(os.getenv("VERIFY_BROWSER_TESTS") != "1", reason="显式启用 Chromium 集成测试")
@pytest.mark.parametrize("model", ["gpt-4.1", "deepseek-flash"])
def test_real_native_browser_agent_done_is_followed_by_one_source_only_repair(monkeypatch, model):
    from fastapi import FastAPI, Request
    from playwright.sync_api import sync_playwright
    from test_api import png
    from test_browser import serve

    app, calls = FastAPI(), []
    original = result()
    original["claims"] = original["claims"][:1]
    original["claims"][0]["sources"] = [{"source_type": "IMAGE", "source_ref": "image_1",
                                         "source_text": "红色图片"}]

    @app.post("/v1/chat/completions")
    async def completion(request: Request):
        body = await request.json()
        calls.append(body)
        payload = ({"evaluation_previous_goal": "Success", "memory": "本地模拟提取",
                    "next_goal": "完成", "thinking": "Scripted integration response.",
                    "action": [{"done": {"success": True, "data": original}}]}
                   if len(calls) == 1 else {"corrections": [{
                       "claim_index": 0, "source_index": 0, "source_type": "IMAGE", "source_ref": "file-red"}]})
        return {"id": "test", "object": "chat.completion", "created": 0, "model": model,
                "choices": [{"index": 0, "finish_reason": "stop", "message": {
                    "role": "assistant", "content": json.dumps(payload, ensure_ascii=False)}}]}

    with sync_playwright() as playwright:
        executable = os.getenv("VERIFY_CHROMIUM") or playwright.chromium.executable_path
    with serve(app) as base_url:
        monkeypatch.setattr(agent, "load_config", lambda: {
            "api_key": "test-only", "model": model, "base_url": base_url + "/v1",
            "browser_executable_path": executable, "timeout_seconds": 60, "max_steps": 3})
        output = asyncio.run(agent.extract_claims("公园", None, [], [StoredImage("file-red", "image/png", png("red"))]))
    assert output.claims[0].sources[0].source_ref == "file-red"
    assert output.claims[0].sources[0].source_text == "红色图片"
    assert output.claims[0].content == original["claims"][0]["content"]
    assert len(calls) == 2
    assert len([part for message in calls[1]["messages"] if isinstance(message["content"], list)
                for part in message["content"] if part["type"] == "image_url"]) == 1
