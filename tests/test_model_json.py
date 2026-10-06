import json

import pytest
from pydantic import BaseModel, ConfigDict, ValidationError

from backend.common.model_json import normalize_model_json


class ModelOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    thinking: str
    action: list[dict]


def test_provider_control_characters_preserve_text_and_require_the_complete_schema():
    content = '{"thinking":"第一行\n第二行\t第三行\r末行\u0000", "action":[{"search_web":{"query":"测试景区"}}]}'
    normalized = normalize_model_json(content)
    result = ModelOutput.model_validate_json(normalized)
    assert result.thinking == "第一行\n第二行\t第三行\r末行\u0000"
    assert result.action == [{"search_web": {"query": "测试景区"}}]
    assert json.loads(normalized)["thinking"] == result.thinking
    with pytest.raises(ValidationError):
        ModelOutput.model_validate_json(normalize_model_json('{"thinking":"一\n二","unregistered":true}'))


@pytest.mark.parametrize("content", ['[]', '{"thinking":"line\\nnext","action":[]}'])
def test_valid_json_is_unchanged(content):
    assert normalize_model_json(content) == content


@pytest.mark.parametrize("content", ['{"thinking":"unfinished', '{"action":[]', '{} trailing', 'prefix {}', "{'action': []}", '{} {}', '{"value":NaN}', '{"value":Infinity}', '{"value":"\\q"}'])
def test_invalid_structure_still_fails_closed(content):
    with pytest.raises(ValueError, match="完整的有效 JSON"):
        normalize_model_json(content)
