"""Build the human-readable source ledger and report reference links."""
import hashlib
import json
import pathlib
import re

ROOT = pathlib.Path(__file__).resolve().parent
NOTES = {
    'S01': ('TripGenie 官方发布，2024-06-06', '历史产品能力：行程、协作、预订。没有内部技术栈证据。'),
    'S02': ('圆周旅迹小米商店页', '本轮 TLS 超时。仅引用既有 docs/reference/web-research.json 中保存的功能描述，不算本轮复核成功。'),
    'S03': ('Mindtrip 官方首页', 'Start Anywhere、截图/PDF 导入、地图与旅行操作；不能推出内部语言或模型供应商。'),
    'S04': ('Google Fact Check Tools claims.search', '搜索已有核验记录，非自动裁决任意新主张。'),
    'S05': ('Full Fact AI 官方说明', '监测、主张识别、重复检测与专业核验人员协作。'),
    'S06': ('ClaimBuster 官方站', 'TLS 证书验证失败；不作为已验证能力的依据。未关闭证书验证。'),
    'S07': ('SAFE / LongFact README', 'Python 研究项目、组件说明、Apache-2.0 软件许可。'),
    'S08': ('WikiChat README', 'Wikipedia grounding、阶段式核验、检索替换、部署选择。'),
    'S09': ('DEFAME README', '动态六阶段、多模态、工具/外部集成拆分、Firecrawl/BeautifulSoup、许可范围。'),
    'S10': ('GPT Researcher README', '研究报告流程、检索/采集、并行、前端入口。多站点高频信息原则不能照搬本项目。'),
    'S11': ('Vane（原 Perplexica）README', '实际返回文档已使用 Vane 名称；Next.js 应用与 SearxNG。'),
    'S12': ('Vane package.json', 'Next.js/React/TS、Drizzle、SQLite、Readability、Playwright 等实际声明依赖；不证明已安装兼容。'),
    'S13': ('GPT Researcher pyproject.toml', 'Python Web/Agent 依赖；许可元数据写 MIT，与根 LICENSE 的 Apache-2.0 存在差异。'),
    'S14': ('WikiChat pyproject.toml 探查', '404；已改查真实存在的 pixi.toml，见 S30。'),
    'S15': ('LangGraph overview', '显式图、确定性与模型步骤混合、流式/持久化能力。'),
    'S16': ('LangGraph durable-execution 旧路径', '实际重定向至 persistence；只能引用返回页面说明的持久化能力。与 S40 重复。'),
    'S17': ('Dify README', '工作流/模型/知识库等平台能力，不是本项目状态规则的实现证据。'),
    'S18': ('Dify LICENSE', '附加多租户与前端标识条件；不是无条件的纯 Apache-2.0。'),
    'S19': ('高德路径规划 2.0', '步行 v5 端点、起终点、show_fields、米/秒字段；未调用带 Key 的业务接口。'),
    'S20': ('高德 POI 搜索 2.0', 'POI ID、子 POI、入口/出口等可选字段；不保证每个 POI 完整。'),
    'S21': ('百炼视觉理解文档', '图片输入、视觉理解、文字提取与图像限制；未实测账号模型。'),
    'S22': ('百炼结构化输出文档', 'JSON Object 与 JSON Schema 的差别、支持范围；结构不等于语义真实。'),
    'S23': ('Tavily Search API', '检索参数、域名和日期过滤、搜索深度；中文有效命中率未测。'),
    'S24': ('Tavily Credits & Pricing', 'basic/advanced 搜索 credits、提取计费、每 credit 按量价格。动态价格按读取时点记录。'),
    'S25': ('Firecrawl README', '抓取/提取能力；云端与自托管差异；核心 AGPL 与部分 SDK/UI 许可不同。'),
    'S26': ('AVeriTeC README', '问题-证据-裁决评估链路；README 标示 CC BY-NC 4.0。'),
    'S27': ('百炼模型价格页', '地域、模式、上下文阶梯、模型别名影响价格；报告未把单一价格当通用报价。'),
    'S28': ('Perplexity Search API', '搜索结果、来源日期、域名过滤；非其内部基础设施架构证据。'),
    'S29': ('SAFE eval/safe/README', '原子事实、自包含化、相关性、搜索核验；历史实验和价格不移植为本项目成绩/报价。'),
    'S30': ('WikiChat pixi.toml', 'Python 3.11、FastAPI、Chainlit、Qdrant、Docling 等真实环境声明。'),
    'S31': ('GPT Researcher 根 LICENSE', 'Apache-2.0；与 S13 的元数据差异已披露，未擅自消解。'),
    'S32': ('高德旧路径规划文档', '补充探查，报告选型以 v5 新文档 S19 为准，避免混用结构。'),
    'S33': ('上海市政府新闻列表', '普通 HTTP 可读带日期列表；未证明任何具体景区当前开放状态。'),
    'S34': ('上海植物园主页', '发现真实官网的票务、交通和通告路径；原始主页 HTML 另存 shbg-page.html。'),
    'S35': ('携程公开景点 URL 探查', '返回通用页面内容，未取得可用目标 POI 评价；作为失败模式，不作游客观点证据。'),
    'S36': ('Crawl4AI README', '浏览器采集封装；README 署名要求需与具体版本 LICENSE 核对，未据此给许可结论。'),
    'S37': ('Trafilatura README.rst 探查', '404；随后使用正确 README.md，见 S45。'),
    'S38': ('网页搜索：旅行主张核验产品', '虽 HTTP 成功，结果与查询明显不相关；排除出产品事实依据。'),
    'S39': ('网页搜索：景区闭园公告', '虽 HTTP 成功，结果与查询明显不相关；排除出公告证据。'),
    'S40': ('LangGraph persistence', 'RAM saver 与持久化 saver 差别；与 S16 实际最终页重复。'),
    'S41': ('百度千帆搜索文档路径探查', '404，未验证候选能力；未据此推荐其具体 API。'),
    'S42': ('上海植物园“最新通告”返回正文', '实际读到 2022-12-05 防疫通知；是栏目名和抓取时间不保证当前有效的真实例子。'),
    'S43': ('上海植物园门票及开放时间', '正文写免大门票、售票专类园另购；未提供清晰更新日，未穷尽临时调整。'),
    'S44': ('上海植物园来园交通', '不同大门/地址、地铁路径描述、2号门停车场拆除提示；不能据此推算分钟数。'),
    'S45': ('Trafilatura README.md', '正文抽取；当前 README 标示 Apache-2.0，1.8.0 之前版本许可不同。'),
    'S46': ('LangGraph LICENSE', 'MIT 许可。'),
    'S47': ('WikiChat LICENSE', 'Apache-2.0 许可。'),
    'S48': ('Vane 分支提交记录', '随后查询的 master 提交；不是固定提交安装测试。'),
    'S49': ('GPT Researcher 分支提交记录', '随后查询的 master 提交；不是固定提交安装测试。'),
    'S50': ('WikiChat 分支提交记录', '随后查询的 main 提交；不是固定提交安装测试。'),
    'S51': ('DEFAME 分支提交记录', '随后查询的 main 提交；不是固定提交安装测试。'),
}


