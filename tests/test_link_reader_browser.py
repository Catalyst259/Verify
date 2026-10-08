"""系统浏览器 + 合成小红书 DOM，验证轮播读取及跳转边界；不访问公网。"""

import asyncio
import base64
from datetime import datetime, timedelta, timezone
from io import BytesIO
import os
from urllib.parse import urlsplit

from PIL import Image
import pytest

from backend.common.errors import LinkReadError
from backend.sources.xiaohongshu import HOME, XiaohongshuSource
from test_api import png
from test_link_materials import NOTE, SHORT

pytestmark = pytest.mark.skipif(os.getenv('VERIFY_BROWSER_TESTS') != '1', reason='显式启用系统 Chrome')


def detail(*, content='免费开放', broken=False, video=False):
    red, blue = (base64.b64encode(png(color)).decode() for color in ('red', 'blue'))
    second = 'data:image/png;base64,invalid' if broken else 'data:image/png;base64,' + blue
    return f'''<html><head><style>
    .swiper-slide {{ display:none; }} .swiper-slide-active {{ display:block; }}
    .swiper-slide img {{ width:64px; height:64px; }}
    </style></head><body><div class="note-container">
    <img class="avatar" src="data:image/png;base64,{blue}">
    <div id="detail-title">测试公园</div><div id="detail-desc">{content}</div>
    {'<video></video>' if video else ''}
    <div class="xhs-slider-container slider-zoom-in"><div class="note-slider">
    <div class="swiper-slide swiper-slide-duplicate" data-swiper-slide-index="1"><img src="{second}"></div>
    <div class="swiper-slide swiper-slide-active" data-swiper-slide-index="0"><img src="data:image/png;base64,{red}"></div>
    <div class="swiper-slide" data-swiper-slide-index="1"><img data-src="{second}"></div>
    <div class="swiper-slide swiper-slide-duplicate" data-swiper-slide-index="0"><img src="data:image/png;base64,{red}"></div>
    </div><button class="arrow-controller right" onclick="move(1)">下一张</button>
    <button class="arrow-controller left" onclick="move(0)">上一张</button></div>
    <div class="comments"><img src="data:image/png;base64,{blue}"></div></div>
    <script>function move(n) {{
      document.querySelector('.swiper-slide-active').classList.remove('swiper-slide-active');
      const el = document.querySelector(`.swiper-slide:not(.swiper-slide-duplicate)[data-swiper-slide-index="${{n}}"]`);
      el.classList.add('swiper-slide-active'); const img=el.querySelector('img');
      if(img.dataset.src) img.src=img.dataset.src;
    }}</script></body></html>'''


async def source_fixture(tmp_path, *, html=None, redirect=None):
    source = XiaohongshuSource(tmp_path / 'profile', pacing_seconds=0, timeout_seconds=30,
        executable_path=os.getenv('VERIFY_CHROMIUM', r'C:\Program Files\Google\Chrome\Application\chrome.exe'))
    context = await source._ensure_context()
    visits = []

    async def document(route):
        url = route.request.url
        visits.append(url)
        if url == HOME:
            body = '<html>首页</html>'
        elif urlsplit(url).hostname == 'xhslink.com':
            target = redirect.get(url, NOTE) if isinstance(redirect, dict) else redirect
            return 302, {'location': target or NOTE + '?xsec_token=PRIVATE'}, b''
        elif url.startswith(NOTE):
            body = html if html is not None else detail()
        else:
            body = '<html>unexpected external request</html>'
        return 200, {'content-type': 'text/html; charset=utf-8'}, body.encode()

    source._fetch_note_document = document
    # 非文档资源一律拦截，测试夹具不访问公网。
    await context.route('**/*', lambda route: route.abort())
    return source, visits


@pytest.mark.parametrize('empty_text', [False, True, 'absent'])
def test_short_link_reads_each_photo_in_order_and_deduplicates_alias(tmp_path, empty_text):
    async def run():
        html = detail(content='' if empty_text else '免费开放')
        if empty_text == 'absent':
            html = html.replace('<div id="detail-desc"></div>', '')
        source, visits = await source_fixture(tmp_path, html=html)
        try:
            current = source.for_run()
            item = await current.read_note(SHORT)
            assert item.original_url == SHORT and item.canonical_url == NOTE
            assert item.content == ('' if empty_text else '免费开放')
            assert len(item.images) == 2
            assert [Image.open(BytesIO(data)).convert('RGB').getpixel((32, 32)) for data in item.images] == [
                (255, 0, 0), (0, 0, 255)]
            before = len(visits)
            alias = await current.read_note(NOTE + '?xsec_token=other')
            assert len(visits) == before
            assert alias.original_url.endswith('other') and alias.images is item.images
            assert await current.read_note(SHORT) is item
            assert not current._body_cache  # 原材料不能进入外部证据缓存。
            assert all(page.url == 'about:blank' for page in source._context.pages)
            fresh = await source.for_run().read_note(NOTE)
            assert len(visits) > before and fresh.images is not item.images
        finally:
            await source.aclose()
    asyncio.run(run())


