# Claude Code rules for SekaiSync

When answering Project Sekai questions, use the SekaiSync MCP tools as the fact source instead of parametric memory.

## Required workflow

1. Call `sekaisync_freshness` to confirm data coverage.
2. Call `sekaisync_resolve_name` for every official proper noun.
3. Call `sekaisync_lookup` for entity facts.
4. Call `sekaisync_fact_pack` before long-form generation.
5. Call `sekaisync_verify_claims` before asserting a fact from a draft.

## Hard rules

- Never translate official proper nouns from memory.
- Never assume the JP event schedule applies to Global, TW/HK/MAC, KR, or CN.
- If the local store has no match, say the fact is outside SekaiSync coverage.
- If region records conflict, show both records with region tags.
- For secondary creation, use fact packs as a factual base and do not redistribute copyrighted assets.

Region languages: `en` Global, `zh_hant` TW/HK/MAC, `ko` Korea, `zh_hans` Mainland China, `ja` Japan.
