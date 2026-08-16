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
