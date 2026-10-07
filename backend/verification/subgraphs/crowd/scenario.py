"""把主张的场景条件（星期/节假日/时段/季节）变成可逐条核对证据的判定条件。

场景条件不是结论里的一句说明：它决定哪些材料能支持主张。周中发布的评价证明不了
周末的拥挤度，夏季的材料也证明不了秋季的结论，几年前的描述也证明不了「现在还成立」。
这里只做能从主张文字与证据日期确定的推论——只写月日的主张无法确定年份，就不补成
某个具体日期，留作待查问题。

证据的发布日期按地点当地的日历日解读：来源自己给出的日期已经是当地的发布日，
不能再用服务器时区换算一次；来源没给日期时只能承认不知道，不拿抓取时间冒充。
时效基准取核验时间：骨架在运行开始时写入 checked_at，与本进程时钟的差只有一次运行的
时长，因此这里直接用当前时刻作基准，不额外把 checked_at 透传进判定。
"""

import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone

from ...models import Evidence

WEEKDAY_NAMES = ("一", "二", "三", "四", "五", "六", "日")
WEEKEND = frozenset({5, 6})
WEEKDAY = frozenset({0, 1, 2, 3, 4})
# 演示阶段的时效上限：两年以前的拥挤度描述不能支持「现在还成立」的结论，只能作背景。
# 接真实客流数据后按供应商口径替换。
STALE_AFTER_DAYS = 730

_WEEKEND_WORDS = re.compile(r"周末|双休|周六|周日|星期六|星期日|礼拜六|礼拜日")
_WEEKDAY_WORDS = re.compile(r"工作日|平日|周一至周五|周一到周五|星期一至星期五")
_HOLIDAYS = re.compile(r"节假日|假期|长假|黄金周|国庆|春节|五一|端午|中秋|元旦|清明|寒暑假|寒假|暑假")
_SEASONS = (("春", (3, 4, 5)), ("夏", (6, 7, 8)), ("秋", (9, 10, 11)), ("冬", (12, 1, 2)))


@dataclass(frozen=True)
class ScenarioCondition:
    """一条场景条件；只有能按日期判别的条件才允许证明材料「不匹配」。

    weekdays/months 由条件本身推出，材料日期落在范围外即可判定场景不符；
    像节假日这种只能靠正文关键词识别的条件，缺关键词只说明材料没覆盖，
    不能据此说材料与主张冲突。
    """

    label: str
    weekdays: frozenset[int] = frozenset()
    months: frozenset[int] = frozenset()

    def violated_by(self, moment: date) -> bool:
        return bool((self.weekdays and moment.weekday() not in self.weekdays)
                    or (self.months and moment.month not in self.months))


@dataclass(frozen=True)
class TimeFit:
    """一条材料与主张场景的对齐结果；moment 为 None 表示发布时间无法确定。"""

    evidence_id: str
    moment: date | None
    violated: tuple[str, ...]
    stale: bool = False

    @property
    def aligned(self) -> bool:
        return self.moment is not None and not self.violated and not self.stale

    def describe(self) -> str:
        if self.moment is None:
            return f"{self.evidence_id} 发布时间未知"
        stamp = f"{self.evidence_id} {self.moment.isoformat()} 周{WEEKDAY_NAMES[self.moment.weekday()]}"
        if self.violated:
            return f"{stamp}（不符合：{'、'.join(self.violated)}）"
        return f"{stamp}（超出时效）" if self.stale else stamp


@dataclass(frozen=True)
class Alignment:
    """整批材料的场景对齐结果；判定阶段据此拒绝用不匹配材料支持结论。"""

    conditions: tuple[ScenarioCondition, ...]
    fits: tuple[TimeFit, ...]

    @property
    def aligned(self) -> frozenset[str]:
        return frozenset(fit.evidence_id for fit in self.fits if fit.aligned)

    @property
    def mismatched(self) -> frozenset[str]:
        return frozenset(fit.evidence_id for fit in self.fits if fit.violated)

    @property
    def undated(self) -> frozenset[str]:
        return frozenset(fit.evidence_id for fit in self.fits if fit.moment is None)

    @property
    def stale(self) -> frozenset[str]:
        return frozenset(fit.evidence_id for fit in self.fits if fit.stale)

    def coverage(self) -> str:
        """写进判定的时间覆盖说明；由代码生成，不让模型自报一个无法核对的覆盖范围。"""
        return "。".join([
            f"场景条件：{'；'.join(item.label for item in self.conditions) or '未给出可核对条件'}",
            "对齐材料 " + ("；".join(fit.describe() for fit in self.fits if fit.aligned) or "无"),
            f"场景不符 {len(self.mismatched)} 条",
            f"超出时效 {len(self.stale)} 条",
            f"发布时间未知 {len(self.undated)} 条",
        ])


def evidence_date(item: Evidence) -> date | None:
    """证据的发布日期；日期与时间戳都只取当地日历日。"""
    published = item.published_at
    if isinstance(published, datetime):
        return published.date()
    return published if isinstance(published, date) else None


def scenario_conditions(text: str) -> tuple[ScenarioCondition, ...]:
    """从主张的场景描述与条件文字里提取代码可以核对的场景条件。

    工作日/周末按优先级二选一：条件里同时出现「周末（工作日不成立）」时，「工作日」
    是在说明例外而不是第二条要求，否则任何日期都会被其中一条否定。
    """
    conditions = []
    if _WEEKEND_WORDS.search(text):
        conditions.append(ScenarioCondition("周末", weekdays=WEEKEND))
    elif _WEEKDAY_WORDS.search(text):
        conditions.append(ScenarioCondition("工作日", weekdays=WEEKDAY))
    seasons = [name for name, _ in _SEASONS if name in text]
    if seasons:
        conditions.append(ScenarioCondition(
            "季节：" + "、".join(seasons),
            months=frozenset(month for name, months in _SEASONS if name in seasons for month in months)))
    if _HOLIDAYS.search(text):
        conditions.append(ScenarioCondition("节假日或假期"))
    return tuple(conditions)


def alignment(text: str, evidence: list[Evidence], *,
              today: date | None = None, stale_after_days: float = STALE_AFTER_DAYS) -> Alignment:
    """按计划给出的场景条件核对每条材料的发布时间，并按时效上限排除过期材料。"""
    conditions = scenario_conditions(text)
    cutoff = (today or datetime.now(timezone.utc).date()) - timedelta(days=stale_after_days)
    fits = []
    for item in evidence:
        moment = evidence_date(item)
        fits.append(TimeFit(
            item.evidence_id or "", moment,
            tuple(condition.label for condition in conditions
                  if moment is not None and condition.violated_by(moment)),
            stale=moment is not None and moment < cutoff,
        ))
    return Alignment(conditions, tuple(fits))
