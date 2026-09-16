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

SekaiSync 的术语刮削是确定性的；遇到它无法判定的译名，会把条目放进本地待裁决队列。
**你（智能体）可以直接介入——不需要任何 API Key**：用你自己的推理判断，SekaiSync 只提供
队列与存储。判断会沉淀为本地方法论，后续刮削自动套用，于是需要你裁决的条数逐轮下降。

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

介入时的判据建议：

- **实体标识必须唯一**——好的标识：`一歌` / `星乃一歌` / `いちか` / `ホシノイチカ`（都唯一指向该角色）。
  **不能当标识的**：`お母さん`（母亲）/ `先輩` / `彼女` / `みんな`——可能指任何人，遇到应 `reject`。
  这条纪律来自 Sekai Viewer 知识图谱抽取 prompt 的正反例设计，能挡掉一整类"看似专名实则泛指"的噪声。
- **片假名术语优先看音译可逆性**——`セカイ→SEKAI`、`カイト→KAITO` 音形对应明确即可 `accept`；
  音形对不上的（`テスト→Huh`）是行位对齐噪声，应 `reject`。
- **拿不准就 `reject`**——错误的译名会污染知识库，`reject` 只是保留待后续证据，成本低得多。
- **能确定正确译名时用 `replace`** 并写清理由——这是最有价值的干预：SekaiSync 抓不到，但你知道。

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
