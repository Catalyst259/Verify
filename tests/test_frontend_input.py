"""真实 Chromium 与本地 API 的材料校验；提取器替身不访问模型或公网。"""
import os
from unittest.mock import patch

import pytest

from backend.extraction.models import ClaimExtractionResult
from backend.verification.capabilities import VerificationCapabilities

# 默认 app 在模块导入时读取配置，测试用空配置避免读取本地模型密钥。
with patch("backend.extraction.agent.read_config", return_value={}):
    from test_api import png
    from test_browser import serve

    from backend import main


pytestmark = pytest.mark.skipif(
    os.getenv("VERIFY_BROWSER_TESTS") != "1", reason="显式启用 Chromium 集成测试"
)


@pytest.mark.parametrize("material", ["none", "whitespace", "text", "link", "image"])
def test_frontend_requires_one_material_and_explains_empty_claims(tmp_path, material):
    from playwright.sync_api import sync_playwright

    calls = []

    async def extract(target_place, description_text, links, images):
        calls.append((target_place, description_text, links, images))
        return ClaimExtractionResult(target_place=target_place, claims=[])

    app = main.create_app(
        tmp_path, extract, subgraphs={}, capabilities=VerificationCapabilities()
    )
    with sync_playwright() as playwright, serve(app) as base_url:
        executable = os.getenv("VERIFY_CHROMIUM") or playwright.chromium.executable_path
        browser = playwright.chromium.launch(executable_path=executable, headless=True)
        try:
            page = browser.new_page()
            posts = []
            page.on("request", lambda request: posts.append(request) if (
                request.method == "POST" and request.url == base_url + "/api/verifications"
            ) else None)
            page.goto(base_url)
            page.locator("#place").fill("测试公园")
            assert "地点仅用于限定核验范围" in page.locator("#place-hint").inner_text()
            if material == "whitespace":
                page.locator("#description").fill(" \n\t ")
                page.locator("#links").fill(" \n\t ")
            elif material == "text":
                page.locator("#description").fill("测试公园很漂亮")
            elif material == "link":
                page.locator("#links").fill(base_url + "/article")
            elif material == "image":
                page.locator("#images").set_input_files({
                    "name": "image.png", "mimeType": "image/png", "buffer": png(),
                })
                page.wait_for_function(
                    "document.querySelectorAll('#uploads li').length === 1 && "
                    "!document.querySelector('#submit').disabled"
                )
            if material in {"none", "whitespace"}:
                page.locator("#result").evaluate("result => { result.hidden = false; }")
                page.locator("#submit").click()
                assert page.locator("#status").inner_text() == "请至少提供一种材料：文字、图片或链接。"
                assert page.locator("#result").is_hidden()
                assert not posts and not calls
            else:
                page.locator("#submit").click()
                page.wait_for_function(
                    "!document.querySelector('#fields').disabled && "
                    "!document.querySelector('#result').hidden"
                )
                assert len(posts) == len(calls) == 1
                target_place, text, links, images = calls[0]
                assert target_place == "测试公园"
                assert text == ("测试公园很漂亮" if material == "text" else None)
                assert links == ([base_url + "/article"] if material == "link" else [])
                assert len(images) == (1 if material == "image" else 0)
                assert "请补充包含具体说法的文字、图片或链接" in page.locator("#status").inner_text()
        finally:
            browser.close()
