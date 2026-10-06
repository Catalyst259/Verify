"""Fact 的单次 OpenAI 兼容调用；输出结构由节点按现有契约校验。"""

import re

from openai import AsyncOpenAI

from backend.extraction.agent import load_config
from backend.common.model_json import normalize_model_json


async def complete(system_prompt: str, task: str) -> str:
    config = load_config()
    async with AsyncOpenAI(
        api_key=config["api_key"], base_url=config["base_url"],
        timeout=config["timeout_seconds"], max_retries=0,
    ) as client:
        response = await client.chat.completions.create(
            model=config["model"],
            messages=[{"role": "system", "content": system_prompt}, {"role": "user", "content": task}],
            # Plan/Validate 的顶层是数组，不能使用只接受对象的 JSON mode。
            extra_body={"thinking": {"type": "disabled"}} if config["model"].startswith("deepseek-") else None,
        )
    choice = response.choices[0]
    if choice.finish_reason != "stop" or not choice.message.content:
        raise ValueError("模型未返回完整的 Fact JSON")
    # 兼容完整响应外的 JSON 或无语言 Markdown 围栏，不从说明文字中抽取 JSON。
    # 围栏内的语法、字段和跨字段约束仍由 Plan/Validate 校验。
    content = choice.message.content.strip()
    fenced = re.fullmatch(r"```(?:json)?[ \t]*\r?\n(.*?)\r?\n```", content, flags=re.DOTALL | re.IGNORECASE)
    return normalize_model_json(fenced[1].strip() if fenced else content)
