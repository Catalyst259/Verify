"""真实 Chromium 与本地 API 的材料校验；提取器替身不访问模型或公网。"""
import os
from unittest.mock import patch

import pytest

from backend.extraction.models import ClaimExtractionResult
from backend.verification.capabilities import VerificationCapabilities
from test_link_materials import NoteReader, NOTE

# 默认 app 在模块导入时读取配置，测试用空配置避免读取本地模型密钥。
with patch("backend.extraction.agent.read_config", return_value={}):
    from backend import main
    from test_api import png
    from test_browser import serve


pytestmark = pytest.mark.skipif(
    os.getenv("VERIFY_BROWSER_TESTS") != "1", reason="显式启用 Chromium 集成测试"
)


@pytest.mark.parametrize("material", ["none", "whitespace", "text", "link", "image"])
def test_frontend_requires_one_material_and_explains_empty_claims(tmp_path, material):
    from playwright.sync_api import sync_playwright

    calls = []

    async def extract(target_place, description_text, links, images, **kwargs):
        calls.append((target_place, description_text, links, images))
        return ClaimExtractionResult(target_place=target_place, claims=[])

    app = main.create_app(
        tmp_path, extract, subgraphs={}, capabilities=VerificationCapabilities(evidence_sources={'xiaohongshu': NoteReader()})
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
                page.locator("#links").fill(NOTE)
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
                assert links == ([NOTE] if material == "link" else [])
                assert len(images) == (1 if material == "image" else 0)
                assert "请补充包含具体说法的文字、图片或链接" in page.locator("#status").inner_text()
        finally:
            browser.close()


def test_share_text_parser_preserves_signed_urls_and_rejects_other_sites(tmp_path):
    from playwright.sync_api import sync_playwright
    from test_link_materials import SHORT

    app = main.create_app(tmp_path, subgraphs={}, capabilities=VerificationCapabilities())
    with sync_playwright() as playwright, serve(app) as base_url:
        browser = playwright.chromium.launch(executable_path=os.getenv('VERIFY_CHROMIUM'), headless=True)
        try:
            page = browser.new_page()
            page.goto(base_url)
            signed = NOTE + '?xsec_token=Abc_123==&xsec_source=pc'
            for text, expected in [('', []), (' \n ', []), (f'分享笔记 {SHORT}，复制查看', [SHORT]),
                (f'第一篇：{signed}\n第二篇 {SHORT}。重复 {signed}', [signed, SHORT])]:
                assert page.evaluate('text => parseXiaohongshuLinks(text)', text) == expected
            for text in ['分享文案没有链接', 'https://example.com/a', NOTE + ':8443',
                         f'{SHORT}\nhttps://evil.test/', 'https://www.xiaohongshu.com/user/profile/a']:
                result = page.evaluate('''text => {
                    try {parseXiaohongshuLinks(text); return 'accepted';} catch(e) {return e.message;}
                }''', text)
                assert result != 'accepted'
            page.locator('#place').fill('公园')
            page.locator('#links').fill('https://example.com/a')
            posts = []
            page.on('request', lambda request: posts.append(request) if request.method == 'POST' else None)
            page.locator('#submit').click()
            assert '小红书' in page.locator('#status').inner_text()
            assert not posts and page.locator('#result').is_hidden()
        finally:
            browser.close()
