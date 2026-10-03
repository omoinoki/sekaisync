# Changelog

English | [中文](CHANGELOG.zh-CN.md)

These notes describe the authorized `0.4.2-alpha` release candidate prepared on
2026-10-03. Publication and issue closure are recorded separately on GitHub;
preparing these notes is not itself proof that either has completed. Earlier
release history is not reconstructed here.

## 0.4.2-alpha - 2026-10-03 (Release Candidate Notes)

### Release Scope

- [Issue #1: character profile body loss and character-name lookup failure](https://github.com/omoinoki/sekaisync/issues/1)
  is addressed by the extraction, lookup, regional evidence, and FactPack
  changes below; the earlier scraper checkpoint alone did not fix it. The frozen
  functional snapshot passed 2,140 tests, and release preparation is now
  authorized. This release does not rebuild existing user stores automatically.

### Master Description Recovery

- Preserve character-profile introductions and descriptive fields, joining
  character names through the same-region, same-generation `characterId`.
- Search regional descriptions without inventing common facts. Scoped lookup
  returns the requested region's facts and evidence; FactPacks disclose their
  actual region, body language, field, and source, including language fallback.
- Support optional FactPack `region` selection through CLI, MCP, and HTTP;
  preserve existing argument forms, Chinese language aliases, and full-ID lookup.
- Correct the same extraction gap for real card titles/skill names, mission
  sentences, and item flavor text without inferring numerical effects.
- Treat official responsibility labels as team-role snapshots, not negative
  evidence about individual abilities or possessions.

Existing stores need a raw-data rebuild or another sync to recover fields that
older extraction discarded. See the [recovery notes](docs/ISSUE_1_PROFILE_RECOVERY_261003.md).

### Coordinated DSH Connect Update

SekaiSync Connect `0.3.9-alpha.1` carries the corresponding DSH adaptation:

- Expose optional FactPack region selection and supported Chinese language
  aliases, requiring explicit-region requests to be acknowledged by the backend.
- Preserve regional lookup evidence and FactPack scope, missing/conflicting
  content, actual body language, source and version ahead of long text.
- Throw execution failures and cancellation for official ToolRuntime error
  handling. Alias no-match remains a domain result, while complete infrastructure
  failure throws; partial status results disclose supplemental endpoint failures.
- Preserve an explicitly configured SekaiSync source root when saving a detached
  store path, unless actual source-file evidence supports a replacement.

Connect's canonical unit suite passed 89/89 with no failures or skipped tests.
Installed DSH `0.2.0-rc.2` ASAR ToolRuntime checks passed all ten tool schemas and
output renders, plus config, concurrency, dispatch, and argument-error handling.
The separate Host/Client-half assembly scripts use simulated contexts; those
checks are not full client end-to-end acceptance.

Real browser UI testing used the unmodified installed DSH in official Web mode
with an isolated profile, workspace and fixture store. Panel Save persisted its
store and original source root after reload; connection and a real JP FactPack
still worked after disable/re-enable. Six QA rounds produced nine genuine plugin
calls, including JP name lookup and language fallback, a TC `zh_tw` FactPack,
synthetic conflict with explicit-region retry, a synthetic missing body, and
failed HTTP404/required-argument calls. A deterministic keyless adapter generated
only tool calls; official tools and backend returned the actual results. This was
not an online-model quality evaluation, all-ten-tool GUI acceptance, or native
folder-picker acceptance. Five-language raw checks were backend checks, not five
separate GUI-language journeys.

WinUI3's existing build and SQLite readers remain compatible in the verified
paths, but its unchanged multi-region entity detail/search still does not expose
all regional bodies. This release makes no WinUI3 source update or complete
WinUI3 GUI-acceptance claim.

### Scraper Checkpoint: sekaisync-scraper-261002

`sekaisync-scraper-261002` is the canonical name of the scraper engineering
checkpoint merged into `main` at `b6c70aa`. It is not a package version or a
separately trained/distributed language model. The existing annotated Git tag
`scraper-261002` remains the historical code reference; it is not renamed.

- Reuse structural alignment to locate comparable text while discovering source
  expressions independently in all five languages. Saturated packets can continue
  onto later pages, with separate boundary audits rather than treating the first
  packet as exhaustive coverage.
- Represent raw source subjects, exact occurrence anchors, frozen source senses,
  and typed target relations separately. Preserve Unicode code-point spans,
  discontinuous vectors, complete expressions, and real omitted/unresolved gaps
  instead of flattening every occurrence into one translation string.
- Freeze source judgments before target review. Use actual target context through
  occurrence review, translation fallback, expansion, and cohesion follow-ups;
  host agents perform semantic reading, while normal validators check evidence
  geometry, source bindings, persistence, and idempotent submission receipts.
- Preserve the existing public interfaces and CLI usage. Occurrence-level
  relations do not automatically become global names, and the two-story support
  requirement for global derived translations is unchanged.
- Improve prompt and boundary handling for dependent clauses and meaningful
  attached grammatical forms without inventing normalized source text or
  mechanically splitting every suffix.

### Validation And Limits

The merged checkpoint passed its frozen 2,090-test engineering regression and
interface checks. These checks are not semantic accuracy measurements. The
first sealed quality experiment failed its full acceptance criteria; later
quality work did not complete full five-language/twenty-direction acceptance.
See the [checkpoint evidence and limitations](docs/SCRAPER_CHECKPOINT_261002.md).

The subsequent full-library rescrape and comparison work is paused at the user's
request. Partial database growth and successful local queries do not establish
whole-library precision, completeness, or a speedup. Omitted, unresolved, missing,
and unprocessed items remain gaps, not successful correspondences.

See the [checkpoint goals](docs/PROJECT_GOALS_261002.md),
[evolution roadmap](docs/TERM_SCRAPER_EVOLUTION_ROADMAP_2026-09-29.md), and
[current release follow-up](docs/TODO.md).
