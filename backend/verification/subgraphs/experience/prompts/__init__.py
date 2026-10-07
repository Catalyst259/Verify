"""加载 Experience 提示词；Plan/Validate 附输出 Schema，Search 只选择工具动作。"""

import json
from pathlib import Path
from typing import Literal

from pydantic import TypeAdapter

from ..model import ExperiencePlan, ValidateResult


def load_system_prompt(step: Literal["plan", "search", "validate"]) -> str:
    """加载静态体验指令；业务 State 应单独作为任务消息传入。

    未知步骤抛出 ValueError；资源读取失败保留对应的文件异常。
    """
    output_types = {"plan": list[ExperiencePlan], "validate": list[ValidateResult]}
    if step not in {"plan", "search", "validate"}:
        raise ValueError(f"未知 Experience 步骤: {step}")
    directory = Path(__file__).resolve().parent
    sections = [directory.joinpath(f"{step}.md").read_text(encoding="utf-8").strip()]
    if step == "search":
        return sections[0]
    schema = TypeAdapter(output_types[step]).json_schema()
    sections.append("## 输出 JSON Schema\n\n```json\n" + json.dumps(schema, ensure_ascii=False, indent=2) + "\n```")
    return "\n\n".join(sections)
