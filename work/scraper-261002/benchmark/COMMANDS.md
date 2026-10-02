# Checkpoint 261002 Runner Commands

Run from the repository root with the bundled Python executable. Registration
and capture flags are Root authorizations, not a permission to bypass a gate.
The source and target references must be separate independent agent originals.

```powershell
$py = 'C:\Users\Mutou\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe'
$runner = 'work/scraper-261002/benchmark/runner.py'
& $py $runner --help
& $py $runner preview
& $py $runner close-product --root-explicit-passed --closure-report work/scraper-261002/validation-02/report.json
& $py $runner register --root-authorized --usage-exceptions work/scraper-261002/benchmark/ROOT_FIXTURE_IDENTITY_REVIEW.md
& $py $runner capture --root-authorized
& $py $runner prepare
```

`prepare` exports five isolated source-reference TXT packets and five unchanged
normal source-host TXT packets below `prepared/<language>/`. Source hosts also
receive the source-host-instructions.txt supported typed-format request, never
the reference packet. Do not dispatch source hosts until all references freeze.
Freeze calls take exactly one `--reference LANG=path` for each of en, ja, ko,
zh_hans and zh_tw. For example:

```powershell
& $py $runner freeze-source-reference --reference en=work/scraper-261002/benchmark/source-originals/en.json --reference ja=work/scraper-261002/benchmark/source-originals/ja.json --reference ko=work/scraper-261002/benchmark/source-originals/ko.json --reference zh_hans=work/scraper-261002/benchmark/source-originals/zh_hans.json --reference zh_tw=work/scraper-261002/benchmark/source-originals/zh_tw.json
& $py $runner source-submit --language en --answer work/scraper-261002/benchmark/source-host-originals/en.json --completion work/scraper-261002/benchmark/source-host-originals/en-completion.json
```

Repeat source-submit for each language and any actual source follow-up packets.
Its successful output names the real source-followups.txt packet and exports a
source-only host-sense-packet.txt with that host's normally acquired subjects.
Freeze the host's source-only meanings with `freeze-host-senses` and five
`--reference LANG=path` arguments. Then run `select-targets`; this only
materializes tasks for the fifty IDs frozen before source-host submissions.

Each source-language target host receives ONLY targets/<language>/target-host.txt
and source-meanings.json. Each independent target reference receives that normal
TXT plus reference-meanings.json and target-reference-contract.txt instead.
The latter includes reference-only raw contexts for misses, not fabricated host
tasks. Freeze all five target references with `freeze-target-reference` and five
`--reference LANG=path` arguments before submitting any target-host answer.

```powershell
& $py $runner target-submit --language en --answer work/scraper-261002/benchmark/target-host-originals/en.json --completion work/scraper-261002/benchmark/target-host-originals/en-completion.json
& $py $runner baseline
& $py $runner score
```

Normal host answers are unmodified JSON arrays/TXT accepted by
`agent_review.parse_judgments_text`. Target relations must preserve their
source's frozen host sense_key and sense_gloss exactly. Source meanings are
external pretarget notes, never extra fields in discovery subjects. All packet
and answer hashes below mean SHA256 of the exact UTF-8 file bytes.

Source completion JSON fields:

```json
{"source_language":"en","allowed_input_path":"C:/dsh_projects/sekaisync-handoff-2026-08-14/work/scraper-261002/benchmark/prepared/en/source-host.txt","packet_sha256":"...","answer_sha256":"...","target_bodies_read":false,"reference_or_selected_units_read":false,"other_languages_read":false}
```

Target completion JSON fields:

```json
{"source_language":"en","allowed_input_path":"C:/dsh_projects/sekaisync-handoff-2026-08-14/work/scraper-261002/benchmark/targets/en/target-host.txt","packet_sha256":"...","answer_sha256":"...","target_reference_read":false,"other_host_answers_read":false}
```

Every successful submit preserves originals, normal receipt, inert replay and
state chain. A partial/failed submission produces STOP.json and is not repaired
in place. Root must report its failure rather than reuse its already seen labels
as another unseen trial. The final score reports strict current typed coverage
and identical common fixed-context lexical scalar correctness for both versions;
the twenty-point improvement gate uses only the latter common metric.
