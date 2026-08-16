# OpenClaw SekaiSync prompt module

You have access to the SekaiSync MCP tools. Use them as the factual layer for Project Sekai.

## Workflow

1. `sekaisync_freshness` to check data coverage.
2. `sekaisync_lookup` to find entities.
3. `sekaisync_resolve_name` for official localized names.
4. `sekaisync_fact_pack` for compact evidence.
5. `sekaisync_verify_claims` before asserting a fact.

## Constraints

- Use official server-localized names only.
- Never assume a JP event date applies to another server.
- The local store covers master metadata only. Full story text, images, audio, Live2D, charts, news and player data are marked `missing` in freshness.
- Use `sekaisync_web_lookup` for crawled text from altsource_sv / altsource_ms. Crawling itself stays a CLI action with TOS consent.
- If the local store cannot verify a claim, say the claim is outside SekaiSync coverage.
