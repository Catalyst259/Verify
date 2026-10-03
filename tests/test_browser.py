"""真实前端 + API + SQLite + browser-use + Chromium；仅模型 HTTP 响应使用测试替身。"""
import base64
from contextlib import contextmanager
import json
import os
from pathlib import Path
import socket
import threading
import time

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
import pytest
import uvicorn

from backend import agent, main
from backend.storage.repository import StorageRepository
from test_api import png

pytestmark = pytest.mark.skipif(os.getenv("VERIFY_BROWSER_TESTS") != "1", reason="显式启用 Chromium 集成测试")


@contextmanager
def serve(app):
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        server = uvicorn.Server(uvicorn.Config(app, log_level="error"))
        thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
        thread.start()
        try:
            deadline = time.monotonic() + 10
            while not server.started:
                if not thread.is_alive() or time.monotonic() > deadline:
                    raise RuntimeError("测试服务器未启动")
                time.sleep(0.05)
            yield f"http://127.0.0.1:{sock.getsockname()[1]}"
        finally:
            server.should_exit = True
            thread.join(timeout=10)


@pytest.mark.parametrize("with_links,model", [(False, "gpt-4.1"), (True, "gpt-4.1"), (True, "deepseek-flash")])
def test_drag_upload_to_real_browser_agent(tmp_path, monkeypatch, with_links, model):
    from playwright.sync_api import sync_playwright

    app = FastAPI()
    requests = []
    provider_calls = []
    page_visits = []

    @app.middleware("http")
    async def capture(request: Request, call_next):
        if request.url.path == "/api/verifications":
            requests.append(await request.json())
        return await call_next(request)

    @app.get("/article/{number}", response_class=HTMLResponse)
    def article(number: int):
        page_visits.append(number)
        return f'<html lang="zh-CN"><body><h1>测试公园</h1><p>PAGE_MARKER_{number} 工作日上午人少。</p></body></html>'

    @app.post("/v1/chat/completions")
    async def completion(request: Request):
        body = await request.json()
        provider_calls.append(body)
        step = len(provider_calls)
        if with_links and step <= 2:
            action = {"navigate": {"url": f"{base_url}/article/{step}", "new_tab": False}}
        else:
            sources = [{"source_type": "IMAGE", "source_ref": code, "source_text": "测试图片"}
                       for code in requests[-1]["image"]]
            sources.append({"source_type": "TEXT", "source_ref": None, "source_text": "工作日上午人少"})
            sources.extend({"source_type": "LINK", "source_ref": link, "source_text": "工作日上午人少"}
                           for link in requests[-1]["link"])
            action = {"done": {"success": True, "data": {
                "target_place": "测试公园", "claims": [{
                    "claim_id": "claim_001", "type": "CROWD", "content": "测试公园工作日上午游客较少。",
                    "sources": sources,
                }],
            }}}
        content = {"evaluation_previous_goal": "Success", "memory": "测试材料", "next_goal": "读取或完成",
                   "thinking": "Scripted test response; not a real model extraction.", "action": [action]}
        return {"id": "test", "object": "chat.completion", "created": 0, "model": "gpt-4.1",
                "choices": [{"index": 0, "message": {"role": "assistant", "content": json.dumps(content)},
                             "finish_reason": "stop"}]}

    app.mount("/", main.create_app(tmp_path))
    # 挂载的子应用不会自动运行 lifespan。
    StorageRepository(tmp_path).initialize()
    with sync_playwright() as playwright, serve(app) as base_url:
        executable = os.getenv("VERIFY_CHROMIUM") or playwright.chromium.executable_path
        monkeypatch.setattr(agent, "load_config", lambda: {
            "api_key": "test-only", "model": model, "base_url": base_url + "/v1",
            "browser_executable_path": executable, "timeout_seconds": 90, "max_steps": 4,
        })
        browser = playwright.chromium.launch(executable_path=executable, headless=True)
        page = browser.new_page()
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        try:
            page.goto(base_url)
            page.locator("#place").fill("测试公园")
            page.locator("#description").fill("工作日上午人少")
            links = [base_url + "/article/1", base_url + "/article/2"] if with_links else []
            page.locator("#links").fill("\n".join(links))
            encoded_images = [base64.b64encode(png(color)).decode() for color in ("red", "blue")]
            page.locator("#dropzone").evaluate("""(zone, images) => {
              const transfer = new DataTransfer();
              images.forEach((image, i) => transfer.items.add(new File(
                [Uint8Array.from(atob(image), c => c.charCodeAt(0))], `${i}.png`, {type: 'image/png'})));
              zone.dispatchEvent(new DragEvent('drop', {dataTransfer: transfer, bubbles: true}));
            }""", encoded_images)
            page.wait_for_function("document.querySelectorAll('#uploads li').length === 2 && !document.querySelector('#submit').disabled")
            page.locator("#submit").click()
            page.wait_for_function("!document.querySelector('#fields').disabled", timeout=120_000)
            assert page.locator("#result").is_visible(), page.locator("#status").inner_text()
            output = json.loads(page.locator("#claims").inner_text())
            assert output["target_place"] == "测试公园"
            assert output["claims"][0]["claim_id"] == "claim_001"
            assert requests[0]["link"] == links
            assert requests[0]["text"] == "工作日上午人少"
            assert len(provider_calls) == (3 if with_links else 1)
            assert page_visits == ([1, 2] if with_links else [])
            for call in provider_calls:
                messages = call["messages"]
                assert Path("prompt.md").read_text() in messages[0]["content"]
                image_content = next(message["content"] for message in messages
                                     if isinstance(message["content"], list)
                                     and message["content"][0].get("text", "").startswith("用户上传图片"))
                assert [part["image_url"]["url"].split(",", 1)[1] for part in image_content if part["type"] == "image_url"] == encoded_images
                assert [part["text"].split(" = ")[1] for part in image_content if part["type"] == "text"] == requests[0]["image"]
                if model == "deepseek-flash":
                    assert call["response_format"] == {"type": "json_object"}
                    schema = json.loads(messages[-1]["content"].split("JSON：", 1)[1])
                else:
                    schema = call["response_format"]["json_schema"]["schema"]
                assert '"search"' not in json.dumps(schema)
            if with_links:
                for index in (1, 2):
                    assert f"PAGE_MARKER_{index}" in json.dumps(provider_calls[index]["messages"])
                    # 除上传原图外，模型也确实收到浏览器页面截图（包括 DeepSeek）。
                    all_images = [part for message in provider_calls[index]["messages"]
                                  if isinstance(message["content"], list) for part in message["content"]
                                  if part["type"] == "image_url"]
                    assert len(all_images) > len(encoded_images)
            assert not errors
        finally:
            browser.close()
