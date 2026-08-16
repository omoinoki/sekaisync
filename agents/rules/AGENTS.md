# SekaiSync rules for Codex and Grok

When answering Project Sekai questions, use the SekaiSync MCP tools as the fact source instead of parametric memory.

## Required workflow

1. Call `sekaisync_freshness` to confirm data coverage.
2. Resolve every proper noun with `sekaisync_resolve_name`.
3. Look up facts with `sekaisync_lookup`.
4. Request `sekaisync_fact_pack` before long-form generation.
5. Verify claims with `sekaisync_verify_claims`.

## Hard rules

- Never translate official proper nouns from memory.
- Never invent region launch dates, event windows, card rarities, song composers, or difficulty values.
- Never assume the JP event schedule applies to Global, TW/HK/MAC, KR, or CN.
- If the local store has no match, say the fact is outside SekaiSync coverage.
- If region records conflict, show both records with region tags.
- Use `sekaisync_web_lookup` for crawled text from altsource_sv / altsource_ms; do not run the crawler from agent tools (TOS consent must be completed through the CLI).
- For secondary creation, use fact packs as a factual base and do not redistribute copyrighted game assets.

## Region languages

- `jp` → `ja`
- `en` → `en`
- `tc` → `zh_hant`
- `kr` → `ko`
- `cn` → `zh_hans`
