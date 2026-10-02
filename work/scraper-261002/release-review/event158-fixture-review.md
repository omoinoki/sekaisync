# Event158 Registry Fixture Review

Review date: 2026-10-02. Mode: read-only metadata/entity-ID inspection, with this new review document as the sole output write.

## Scoped Finding

The event158 references in the two paths reported by `benchmark-02/STOP.json` are registry metadata/entity-ID test fixtures, not source or target dialogue used for scraping or a semantic trial. Both test files are byte-identical at the SHA-256 values below. Within these files, event158 has event metadata only and is used as a negative lookup assertion. No event158 episode, dialogue row, aligned translation, semantic reference label, or host answer is present.

This is a finding about these specific references, not proof that event158 has never been used elsewhere. It does not clear the entire historical usage scope, change the usage audit policy, authorize a fixture exception, rerank or replace a selected family, remove the STOP, authorize registration, or authorize any semantic stage. Root must make any separate decision under the governing protocol.

## Exact Input Hashes

SHA-256 hashes were computed from complete file bytes with PowerShell `Get-FileHash -Algorithm SHA256`; values are lowercase below. File paths are rooted at `C:/dsh_projects/sekaisync-handoff-2026-08-14/`.

| File | SHA-256 |
| --- | --- |
| `tests/test_registry.py` | `92db9017885810d4c95d5b909c7bbce6c8b42d0f654e9229e6712d6e06c36d59` |
| `work/scraper-repair-20260929/baseline/tests/test_registry.py` | `92db9017885810d4c95d5b909c7bbce6c8b42d0f654e9229e6712d6e06c36d59` |
| `work/scraper-261002/benchmark-02/STOP.json` | `05aa151cf0656d1b73b493de2ade95ea5d878317b479836d12fcce3f183452b2` |
| `work/scraper-261002/benchmark-02/metadata-plan.json` | `607dea0bbeef57df1351779950a4f20e80c8f955f529a7f7372d534525becd17` |
| `work/p0-exhaustive-20261001/source-sense-trial-01/prepare_trial.py` | `0421bf78d491018c14e2cb03a93280a0396607634a0f8b18b60c9841252db850` |

The full `prepare_trial.py` file was hashed, but its whole source was not inspected. The intended source inspection was its `audit_usage` function; the incidental adjacent-code exposure is disclosed below.

## Registry Line Evidence

The following line references apply independently to both `tests/test_registry.py` and `work/scraper-repair-20260929/baseline/tests/test_registry.py`, since their complete bytes and numbering match.

| Lines | Evidence | Classification |
| --- | --- | --- |
| 6 | Imports `build_registry`, `lookup_entity`, and `save_registry` from `sekaisync.registry`. | Registry-only test surface; imported implementation was not inspected or executed. |
| 10-13 | `_write_region` creates `store_root / "raw" / "jp" / "source"` and writes `events.json`. | Locally constructed metadata fixture; the variable/path word `source` does not establish a scraped dialogue source. |
| 16-21 | Event object has `id: 158`, an event name, `startAt: 1740031200000`, and `eventType: "marathon"`. | Event-level metadata only; no dialogue, episode, source/target pairing, or semantic annotations. |
| 22-27 | A second event object has `id: 174` and its own metadata. | Numeric entity-ID comparator. |
| 33-46 | The `eventStories.json` fixture contains event/story ID 174 only, with an outline and an episode title. | Event174 registry search metadata, not event158 dialogue or event158 semantic trial input. |
| 55-68 | `test_event_lookup_uses_outline_and_episode_title` builds a temporary registry and asserts event174/event_story174 lookup outcomes. | Registry metadata lookup test. |
| 70-80 | `test_numeric_query_prefers_entity_id_over_timestamp` builds a temporary registry, saves `kb/registry.json`, queries `"174"`, asserts `"event:174"`, and asserts `"event:158"` is not returned. | Entity-ID versus timestamp regression fixture. Event158's timestamp begins with 174, explaining the comparator. |
| 80 | `self.assertNotIn("event:158", [entity.id for entity, _ in matches])` | The explicit family-string hit that explains the audit match; it is a negative entity-ID assertion, not a selected story or dialogue. |

Neither test contains `event:158:8` or `event_story:158:8`. The event158 use is at event-family metadata level; the only event story object belongs to event174. No scraper or semantic-trial call is present in either complete test file. This statement is based on code inspection, not test execution or tracing the imported registry implementation.

## STOP And Audit Evidence

- `work/scraper-261002/benchmark-02/STOP.json:2-4` records `status: "FAIL"`, `stage: "register"`, and the error `selected family prior use or unreadable usage scope; do not rerank`, followed by the exact two reviewed test paths.
- `work/p0-exhaustive-20261001/source-sense-trial-01/prepare_trial.py:168-172` defines `audit_usage`, derives the event-family number, and constructs `event:{number}(:|\b)|event_story:{number}:|event[_-]{number}([_:-]|\b)`.
- `prepare_trial.py:173-178` includes `*.py` among audit globs and several temporary/body-store/reference exclusions; it does not exclude all test modules or this baseline test path.
- `prepare_trial.py:182-187` calls `rg -uu -l` and fails if there is a nonempty matching-path output, a nonempty stderr, or a return code outside 0/1. This is a conservative family-string/path gate, not a classifier of fixture versus dialogue use.
- `prepare_trial.py:188-189` reports its success mode as `rg path matches only; no matched line bodies returned`.

For family 158, the first pattern branch matches the line-80 literal `event:158` because the closing quote follows a word boundary. This static explanation is consistent with the STOP path list. The audit was not rerun, and this report does not override its result.

## Metadata Plan Evidence

- `work/scraper-261002/benchmark-02/metadata-plan.json:5-8` selects `event:158:8` / `event_story:158:8` and records completeness/release-status metadata.
- `metadata-plan.json:10-173` contains the selected family's five-language page IDs, language labels, character counts, hashes, timestamps, trust, and flags. Page-ID/body-hash metadata binds candidate identity but is not dialogue content and does not establish a past semantic trial.
- `metadata-plan.json:380-381` explicitly records `source_or_target_bodies_read: false` and `formal_registration_authorized: false`.

Those two booleans are declarations in the inspected artifact, not an independently verified execution history. The body hashes were not resolved to corpus files, opened, compared against dialogue, or used to infer semantic answers. No corpus/body page was read during this review.

## Scope And Limitations

Content inspection was limited to the complete two STOP-listed registry tests, the complete STOP JSON and metadata-plan JSON, and the `audit_usage` code. A request using `rg -n -A 95 '^def audit_usage'` inadvertently returned adjacent code lines 192-263 in `prepare_trial.py` as well: `metadata_universe`, `strip_labels`, and the opening portion of `census_release`. That was an inspection-scope overrun, disclosed immediately to Root. These were metadata-handling code lines only; no corpus bodies, semantic reference labels, or host answers were returned or inspected. The conclusions above do not rely on the adjacent-code exposure.

No wider search, corpus database read, source/target body read, semantic label/host-answer read, unit-test execution, scraper execution, trial execution, or audit rerun was performed. No subagent was spawned. Product code, tools, and previous artifacts were not edited; only this new review document was written with `apply_patch`.

Actual body/semantic evidence use observed within this review: none. Global absence of prior body/semantic use: not determined by this deliberately narrow scope. Existing STOP and metadata-plan authorization state remain unchanged.