def test_document_transport_disables_automatic_redirects(tmp_path):
    async def run():
        disposed = []

        class Response:
            status = 302
            headers = {'location': 'https://evil.test'}

            async def body(self):
                return b''

            async def dispose(self):
                disposed.append(True)

        class Route:
            async def fetch(self, **kwargs):
                assert kwargs == {'max_redirects': 0, 'timeout': 25000}
                return Response()

        source = XiaohongshuSource(tmp_path / 'unused')
        assert await source._fetch_note_document(Route()) == (302, Response.headers, b'')
        assert disposed == [True]
    asyncio.run(run())


@pytest.mark.parametrize('loop', [False, True])
def test_relative_multihop_redirect_and_loop_limit(tmp_path, loop):
    async def run():
        second = 'http://xhslink.com/o/second'
        redirects = {SHORT: '/o/second', second: SHORT if loop else NOTE}
        source, visits = await source_fixture(tmp_path, redirect=redirects)
        try:
            if loop:
                with pytest.raises(LinkReadError) as caught:
                    await source.read_note(SHORT)
                assert caught.value.status_code == 422 and len(visits) <= 8
            else:
                item = await source.read_note(SHORT)
                assert item.canonical_url == NOTE and len(item.images) == 2
                assert visits == [HOME, SHORT, second, NOTE]
        finally:
            await source.aclose()
    asyncio.run(run())


def test_active_reader_cancellation_releases_profile(tmp_path):
    async def run():
        source, _ = await source_fixture(tmp_path)
        entered = asyncio.Event()

        async def wait(*args):
            entered.set()
            await asyncio.sleep(30)

        source._note_images = wait
        task = asyncio.create_task(source.read_note(NOTE))
        await asyncio.wait_for(entered.wait(), timeout=10)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=6)
        assert not source._lock.locked() and source._owner_task is None
        assert source._context is None
        await source.aclose()
    asyncio.run(run())


@pytest.mark.parametrize('target', ['https://evil.test/private', 'http://127.0.0.1/private',
    'https://www.xiaohongshu.com.evil.test/a', 'file:///etc/passwd'])
def test_short_link_cannot_navigate_outside_xhs(tmp_path, target):
    async def run():
        source, visits = await source_fixture(tmp_path, redirect=target)
        try:
            with pytest.raises(LinkReadError) as caught:
                await source.read_note(SHORT)
            assert caught.value.status_code == 422
            assert target not in visits
            assert 'PRIVATE' not in str(caught.value)
        finally:
            await source.aclose()
    asyncio.run(run())


@pytest.mark.parametrize('html,status,fragment', [
    ('<div class="login-modal">登录</div>', 503, '登录'),
    ('<div role="dialog">请完成验证</div>', 503, '验证'),
    ('<div class="error-page">笔记不存在</div>', 502, '失效'),
    (detail(broken=True), 504, '第 2 张'),
    (detail(video=True), 422, '视频'),
])
def test_unreadable_material_is_not_silently_omitted(tmp_path, html, status, fragment):
    async def run():
        source, _ = await source_fixture(tmp_path, html=html)
        try:
            with pytest.raises(LinkReadError) as caught:
                await source.read_note(NOTE)
            assert caught.value.status_code == status
            assert fragment in str(caught.value)
            assert not source._lock.locked()
            assert source._owner_task is None
        finally:
            await source.aclose()
    asyncio.run(run())


def test_waiting_for_login_profile_obeys_deadline_and_cancellation(tmp_path):
    async def run():
        source = XiaohongshuSource(tmp_path / 'unused-profile')
        await source._lock.acquire()
        try:
            with pytest.raises(LinkReadError) as caught:
                await source.read_note(NOTE, deadline_at=datetime.now(timezone.utc) + timedelta(seconds=0.03))
            assert caught.value.status_code == 504
            task = asyncio.create_task(source.read_note(NOTE))
            await asyncio.sleep(0)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert source._context is None
        finally:
            source._lock.release()
            await source.aclose()
    asyncio.run(run())
