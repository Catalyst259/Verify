"""加载 ROUTE 提示词；Plan/Validate 附输出 Schema，Search 描述实测的记录规则。

业务 State 单独作为任务消息传入，不插值进系统指令。
"""

import json
from pathlib import Path
from typing import Literal

from pydantic import TypeAdapter

from ..model import RoutePlan, RouteValidateResult


def load_system_prompt(step: Literal["plan", "search", "validate"]) -> str:
    if step not in {"plan", "search", "validate"}:
        raise ValueError(f"未知 Route 步骤: {step}")
    directory = Path(__file__).resolve().parent
    sections = [directory.joinpath(f"{step}.md").read_text(encoding="utf-8").strip()]
    if step == "search":
        return sections[0]
    schema = TypeAdapter({"plan": list[RoutePlan], "validate": list[RouteValidateResult]}[step]).json_schema()
    sections.append("## 输出 JSON Schema\n\n```json\n" + json.dumps(schema, ensure_ascii=False, indent=2) + "\n```")
    return "\n\n".join(sections)
