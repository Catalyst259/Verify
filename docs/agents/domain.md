# Domain Docs

How the engineering skills should consume this repo's domain documentation when exploring the codebase.

This repo is **single-context**: one `GLOSSARY.md` and one `docs/adr/` at the repo root. There is no `GLOSSARY-MAP.md` and no per-context glossary.

## Before exploring, read these

- **`GLOSSARY.md`** at the repo root
- **`docs/adr/`**: read ADRs that touch the area you're about to work in.

If any of these files don't exist, **proceed silently**. Don't flag their absence; don't suggest creating them upfront. The `/domain-modeling` skill (reached via `/grill-with-docs` and `/improve-codebase-architecture`) creates them lazily when terms or decisions actually get resolved.

## File structure

```
/
├── GLOSSARY.md
├── docs/adr/
│   ├── 0001-<decision-slug>.md
│   └── 0002-<decision-slug>.md
└── backend/
```

## Use the glossary's vocabulary

When your output names a domain concept (in an issue title, a refactor proposal, a hypothesis, a test name), use the term as defined in `GLOSSARY.md`. Don't drift to synonyms the glossary explicitly avoids.

If the concept you need isn't in the glossary yet, that's a signal: either you're inventing language the project doesn't use (reconsider) or there's a real gap (note it for `/domain-modeling`).

Until `GLOSSARY.md` exists, fall back to the vocabulary already established in `docs/成果/业务规则.md` and `docs/成果/验一下PRD.md` — `MATERIAL`, `TARGET_PLACE`, `CLAIM`, `CLAIMTYPE` (`FACT`/`ROUTE`/`CROWD`/`EXPERIENCE`), `Evidence`, `SubgraphResult`, `Assessment`, `verdict`.

## Flag ADR conflicts

If your output contradicts an existing ADR, surface it explicitly rather than silently overriding:

> _Contradicts ADR-0007 (event-sourced orders), but worth reopening because…_
