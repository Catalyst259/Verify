# 开发前检查 upstream

每次开始开发（包括修改代码、配置或文档）之前，先检查工作区、远端配置及 upstream 的最新状态。

- 运行 `git status --short`、`git remote -v` 和 `git branch -vv`。
- 获取 upstream 最新提交，再比较当前 HEAD 与上游默认分支的提交及相关文件差异；不能仅依赖本地缓存的远端分支。
- 本仓库的 fork 上游是 `Catalyst259/Verify`，当前默认分支为 `main`。若没有 `upstream` 远端，可通过 `git fetch https://github.com/Catalyst259/Verify.git main` 获取，并与 `FETCH_HEAD` 比较。
- 向用户简要报告上游是否更新、当前分支是否领先/落后/分叉，以及更新是否影响本次开发；根据差异调整实现。
- 检查上游不等于自动合并或重置。保留用户的本地修改；若无法访问远端，明确说明尚未确认最新状态。

# 代码注释

注释和 docstring 应让未参与设计讨论的维护者直接读懂：说明业务含义、必要约束、输入输出、异常及行为原因。不要写对话引用、助手的思考过程或依赖讨论上下文才能理解的表述。

## Agent skills

### Issue tracker

Issues and specs for this repo live as markdown files under `.scratch/` in this repo. See `docs/agents/issue-tracker.md`.

### Triage labels

The five canonical triage roles, each label string equal to its name. See `docs/agents/triage-labels.md`.

### Domain docs

Single-context: `GLOSSARY.md` and `docs/adr/` at the repo root. See `docs/agents/domain.md`.
