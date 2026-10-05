"""加载 Fact 系统提示词，并附上现有 Pydantic 契约生成的输出 Schema。"""

import json
from pathlib import Path
from typing import Literal

from pydantic import TypeAdapter

from ..model import FactPlan, SearchResult, ValidateResult


def load_system_prompt(step: Literal["plan", "search", "validate"]) -> str:
    """加载静态指令；PlanSkill 在调用模型前注入，不引入额外模型或工具调用。

    业务 State 应单独作为任务消息传入，不能插值进系统指令。
    未知步骤抛出 ValueError；资源读取失败保留对应的文件异常。
    """
    output_types = {"plan": list[FactPlan], "search": SearchResult, "validate": list[ValidateResult]}
    if step not in output_types:
        raise ValueError(f"未知 Fact 步骤: {step}")
    directory = Path(__file__).resolve().parent
    sections = [directory.joinpath(f"{step}.md").read_text(encoding="utf-8").strip()]
    if step == "plan":
        skill = directory.parent.joinpath("skills/fact-plan/SKILL.md").read_text(encoding="utf-8")
        sections.append(skill.split("---", 2)[2].strip())
    schema = TypeAdapter(output_types[step]).json_schema()
    sections.append("## 输出 JSON Schema\n\n```json\n" + json.dumps(schema, ensure_ascii=False, indent=2) + "\n```")
    return "\n\n".join(sections)
