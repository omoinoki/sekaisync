"""Synthetic adapter checks only; no stage, usage scan, corpus or product test."""
import importlib.util
import json
from pathlib import Path
import re
import sys
from types import SimpleNamespace

sys.dont_write_bytecode = True
spec = importlib.util.spec_from_file_location("synthetic_trial03_adapter", Path(__file__).with_name("runner.py"))
r = importlib.util.module_from_spec(spec)
spec.loader.exec_module(r)
passed = []


def case(name, action, fail=False):
    try:
        action()
    except (ValueError, FileExistsError):
        if not fail:
            raise
    else:
        if fail:
            raise AssertionError("synthetic rejection missing: " + name)
    passed.append(name)


assert r.HERE == Path(__file__).resolve().parent
assert r.parent.HERE != r.HERE and r.parent.context["HERE"] == r.parent.HERE
assert r.prepare.__globals__ is r.context and r.score.__globals__ is r.context
assert r.context["closure"].__globals__ is r.context
assert r.context["tooling"] is r.tooling and r.context["hashes"] is r.hashes
assert r.context["__file__"] == r.__file__ and r.EXPECTED_TEST_COUNT == 2090
assert r.historical_audit is not r.b.audit_usage
assert r.historical_audit.__globals__ is r.audit_context
assert r.b.audit_usage.__globals__["subprocess"] is r.subprocess
passed.append("fresh namespace; 2090 closure and strict scorer inherited; original audit unmodified")

plan = dict(families=[dict(story_key=story) for story in r.FIXED_STORIES],
            source_or_target_bodies_read=False, fixed_source_rows=80,
            fixed_target_obligations=200, seed=r.b.SEED)
case("fixed before-body selection accepted", lambda: r.fixed_plan(plan))
case("replacement family rejected", lambda: r.fixed_plan(dict(plan, families=plan["families"][::-1])), True)
case("denominator drift rejected", lambda: r.fixed_plan(dict(plan, fixed_target_obligations=199)), True)
case("body-read plan rejected", lambda: r.fixed_plan(dict(plan, source_or_target_bodies_read=True)), True)

allowed = {str(r.ROOT / "tests/test_registry.py"): "a" * 64,
           str(r.ROOT / "work/scraper-repair-20260929/baseline/tests/test_registry.py"): "a" * 64}
case("both exact fixture matches accepted", lambda: r.reviewed_matches(r.FIXED_STORIES[0], list(allowed), allowed))
case("other audit match rejected", lambda: r.reviewed_matches(r.FIXED_STORIES[0], [*allowed, "another.py"], allowed), True)
case("only one reviewed path rejected", lambda: r.reviewed_matches(r.FIXED_STORIES[0], list(allowed)[:1], allowed), True)
case("exception for other fixed family rejected", lambda: r.reviewed_matches(r.FIXED_STORIES[1], list(allowed), allowed), True)
case("duplicate audit path rejected", lambda: r.reviewed_matches(r.FIXED_STORIES[0], [*allowed, *allowed], allowed), True)
for code, stderr, failed in ((0, "", False), (1, "", False), (2, "", True), (0, "unreadable", True)):
    fake = SimpleNamespace(returncode=code, stderr=stderr, stdout="")
    guard = r.AuditSubprocess(run=lambda *args, value=fake, **kwargs: value)
    case("isolated rg preflight " + str(code) + "/" + repr(stderr), lambda guard=guard: guard.run([]), failed)

for story in r.FIXED_STORIES:
    number = r.b.event_family(story).split(":")[1]
    pattern = rf"event:{number}(:|\b)|event_story:{number}:|event[_-]{number}([_:-]|\b)"
    for name in r.LOCAL_TOOLING:
        assert re.search(pattern, (r.HERE / name).read_text(encoding="utf-8")) is None
passed.append("new audit-included tooling has no self-created selected-family match")
old_contract = (r.PREVIOUS / "PROTOCOL.md").read_text(encoding="utf-8").split("<!-- reference-contract:start -->")[1].split("<!-- reference-contract:end -->")[0].strip()
new_contract = (r.HERE / "PROTOCOL.md").read_text(encoding="utf-8").split("<!-- reference-contract:start -->")[1].split("<!-- reference-contract:end -->")[0].strip()
assert old_contract == new_contract
passed.append("source reference contract copied exactly from frozen parent")

assert not (r.HERE / "tooling-seal.json").exists()
assert not (r.HERE / "metadata-plan.json").exists()
assert not (r.HERE / "metadata-registration.json").exists()
assert not (r.HERE / "body-handoff").exists()
print(json.dumps(dict(status="PASS", synthetic_checks=len(passed), checks=passed,
    stage_calls=0, historical_usage_scans=0, corpus_body_reads=0, product_tests_run=0,
    registration_or_capture_authorized=False), indent=2))