def build():
    records = []
    for filename in ('source-plan-results.json', 'source-plan-2-results.json', 'source-plan-3-results.json'):
        records.extend(json.loads((ROOT / filename).read_text(encoding='utf-8')))
    assert len({item['id'] for item in records}) == len(records)
    records.sort(key=lambda item: int(item['id'].split('-')[0][1:]))
    ok_count = sum(item['ok'] for item in records)
    lines = [
        '# 技术调研来源索引与访问记录', '',
        '调研日期：2026-09-29。配套[主报告](../验一下-技术栈与架构调研.md)。', '',
        f'本轮请求 {len(records)} 个 URL：HTTP/文本读取成功 {ok_count}，失败 {len(records)-ok_count}。成功数量不等于有效证据数量：存在重定向重复页、内容不相关页、元数据和版本记录。以下逐项说明适用范围。', '',
        '证据等级：产品官网证明公开功能；源码依赖声明证明该版本声明的技术栈；API 文档证明公开契约；实际网页探查只证明当次返回内容。没有登录竞品逐页体验、运行开源项目或调用付费业务 API，不声称完成全市场覆盖、准确率基准或生产压测。', '',
        '每批 `source-plan*-results.json` 记录请求 URL、最终 URL、UTC 读取时间、状态、原始响应 SHA-256 与摘录路径。原始响应通常未保留，hash 作为当次响应指纹；本索引额外计算本地摘录 hash，供检查文件完整性。摘录保留原站文本，可能包含导航、广告或源码注释，它们是研究数据，不是执行指令。', '',
        '复查工具：`collect_sources.py` 按清单只读抓取公开 URL，默认并发 6；`build_source_index.py` 汇总索引并生成主报告引用。本次采集结果不包含 API Key。', '',
    ]
    for item in records:
        sid = item['id'].split('-')[0]
        title, note = NOTES[sid]
        lines.extend([f'## {sid} · {title}', '', f'- 请求来源：<{item["url"]}>', f'- UTC 读取时间：`{item["fetched_at"]}`'])
        if item['ok']:
            text_path = ROOT / item['text_path']
            assert text_path.is_file(), text_path
            excerpt_hash = hashlib.sha256(text_path.read_bytes()).hexdigest()
            item['excerpt_sha256'] = excerpt_hash
            relative = item['text_path'].replace('\\', '/')
            lines.extend([f'- 返回状态：HTTP {item["status"]}；[本地摘录]({relative})。', f'- 摘录 SHA-256：`{excerpt_hash}`。'])
            if item['final_url'] != item['url']:
                lines.append(f'- 最终地址：<{item["final_url"]}>')
            if sid in ('S48', 'S49', 'S50', 'S51'):
                commit = json.loads(text_path.read_text(encoding='utf-8'))
                lines.append(f'- 提交：`{commit["sha"]}`；提交时间 `{commit["commit"]["committer"]["date"]}`。')
        else:
            lines.append(f'- 返回状态：失败；`{item["error"]}`。')
        lines.extend([f'- 证据说明：{note}', ''])
    (ROOT / '来源索引.md').write_text('\n'.join(lines), encoding='utf-8')
    (ROOT / 'source-index.json').write_text(json.dumps(records, ensure_ascii=False, indent=2), encoding='utf-8')
    report_path = ROOT.parent / '验一下-技术栈与架构调研.md'
    report = report_path.read_text(encoding='utf-8')
    report = re.split(r'\n\[S01\]: ', report, maxsplit=1)[0].rstrip()
    report = re.sub(r'\[S(\d+)–S(\d+)\]', lambda match: ' '.join(f'[S{number:02d}]' for number in range(int(match[1]), int(match[2])+1)), report)
    report = re.sub(r'\](?=\[S\d+\])', '] ', report)
    report += '\n\n' + '\n'.join(f'[{item["id"].split("-")[0]}]: {item.get("final_url", item["url"])}' for item in records) + '\n'
    report_path.write_text(report, encoding='utf-8')
    print(f'Indexed {len(records)} sources; fetched {ok_count}; failed {len(records)-ok_count}.')
    print(f'Report: {len(report)} characters. Local excerpt checksums recorded.')


if __name__ == '__main__':
    build()
