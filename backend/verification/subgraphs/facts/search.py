"""browser-use 取证适配：工具预算和网页记录由代码持有，Agent 只选择动作。"""

import asyncio
from collections.abc import Awaitable, Callable, Mapping
from datetime import date, datetime, timezone
import json
from time import perf_counter
from urllib.parse import quote_plus, urlsplit
from uuid import uuid4

from backend.extraction.agent import load_config
from backend.sources.xiaohongshu import canonical_note_id
from backend.verification.capabilities import EvidenceSource
from backend.verification.models import FactSourceType

from .diagnostics import page_category, record, timed
from .model import MAX_EVIDENCE_PER_ROUND, MAX_QUERIES, MAX_RESULTS_PER_QUERY, MAX_TOOL_CALLS, FactEvidence, SearchResult
from .state import FactClaimState, FactRoundState


class SearchSession:
    """单条 Claim 一轮的真实取证记录；超时或 Agent 输出损坏也不丢失材料。"""

    def __init__(self, claim: FactClaimState, deadline_at: datetime, *, checked_at: datetime | None = None,
                 input_urls: tuple[str, ...] = (), evidence_sources: Mapping[str, EvidenceSource] | None = None):
        self.claim_id = claim.plan.claim_id
        self.deadline_at = deadline_at
        self.round = FactRoundState(round_number=len(claim.rounds) + 1)
        self.evidence: list[FactEvidence] = []
        self.diagnostics: list[str] = []
        self.diagnostic_context = {"claim_id": self.claim_id, "round_number": self.round.round_number,
                                   "checked_at": checked_at.isoformat() if checked_at else None}
        self.seen = {(item.url, item.content) for item in claim.evidence}
        self.evidence_sources = evidence_sources or {}
        self.input_note_ids = frozenset(identity for url in input_urls
                                        if (identity := canonical_note_id(url)) is not None)

    def remaining_budget(self) -> dict:
        return {
            "tool_calls": MAX_TOOL_CALLS - self.round.tool_calls,
            "queries": MAX_QUERIES - self.round.queries,
            "evidence": MAX_EVIDENCE_PER_ROUND - len(self.evidence),
            "max_results_per_query": MAX_RESULTS_PER_QUERY,
        }

    def exhausted(self) -> bool:
        return self.round.tool_calls >= MAX_TOOL_CALLS or len(self.evidence) >= MAX_EVIDENCE_PER_ROUND

    def update_round(self, **changes):
        self.round = FactRoundState.model_validate(self.round.model_dump() | changes)

    async def execute(self, operation: Callable[[], Awaitable], *, query: bool = False,
                      diagnostic: dict | None = None, retrieved_at: datetime | None = None) -> dict:
        """执行一次搜索或读取；失败也计数，拒绝超额动作并返回最新预算。"""
        expired = datetime.now(timezone.utc) >= self.deadline_at
        if expired or self.exhausted() or (query and self.round.queries >= MAX_QUERIES):
            return {"stopped": True, "deadline_reached": expired, "remaining_budget": self.remaining_budget()}
        query_index = self.round.queries if query else None
        tool_call = self.round.tool_calls + 1
        self.update_round(
            tool_calls=self.round.tool_calls + 1,
            queries=self.round.queries + int(query),
            results_per_query=[*self.round.results_per_query, 0] if query else self.round.results_per_query,
        )
        started = perf_counter()
        failure = None
        try:
            async with asyncio.timeout(max(0, (self.deadline_at - datetime.now(timezone.utc)).total_seconds())):
                data = await operation()
            if query:
                data = data[:MAX_RESULTS_PER_QUERY]
                counts = list(self.round.results_per_query)
                counts[query_index] = len(data)
                self.update_round(results_per_query=counts)
            else:
                if canonical_note_id(data.get("url", "")) in self.input_note_ids:
                    raise ValueError("输入笔记不能作为本次核验的外部独立证据")
                item = FactEvidence.model_validate(data | {
                    "evidence_id": uuid4().hex,
                    "retrieved_at": retrieved_at if retrieved_at is not None else datetime.now(timezone.utc),
                })
                key = (item.url, item.content)
                if key in self.seen:
                    data = {"duplicate": True}
                elif len(self.evidence) >= MAX_EVIDENCE_PER_ROUND:
                    return {"stopped": True, "deadline_reached": False,
                            "remaining_budget": self.remaining_budget()}
                else:
                    self.seen.add(key)
                    self.evidence.append(item)
                    self.update_round(new_evidence_count=len(self.evidence))
                    data = item.model_dump(mode="json")
            result = {"data": data}
        except Exception as error:
            failure = error
            detail = str(error) or ("操作超时" if isinstance(error, TimeoutError) else type(error).__name__)
            message = f"{type(error).__name__}: {detail}"
            self.update_round(search_error="; ".join(filter(None, [self.round.search_error, message])))
            result = {"error": message}
        except asyncio.CancelledError as error:
            failure = error
            raise
        finally:
            if diagnostic is not None and failure is not None:
                if isinstance(failure, TimeoutError) or datetime.now(timezone.utc) >= self.deadline_at:
                    if diagnostic.get("category") in {"load_cancelled", "dom_cancelled"}:
                        diagnostic["category"] = diagnostic["category"].replace("cancelled", "timeout")
                diagnostic.update(category=page_category(diagnostic) or "unknown",
                                  elapsed_ms=round((perf_counter() - started) * 1000, 3),
                                  error_type=type(failure).__name__, error=str(failure) or type(failure).__name__)
                self.diagnostics.append(record("page_failure", **self.diagnostic_context,
                                               tool_call=tool_call, **diagnostic))
        if diagnostic is not None and failure is not None:
            result["diagnostic"] = diagnostic
        return result | {"remaining_budget": self.remaining_budget(),
                         "deadline_reached": datetime.now(timezone.utc) >= self.deadline_at}

    def result(self, raw: str | None = None) -> SearchResult:
        """核对 Agent 的结束输出；始终以工具记录为准，保留所有已读取材料。"""
        if raw is not None:
            output = SearchResult.model_validate_json(raw)
            records = {item.evidence_id: item for item in self.evidence}
            if output.claim_id != self.claim_id:
                raise ValueError("Search 返回了其他 Claim 的结果")
            if len({item.evidence_id for item in output.evidence}) != len(output.evidence):
                raise ValueError("Search 返回了重复的证据 ID")
            if any(records.get(item.evidence_id) != item for item in output.evidence):
                raise ValueError("Search 返回了未读取或被改写的证据")
            if output.error and not self.round.search_error:
                raise ValueError("Search 返回了工具未记录的错误")
        return SearchResult(claim_id=self.claim_id, evidence=self.evidence, error=self.round.search_error)


