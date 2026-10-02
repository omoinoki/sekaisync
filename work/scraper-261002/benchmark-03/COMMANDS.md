# Root-Only Prospective Commands

Creation and synthetic checks authorize no data stage. Run from repository
root. Never invoke benchmark-02 again or clear its STOP. Root reviews and
freezes all tooling, then authorizes each stage below separately.

```powershell
$py = 'C:\Users\Mutou\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe'
$runner = 'work/scraper-261002/benchmark-03/runner.py'
& $py -B $runner --help
& $py -B work/scraper-261002/benchmark-03/check_tooling.py
# Root alone, after review:
& $py -B $runner freeze-tooling
& $py -B $runner preview --root-authorized
& $py -B $runner close-product --root-explicit-passed --closure-report work/scraper-261002/validation-03/report.json
$review = 'work/scraper-261002/release-review/event158-fixture-review.md'
$reviewSha = '9e9726bd1c1cc944be455b1d43fcb7f5b249b592fe13bf98f615176145780532'
& $py -B $runner register --root-authorized --root-confirmed-fixture-review --fixture-review $review --fixture-review-sha256 $reviewSha
# Separate Root capture authorization, only after independent fixture review:
& $py -B $runner capture --root-authorized
& $py -B $runner prepare
```

The closure still independently verifies actual 2090 full PASS and exact current
code; these are not permission to read bodies. Registration records schema
work/checkpoint261002-trial03-metadata-registration@1 and the nested metadata
identity proof schema work/checkpoint261002-trial03-metadata-identity-proof@1.
The exact independent-review and two-path hash binding are in PROTOCOL.

Use the pinned benchmark-02/COMMANDS.md source/sense/target/baseline commands,
with every trial artifact path changed to benchmark-03 and this runner, never
old inputs/stores. Each original must be new and inside benchmark-03. All five
independent source references freeze before any source host; all own host senses
freeze before select-targets; all five independent target references freeze
before target hosts. Fresh agents use fork_turns="none" and only allowed own
packets/contracts. No host/reference sharing. Normal follow-ups only.

Freeze stages accept repeated --reference LANG=benchmark-03-local-original.json
for exactly en, ja, ko, zh_hans and zh_tw. source-submit/target-submit accept
--language LANG --answer benchmark-03-local-answer.json --completion
benchmark-03-local-completion.json. Baseline and score remain separate final
stages. Do not rerun any failed/partial stage; STOP permanently retains it.
No new reader after 19:15; stop all work at 20:00 Asia/Singapore today. Source
morphology evidence takes priority. Incomplete or quality FAIL is not PASS.
