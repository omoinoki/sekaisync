# Second Trial Command Order

Run from repository root with the bundled Python. This file documents future
commands; creation of tooling authorizes NONE of the metadata or semantic
stages. Root must finish the original trial's target stages before any generic
product repair. Freeze all tooling before any fresh reader is dispatched.

```powershell
$py = 'C:\Users\Mutou\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe'
$runner = 'work/scraper-261002/benchmark-02/runner.py'
& $py -B $runner --help
& $py -B work/scraper-261002/benchmark-02/check_tooling.py
& $py -B $runner freeze-tooling
```

The synthetic tooling cases are NOT the 9 planned product tests and cannot
satisfy the exact 2090-test closure. No preview has been selected by these checks.
Root can now cancel or separately authorize the metadata-only preview:

```powershell
& $py -B $runner preview --root-authorized
```

After Root reviews that exact preview and the original target stages finish,
perform any authorized generic product repair without consulting new bodies or
labels. Run the unchanged canonical validation runner into a FRESH checkpoint
directory, for example validation-03 ONLY IF IT DOES NOT EXIST:

```powershell
& $py -B work/scraper-261002/run_validation.py --output work/scraper-261002/validation-03
& $py -B $runner close-product --root-explicit-passed --closure-report work/scraper-261002/validation-03/report.json
& $py -B $runner register --root-authorized
& $py -B $runner capture --root-authorized
& $py -B $runner prepare
```

close-product requires actual canonical 2090 tests, not a planned count or older
PASS. Root reviews before each authorization. An existing output, partial stage,
missing evidence or any failure stops this second trial permanently; do not retry
or remove STOP.json. No usage exceptions or original artifact directories apply.

Dispatch five fresh independent source-reference agents, each reading ONLY
prepared/LANG/source-reference.txt plus the source reference contract. All
originals must live under benchmark-02. Freeze exactly en/ja/ko/zh_hans/zh_tw:

```powershell
& $py -B $runner freeze-source-reference --reference en=work/scraper-261002/benchmark-02/source-originals/en.json --reference ja=work/scraper-261002/benchmark-02/source-originals/ja.json --reference ko=work/scraper-261002/benchmark-02/source-originals/ko.json --reference zh_hans=work/scraper-261002/benchmark-02/source-originals/zh_hans.json --reference zh_tw=work/scraper-261002/benchmark-02/source-originals/zh_tw.json
& $py -B $runner source-submit --language en --answer work/scraper-261002/benchmark-02/source-host-originals/en.json --completion work/scraper-261002/benchmark-02/source-host-originals/en-completion.json
```

Dispatch separate fresh source hosts ONLY AFTER all references freeze. Give each
prepared/LANG/source-host.txt and source-host-instructions.txt, never labels,
selection, targets, other languages or previous trial data. Repeat source-submit
for five languages and actual normal source-followups.txt where needed. Retain
unchanged raw answers, completion binding, normal receipt, state and inert replay.

Every source submit exports a source-only host-sense-packet.txt. Fresh isolated
host-sense writers read only their own acquired vectors and source rows. Freeze
all five senses before ANY target read:

```powershell
& $py -B $runner freeze-host-senses --reference en=work/scraper-261002/benchmark-02/host-sense-originals/en.json --reference ja=work/scraper-261002/benchmark-02/host-sense-originals/ja.json --reference ko=work/scraper-261002/benchmark-02/host-sense-originals/ko.json --reference zh_hans=work/scraper-261002/benchmark-02/host-sense-originals/zh_hans.json --reference zh_tw=work/scraper-261002/benchmark-02/host-sense-originals/zh_tw.json
& $py -B $runner select-targets
```

Each fresh independent target-reference reader gets ONLY targets/LANG/target-host.txt,
reference-meanings.json and target-reference-contract.txt. They return all 40
obligations per source language, including reference-only miss contexts. Freeze
all five originals before dispatching target hosts:

```powershell
& $py -B $runner freeze-target-reference --reference en=work/scraper-261002/benchmark-02/target-originals/en.json --reference ja=work/scraper-261002/benchmark-02/target-originals/ja.json --reference ko=work/scraper-261002/benchmark-02/target-originals/ko.json --reference zh_hans=work/scraper-261002/benchmark-02/target-originals/zh_hans.json --reference zh_tw=work/scraper-261002/benchmark-02/target-originals/zh_tw.json
& $py -B $runner target-submit --language en --answer work/scraper-261002/benchmark-02/target-host-originals/en.json --completion work/scraper-261002/benchmark-02/target-host-originals/en-completion.json
& $py -B $runner baseline
& $py -B $runner score
```

Each separate fresh target host gets ONLY its unchanged normal target-host.txt and
source-meanings.json, never reference meanings or labels. Submit five languages
and genuine normal follow-ups through the same unmodified API. baseline preflights
both fresh local destinations; do not reuse old extraction/store/output paths.
score launches its own read-only consumer reload and emits the sole authoritative
scores/report.json, with omissions/unresolved/misses at zero correspondence credit.

Source completion JSON (all paths and hashes bind exact UTF-8 bytes):

```json
{"source_language":"en","allowed_input_path":"C:/dsh_projects/sekaisync-handoff-2026-08-14/work/scraper-261002/benchmark-02/prepared/en/source-host.txt","packet_sha256":"...","answer_sha256":"...","target_bodies_read":false,"reference_or_selected_units_read":false,"other_languages_read":false}
```

Target completion JSON:

```json
{"source_language":"en","allowed_input_path":"C:/dsh_projects/sekaisync-handoff-2026-08-14/work/scraper-261002/benchmark-02/targets/en/target-host.txt","packet_sha256":"...","answer_sha256":"...","target_reference_read":false,"other_host_answers_read":false}
```

External metadata dependencies and narrow unchanged code dependencies are listed
in PROTOCOL.md and dependency-manifest.json. Help/syntax/synthetic checks read no
corpus bodies, old answers/references, stores or semantic scores. Root must report
any quality FAIL or incomplete stage as such; no second-trial success is implied.