PAGE_SNAPSHOT = r"""() => {
    const navigation = performance.getEntriesByType('navigation')[0];
    const text = (document.body?.innerText || '').replace(/\s+/g, ' ').trim();
    const signals = ['#challenge-form', '#anomaly-modal', '.anomaly-modal', '#cf-challenge-running']
        .filter(selector => document.querySelector(selector));
    if (/verify (that )?you are human|confirm you are human|unfortunately, bots use duckduckgo too|请完成.{0,8}人机验证/i.test(text)) {
        signals.push('human_verification_text');
    }
    return {
        time_origin: performance.timeOrigin,
        http_status: navigation?.responseStatus || null,
        http_status_source: navigation?.responseStatus ? 'navigation_timing' : null,
        final_url: location.href, title: document.title.slice(0, 300),
        page_excerpt: text.slice(0, 1000), ready_state: document.readyState,
        challenge_detected: signals.length > 0, challenge_signals: signals
    };
}"""


async def page_data(browser, url: str, script: str, *, diagnostic: dict | None = None):
    """一次导航并读取 DOM，失败时尽力保存现场；不额外发起 HTTP 请求。

    HTTP 状态来自主文档的 Navigation Timing，浏览器不提供时保留 null。
    失败现场读取最多等待半秒，且不把导航前的旧文档归到新请求。
    """
    diagnostic = diagnostic if diagnostic is not None else {}
    diagnostic.update(requested_url=url, http_status=None, final_url=None, title=None, page_excerpt=None,
                      ready_state=None, navigation_ms=0, dom_read_ms=0, snapshot_ms=0)
    if urlsplit(url).scheme not in {"http", "https"} or not urlsplit(url).hostname:
        raise ValueError("取证只支持完整的 HTTP(S) URL")
    previous_document = None
    try:
        async with asyncio.timeout(0.5):
            page = await browser.get_current_page()
            if page is not None:
                previous_document = json.loads(await page.evaluate("() => performance.timeOrigin"))
    except Exception:
        pass
    except asyncio.CancelledError:
        diagnostic.update(category="load_cancelled", failure_stage="prepare")
        raise

    async def snapshot(*, failed_navigation=False):
        started = perf_counter()
        try:
            async with asyncio.timeout(0.5):
                page = await browser.get_current_page()
                if page is None:
                    raise ValueError("浏览器没有可读取的页面")
                data = json.loads(await page.evaluate(PAGE_SNAPSHOT))
                origin = data.pop("time_origin", None)
                if failed_navigation and (previous_document is None or origin == previous_document):
                    diagnostic["snapshot_error"] = "无法确认新文档已加载，未采用可能属于上一页面的现场"
                else:
                    diagnostic.update(data)
        except Exception as error:
            diagnostic["snapshot_error"] = f"{type(error).__name__}: {error}"
        finally:
            diagnostic["snapshot_ms"] += round((perf_counter() - started) * 1000, 3)

    phase = "navigation"
    started = perf_counter()
    try:
        try:
            await browser.navigate_to(url)
        finally:
            diagnostic["navigation_ms"] = round((perf_counter() - started) * 1000, 3)
        phase = "dom_read"
        await snapshot()
        started = perf_counter()
        try:
            page = await browser.get_current_page()
            if page is None:
                raise ValueError("浏览器没有可读取的页面")
            return json.loads(await page.evaluate(script))
        finally:
            diagnostic["dom_read_ms"] = round((perf_counter() - started) * 1000, 3)
    except (Exception, asyncio.CancelledError) as error:
        diagnostic["failure_stage"] = phase
        suffix = "timeout" if isinstance(error, TimeoutError) else (
            "cancelled" if isinstance(error, asyncio.CancelledError) else "error")
        diagnostic["category"] = ("load_" if phase == "navigation" else "dom_") + suffix
        await snapshot(failed_navigation=phase == "navigation")
        raise


