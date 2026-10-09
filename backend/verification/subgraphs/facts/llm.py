"""Fact 的单次 OpenAI 兼容调用；输出结构由节点按现有契约校验。"""

import json
import re

from openai import AsyncOpenAI

from backend.common.errors import ModelOutputError
from backend.extraction.agent import load_config
from backend.common.model_json import normalize_model_json


async def complete(system_prompt: str, task: str) -> str:
    config = load_config()
    options = {}
    if config["model"].startswith("deepseek-"):
        # JSON mode 要求顶层对象；只包装传输格式，节点仍校验原有数组契约。
        system_prompt += ('\n\n传输格式：将上述要求的完整 JSON 数组放入顶层对象的 items 字段，'
                          '只输出该 JSON 对象，不加其他字段或解释。上述数组 Schema 适用于 items 的值。'
                          '没有条目时输出 {"items": []}。')
        options = {"response_format": {"type": "json_object"},
                   "extra_body": {"thinking": {"type": "disabled"}}}
    async with AsyncOpenAI(
        api_key=config["api_key"], base_url=config["base_url"],
        timeout=config["timeout_seconds"], max_retries=0,
    ) as client:
        response = await client.chat.completions.create(
            model=config["model"],
            messages=[{"role": "system", "content": system_prompt}, {"role": "user", "content": task}],
            **options,
        )
    if not response.choices:
        raise ModelOutputError("模型未返回完整的 JSON 响应")
    choice = response.choices[0]
    if choice.finish_reason != "stop" or not choice.message.content:
        raise ModelOutputError("模型未返回完整的 JSON 响应")
    # 兼容完整响应外的 JSON 或无语言 Markdown 围栏，不从说明文字中抽取 JSON。
    # 围栏内的语法、字段和跨字段约束仍由 Plan/Validate 校验。
    content = choice.message.content.strip()
    fenced = re.fullmatch(r"```(?:json)?[ \t]*\r?\n(.*?)\r?\n```", content, flags=re.DOTALL | re.IGNORECASE)
    try:
        content = normalize_model_json(fenced[1].strip() if fenced else content)
    except ValueError:
        raise ModelOutputError("模型未返回完整的有效 JSON") from None
    if options:
        envelope = json.loads(content)
        if not isinstance(envelope, dict) or set(envelope) != {"items"} or not isinstance(envelope["items"], list):
            raise ModelOutputError("模型必须返回仅含 items 数组的 JSON 对象")
        return json.dumps(envelope["items"], ensure_ascii=False)
    return content
