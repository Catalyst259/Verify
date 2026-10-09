"""链接输入契约、材料注入、来源隔离和运行截止时间；不访问公网或模型。"""

import asyncio
from datetime import datetime, timezone

from fastapi.testclient import TestClient
import pytest

from backend.main import create_app
from backend.common.errors import LinkReadError
from backend.common.xiaohongshu_links import canonical_note_id, valid_note_url
from backend.extraction.materials import LinkMaterial
from backend.extraction.models import ClaimExtractionResult
from backend.verification import service as service_module
from backend.verification.budget import RunBudget
from backend.verification.capabilities import VerificationCapabilities
from backend.verification.models import SubgraphResult
from test_graph import subgraph

NOTE = 'https://www.xiaohongshu.com/explore/' + 'a' * 24
SHORT = 'http://xhslink.com/o/Ab12'


class NoteReader:
    def __init__(self):
        self.calls = []

    async def read_note(self, url, *, deadline_at):
        self.calls.append((url, deadline_at))
        return LinkMaterial(url, NOTE, '公园', '公园免费开放', (b'PNG fixture',))


@pytest.mark.parametrize('url', [NOTE, NOTE + '?xsec_token=keep&xsec_source=pc', SHORT,
    NOTE.replace('/explore/', '/discovery/item/'), NOTE.replace('/explore/', '/search_result/'),
    NOTE.replace('https://', 'http://').replace('.com/', '.com:80/')])
def test_accepted_note_links(url):
    assert valid_note_url(url)


@pytest.mark.parametrize('url', ['https://example.com/a', 'file:///a', 'javascript:alert(1)',
    'https://www.xiaohongshu.com', 'https://www.xiaohongshu.com/user/profile/' + 'a' * 24,
    'https://www.xiaohongshu.com/search_result?keyword=test', 'https://xhslink.com/',
    NOTE.replace('.com', '.com.evil.test'), NOTE.replace('www.', 'user@www.'),
    NOTE.replace('.com', '.com:8443'), NOTE + '\\oops', NOTE + '\n',
    'http://127.0.0.1/a', 'https://[broken', 'https://xhslink.com@evil.test/o/abc'])
def test_rejected_links_never_call_reader_or_model(tmp_path, url):
    reader = NoteReader()

    async def unexpected(*args, **kwargs):
        pytest.fail('无效输入不得调用模型')

    # DTO 允许用户粘贴时的首尾空白，但不允许 URL 内部空白。
    if url == NOTE + '\n':
        assert not valid_note_url(url)
        return
    with TestClient(create_app(tmp_path, unexpected, capabilities=VerificationCapabilities(
        evidence_sources={'xiaohongshu': reader}))) as client:
        assert client.post('/api/verifications', json={'target_place': '公园', 'link': [url]}).status_code == 422
    assert not reader.calls


def test_link_only_and_mixed_keep_original_refs_and_exclude_resolved_note(tmp_path):
    reader = NoteReader()
    seen = []

    async def extract(place, text, links, images, *, link_materials):
        assert links == [SHORT]
        assert link_materials[0].images == (b'PNG fixture',)
        assert link_materials[0].original_url == SHORT
        return ClaimExtractionResult(target_place=place, claims=[{
            'claim_id': 'input', 'type': 'FACT', 'content': '公园免费开放',
            'sources': [{'source_type': 'LINK', 'source_ref': SHORT, 'source_text': '公园免费开放'}],
        }])

    async def check(state, runtime):
        seen.append(runtime.context)
        assert canonical_note_id(NOTE) in {canonical_note_id(url) for url in runtime.context.input_urls}
        assert runtime.context.run_budget.deadline_at == reader.calls[-1][1]
        return {'result': SubgraphResult(graph_name='fact', status='skipped')}

    caps = VerificationCapabilities(evidence_sources={'xiaohongshu': reader})
    with TestClient(create_app(tmp_path, extract, subgraphs={'fact': subgraph('fact', check)}, capabilities=caps)) as client:
        for text in ('', '停车方便'):
            result = client.post('/api/verifications', json={'target_place': '公园', 'link': [SHORT], 'text': text})
            assert result.status_code == 200, result.text
            assert result.json()['claims'][0]['sources'][0]['source_ref'] == SHORT
    assert caps.link_materials == caps.input_urls == ()
    assert seen[0].run_budget is not seen[1].run_budget


@pytest.mark.parametrize('status', [422, 502, 503, 504])
def test_link_errors_fail_whole_submission_with_index(tmp_path, status):
    class Broken(NoteReader):
        async def read_note(self, url, **kwargs):
            if url == SHORT:
                raise LinkReadError('可读原因', status)
            return await super().read_note(url, **kwargs)

    async def unexpected(*args, **kwargs):
        pytest.fail('材料不完整不能调用模型')

    with TestClient(create_app(tmp_path, unexpected, capabilities=VerificationCapabilities(
        evidence_sources={'xiaohongshu': Broken()}))) as client:
        response = client.post('/api/verifications', json={'target_place': '公园', 'text': '其他材料', 'link': [NOTE, SHORT]})
    assert response.status_code == status
    assert response.json() == {'detail': '第 2 条小红书链接：可读原因'}


def test_link_read_and_extraction_share_deadline(tmp_path, monkeypatch):
    monkeypatch.setattr(service_module, 'RunBudget', lambda **kwargs: RunBudget(timeout_seconds=0.12, **kwargs))

    class Slow(NoteReader):
        async def read_note(self, *args, **kwargs):
            await asyncio.sleep(0.07)
            return await super().read_note(*args, **kwargs)

    async def extract(*args, **kwargs):
        await asyncio.sleep(0.07)
        pytest.fail('提取不应重新获得完整预算')

    with TestClient(create_app(tmp_path, extract, capabilities=VerificationCapabilities(
        evidence_sources={'xiaohongshu': Slow()}))) as client:
        result = client.post('/api/verifications', json={'target_place': '公园', 'link': [SHORT]})
    assert result.status_code == 504


def test_expired_read_has_no_extra_model_call(tmp_path, monkeypatch):
    monkeypatch.setattr(service_module, 'RunBudget', lambda **kwargs: RunBudget(timeout_seconds=0.01, **kwargs))

    class Slow(NoteReader):
        async def read_note(self, *args, **kwargs):
            await asyncio.sleep(1)

    async def unexpected(*args, **kwargs):
        pytest.fail('读取超时后不能调用模型')

    with TestClient(create_app(tmp_path, unexpected, capabilities=VerificationCapabilities(
        evidence_sources={'xiaohongshu': Slow()}))) as client:
        result = client.post('/api/verifications', json={'target_place': '公园', 'link': [SHORT]})
    assert result.status_code == 504 and '第 1 条' in result.json()['detail']


def test_third_party_task_logs_redact_signed_urls_without_changing_material(caplog):
    import logging
    from backend.common.logging import install_link_redaction
    install_link_redaction()
    install_link_redaction()
    url = NOTE + '?xsec_token=DO_NOT_LOG&xsec_source=pc'
    with caplog.at_level(logging.INFO):
        logging.getLogger('browser_use.Agent.test').info('Task: %s', {'link': [url]})
    assert 'DO_NOT_LOG' not in caplog.text
    assert NOTE in caplog.text and '[redacted]' in caplog.text
    assert url.endswith('xsec_source=pc')