def create_tools(session: SearchSession, browser):
    from browser_use import Tools
    from browser_use.agent.views import ActionResult

    tools = Tools(output_model=SearchResult)
    # 所有取证均走以下两个动作，防止默认导航或 extract 绕过预算和正文记录。
    for name in list(tools.registry.registry.actions):
        if name != "done":
            tools.exclude_action(name)

    @tools.action("搜索网页候选（最多 10 个），需 read_page 读取正文；已配置的小红书并行读取 WEB 正文证据。")
    async def search_web(query: str):
        diagnostic = {"operation": "search_web"}
        async def search():
            data = await page_data(browser, "https://html.duckduckgo.com/html/?q=" + quote_plus(query), """() => ({
                results: Array.from(document.querySelectorAll('.result__a')).slice(0, 10).map(a => ({
                    title: a.innerText,
                    url: new URL(a.href).searchParams.get('uddg') || a.href
                })),
                empty: !!document.querySelector('.no-results')
            })""", diagnostic=diagnostic)
            category = page_category(diagnostic, unrecognized=not data["results"] and not data["empty"])
            if category:
                diagnostic["category"] = category
                raise ValueError(f"搜索页面不可用：{category}；详见页面诊断")
            return data["results"]
        source = session.evidence_sources.get("xiaohongshu")
        if source is None:
            result = await session.execute(search, query=True, diagnostic=diagnostic)
        else:
            first_evidence = len(session.evidence)

            async def crawler_execute(operation, *, query=False, **kwargs):
                budget = session.remaining_budget()
                if not query and (budget["tool_calls"] <= 1 or budget["evidence"] <= 1):
                    return {"stopped": True,
                            "deadline_reached": datetime.now(timezone.utc) >= session.deadline_at,
                            "remaining_budget": budget}
                return await session.execute(operation, query=query, **kwargs)

            async def crawl():
                try:
                    await source.search(query, execute=crawler_execute, excluded_ids=session.input_note_ids,
                                        deadline_at=session.deadline_at)
                    return None
                except Exception as error:
                    message = f"Xiaohongshu: {type(error).__name__}: {str(error) or '来源未完成'}"
                    session.update_round(search_error="; ".join(filter(None, [session.round.search_error, message])))
                    return message

            result, crawler_error = await asyncio.gather(
                session.execute(search, query=True, diagnostic=diagnostic), crawl(),
            )
            result["crawler_evidence"] = [item.model_dump(mode="json") for item in session.evidence[first_evidence:]
                                          if canonical_note_id(item.url) is not None]
            result["crawler_error"] = crawler_error
            result["remaining_budget"] = session.remaining_budget()
            result["deadline_reached"] = datetime.now(timezone.utc) >= session.deadline_at
        return ActionResult(extracted_content=json.dumps(result, ensure_ascii=False))

    @tools.action("读取网页可见正文；仅确认发布主体后才标记 OFFICIAL，身份不明使用 WEB。")
    async def read_page(url: str, source_type: FactSourceType = "WEB"):
        diagnostic = {"operation": "read_page"}
        async def read():
            data = await page_data(browser, url, """() => ({
                source: document.title || location.hostname, url: location.href,
                content: document.body.innerText,
                published_at: document.querySelector('meta[property="article:published_time"]')?.content || null
            })""", diagnostic=diagnostic)
            category = page_category(diagnostic)
            if category:
                diagnostic["category"] = category
                raise ValueError(f"读取页面不可用：{category}；详见页面诊断")
            published = data["published_at"]
            if published:
                try:
                    data["published_at"] = date.fromisoformat(published)
                except ValueError:
                    try:
                        data["published_at"] = datetime.fromisoformat(published)
                    except ValueError:
                        data["published_at"] = None
            return data | {"source_type": source_type}
        result = await session.execute(read, diagnostic=diagnostic)
        return ActionResult(extracted_content=json.dumps(result, ensure_ascii=False))

    return tools


