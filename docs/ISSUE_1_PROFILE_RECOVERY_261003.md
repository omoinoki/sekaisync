# Issue #1: Master Description Recovery

Date: 2026-10-03 (Asia/Singapore)

This note documents the master-data knowledge repair included in the
`0.4.2-alpha` release preparation after the `sekaisync-scraper-261002` checkpoint.
It is not a full-corpus rescrape, a new semantic-accuracy measurement, or proof
that an existing user's store has already been repaired.

## Corrected Coverage

- `characterProfiles.introduction` and the table's descriptive fields survive
  registry extraction, regional SQL persistence, lookup, and FactPack rendering.
- Profile names come from `gameCharacters` using the explicit `characterId`
  foreign key in the same region and pinned generation. The profile ID is not
  used as a character ID. Joined name sources retain their original table and
  content hash.
- Card `prefix`, `cardRarityType`, `attr`, `cardSkillName`, and
  `specialTrainingSkillName` are retained. Existing generic card aliases remain
  supported. Technical asset names do not displace the localized card title.
- Mission `sentence` and `areaItems`, `mysekaiFixtures`, and `eventItems`
  `flavorText` are retained as language-specific descriptions.
- Full entity IDs remain searchable after an entity acquires a display name.

Descriptions are evidence, not additional inferred structured facts. This does
not implement mission rewards/conditions, furniture effects, or card-skill
numerical modeling. Official team-role labels are not exhaustive capability or
ownership statements; missing evidence remains unknown.

## Regional And Language Semantics

Unscoped lookup searches regional descriptions, but still returns only genuine
common values in `facts`. `region_facts`, `field_status`, `coverage`, and
`needs_region` retain the actual regional evidence. Explicit-region lookup
returns that region's facts and provenance, not the common projection.

A current FactPack chooses a single regional snapshot for a language-bearing
entity. It reports the actual `region`, `effective_language`, `body_field`,
`source`, `version`, and `retrieval`. `zh_tw`/`zh_hant` and `zh_cn`/`zh_hans`
aliases are supported. Language fallback is disclosed in metadata and in the
persisted text's `Body Language` line. Conflicting same-language snapshots
require an explicit region rather than an arbitrary first match.

`factpack --region` and the optional `region` argument on MCP/HTTP FactPack select
only that server's current facts. Missing regional data does not borrow another
server's body. Existing argument forms remain valid.

Time-filtered FactPacks still withhold future and undated entities. Character
profiles have no supported public-time model, so `as_of` does not make their
current descriptions historical facts or enable chapter-level spoiler control.

## Repairing An Existing Store

Upgrading source code alone cannot recover fields already discarded from the
SQL registry. Rebuild from raw master JSON by running sync again:

Restart already-running MCP/HTTP processes after updating the package or source.
`refresh` reloads store indexes, not Python implementation modules. The automatic
revision reload described below assumes the process is running the fixed code.

```sh
python -m sekaisync --no-event-check sync --regions jp,en,cn,tc,kr
python -m sekaisync --no-event-check lookup --query "character_profile:18" --type character_profile
python -m sekaisync --no-event-check factpack --id character_profile:18 --language ja --region jp
```

For offline recovery, use the existing `sync --local REGION=PATH` interface with
local master mirrors and include every region to retain. Do not import an older
`registry.json` to recover omitted fields; the raw tables are the authority.
No new schema migration is introduced. Existing supported-schema requirements
and explicit migration safeguards still apply. A long-lived Core observes the
new committed revision without an explicit refresh.

## Verification Scope

Frozen full regression: 2,140/2,140 PASS in 486.321 seconds on Python 3.13.14,
including 50 new tests. SHA256 manifests over `sekaisync`, `scripts`, and `tests`
Python files matched before and after the run. Command:
`python -B -X utf8 -m unittest discover -s tests -t .`.
Local evidence: `work/issue-1-20261003/validation.json` and the frozen test log.
The earlier exploratory run rejected import-time code drift while implementation
was still changing; it is preserved separately and is not acceptance evidence.

The built wheel passed isolated installation, CLI init/sync/lookup, Core, and
MCP smoke checks without importing the checkout package. Existing SQLite
`ResourceWarning` messages also occur in the checkpoint health log and remain
a separate cleanup concern; the regression result is not a zero-warning claim.

New regressions cover extraction and foreign-key isolation, language aliases and
fallback disclosure, region conflicts, time withholding, actual CLI init/sync,
MCP dispatch, loopback HTTP REST, unchanged-raw repair of an old sparse database,
and unsupported ability/ownership claims remaining unknown.

Read-only verification used local five-region raw generation
`20260926T085102Z-9e7b8f0f`: 73,849 registry entities and 26 profiles. Profile 18
rendered the matching source introduction in all five languages, including
traditional-Chinese alias requests. Character-name and role-keyword lookups,
card 1 title/rarity/attribute/skill, and beginner-mission sentence checks passed.
This verifies the recorded local snapshot, not the newest upstream tables.

These checks used isolated rebuilt fixtures; they did not rebuild the production
store. Release preparation is authorized for SekaiSync `0.4.2-alpha` and the
coordinated SekaiSync Connect `0.3.9-alpha.1` adaptation. Publication is recorded
separately on GitHub; users still need the recovery steps above for older stores.

Connect's canonical unit suite passed 89/89. Targeted official installed-host
Web mode UI checks also passed panel Save/reload, connection, disable/re-enable,
and regional/error-state tool calls. Tool results were real, while a keyless
deterministic adapter generated the calls; no online-model quality claim is
made. Five-language raw verification was backend coverage, not five-language
GUI acceptance. The unchanged WinUI3 client still needs its separate
multi-region entity-detail/body-search adaptation. See the
[release validation boundaries](../CHANGELOG.md).
