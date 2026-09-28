---
name: SekaiSync
description: Use local Project Sekai master data, official localized names, and compact fact packs before answering questions or generating secondary creation content.
---

# SekaiSync skill

Use this skill whenever the conversation involves Project Sekai characters, songs, cards, events, units, stories, or fan-created content.

## Workflow

1. Check `sekaisync_freshness` to confirm data coverage.
2. Resolve every proper noun with `sekaisync_resolve_name`.
3. Look up facts with `sekaisync_lookup`.
4. Request `sekaisync_fact_pack` for the entity IDs the answer needs.
5. Verify generated claims with `sekaisync_verify_claims`.

## Output contract

- Use only the official localized name returned by SekaiSync.
- Do not add facts absent from the returned record.
- If there is no match, state that the local knowledge base does not cover the entity.
- The local store covers master metadata only. Full story text, images, audio, Live2D, charts, news and player data are marked `missing` in freshness; return unverified for those categories.
- Use `sekaisync_web_lookup` for crawled text from altsource_sv / altsource_ms. Crawling itself stays a CLI action with TOS consent.
- Base secondary creation on fact packs and avoid redistributing copyrighted assets.

## 术语刮削干预（可选，需要时使用）

SekaiSync 的术语提取流程基于确定性算法运行；当遇到无法判定的译名候选时，相关条目会进入本地待裁决队列。
**智能体可以直接对队列进行裁决，无需任何外部 API Key**：由智能体自身完成语义推理，SekaiSync 仅负责提供队列数据与存储机制。裁决结果将沉淀为本地方法库，在后续提取中自动复用，使后续需要人工或模型裁决的条目数量逐步减少。

```bash
# 1. 导出待裁决队列（自包含：每条含 term/候选/证据，无需回读语料）
sekaisync terms review export --out queue.txt --limit 20

# 2. 读 queue.txt，逐条给出判断：
#    decision: accept（采纳 hint）| reject（否定）| replace（补正确译名，需填 value）
#    每条写一句中文 rationale（会沉淀进方法论），并指定 generalize: pair（仅该术语）或 pattern（泛化）

# 3. 写回
sekaisync terms review submit --file judgments.txt

# 查看沉淀与复用率
sekaisync terms review stats
sekaisync terms review methodology
```

裁决准则建议：

- **实体标识必须具备唯一指向性**：有效的专有标识如 `一歌` / `星乃一歌` / `いちか` / `ホシノイチカ`（均明确指向特定角色）；**不可作为标识的通用词汇**如 `お母さん`（母亲）/ `先輩` / `彼女` / `みんな`（泛指非专有称谓），遇到此类条目应执行 `reject`。该规则参考了Sekai Viewer 知识图谱抽取 prompt 的正反例设计，能有效排除看似专有名词实为日常泛指的高频噪声。
- **片假名术语优先验证音形对应与可逆性**：如 `セカイ→SEKAI`、`カイト→KAITO` 等音形对应明确的条目可执行 `accept`；对于音形不匹配（如 `テスト→Huh`）的行位对齐错位噪声，应执行 `reject`。
- **存疑条目统一执行 `reject`**：错误的译名会污染知识库，而 `reject` 仅保留待后续证据补充，试错成本远低于错误采纳。
- **确知正确译名时使用 `replace` 并注明依据**：此为价值最高的主动补充手段，可直接补齐自动化提取阶段缺失的先验知识。

## Deployment

Copy this file to the platform's skill directory:

| 平台 | 目标路径 |
| --- | --- |
| Codex | `.agents/skills/SekaiSync/SKILL.md` |
| Claude Code | `.claude/skills/SekaiSync/SKILL.md` |
| Cursor | `.cursor/skills/SekaiSync/SKILL.md` |
| Grok | `.grok/skills/SekaiSync/SKILL.md` |
| Hermes | `~/.hermes/skills/SekaiSync/SKILL.md` |
| AstrBot | Plugins > Skills 上传 |