async def run_search(session: SearchSession, system_prompt: str, task: str) -> str:
    """运行一个有界 browser-use Agent；结束或取消时关闭浏览器。"""
    from browser_use import Agent, Browser, ChatOpenAI
    from browser_use.llm.messages import UserMessage
    from browser_use.llm.openai.serializer import OpenAIMessageSerializer
    from browser_use.llm.views import ChatInvokeCompletion

    config = load_config()

    class SearchChatOpenAI(ChatOpenAI):
        async def ainvoke(self, messages, output_format=None, **kwargs):
            with timed(session.diagnostics, "search_llm", **session.diagnostic_context):
                return await self.invoke_model(messages, output_format, **kwargs)

        async def invoke_model(self, messages, output_format=None, **kwargs):
            if config["model"].startswith("deepseek-") and output_format is not None:
                schema = json.dumps(output_format.model_json_schema(), ensure_ascii=False)
                messages = [*messages, UserMessage(content="返回符合此 JSON Schema 的 JSON：" + schema)]
                async with self.get_client() as client:
                    response = await client.chat.completions.create(
                        model=self.model, messages=OpenAIMessageSerializer.serialize_messages(messages),
                        response_format={"type": "json_object"}, extra_body={"thinking": {"type": "disabled"}},
                    )
                return ChatInvokeCompletion(
                    completion=output_format.model_validate_json(response.choices[0].message.content or ""),
                    usage=self._get_usage(response),
                )
            return await super().ainvoke(messages, output_format=output_format, **kwargs)

    llm = SearchChatOpenAI(model=config["model"], api_key=config["api_key"], base_url=config["base_url"],
                          timeout=config["timeout_seconds"], max_retries=0)
    browser = Browser(headless=True, use_cloud=False, enable_default_extensions=False,
                      executable_path=config["browser_executable_path"] or None)

    async def should_stop():
        return session.exhausted() or datetime.now(timezone.utc) >= session.deadline_at

    try:
        agent = Agent(
            task=task, llm=llm, browser=browser, tools=create_tools(session, browser),
            extend_system_message=system_prompt, output_model_schema=SearchResult,
            use_vision=False, use_judge=False, enable_planning=False, message_compaction=False,
            enable_signal_handler=False, directly_open_url=False, max_actions_per_step=1,
            max_failures=1, final_response_after_failure=False, register_should_stop_callback=should_stop,
        )
        history = await agent.run(max_steps=min(config["max_steps"], MAX_TOOL_CALLS + 1))
        if datetime.now(timezone.utc) >= session.deadline_at:
            raise TimeoutError("Search 已到内部截止时间")
        if history.is_successful():
            return str(history.final_result())
        if session.exhausted():
            return session.result().model_dump_json()
        raise RuntimeError("Search Agent 未完成取证")
    finally:
        with timed(session.diagnostics, "browser_cleanup", **session.diagnostic_context):
            async with asyncio.timeout(3):
                await browser.kill()
