from __future__ import annotations

import ast
import contextlib
import hashlib
import json
import platform
import sqlite3
import sys
import tempfile
import threading
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from sekaisync import dbstore
from sekaisync.core import SekaiSyncCore
from sekaisync.factpacks import build_fact_pack, build_fact_pack_at
from sekaisync.glossary import resolve_name
from sekaisync.models import Entity, GlossaryTerm
from sekaisync.registry import build_registry
from sekaisync.termindex import TermRecord
from sekaisync.trust import trust_for_page
from sekaisync.webindex import web_browse


def term(term_id: str, evidence: list[dict] | None = None) -> TermRecord:
    return TermRecord(
        id=term_id,
        canonical=term_id,
        source_language="ja",
        names={"ja": term_id},
        evidence=evidence or [],
    )


def probe_terms(root: Path) -> dict:
    dbstore.save_terms_records(root, [term("keep"), term("remove")])
    dbstore.save_terms_records(root, [term("keep")], replace_evidence=True)
    ids = [record.id for record in dbstore.load_terms_records(root)]
    evidence = [{"story_key": "fixture:1", "language": "ja", "sentence": "fixture sentence"}]
    dbstore.save_terms_records(root, [term("keep", evidence)])
    dbstore.save_terms_records(root, [term("keep", [])], replace_evidence=True)
    with dbstore.connect(root) as conn:
        advertised = conn.execute("SELECT evidence_count FROM terms WHERE id='keep'").fetchone()[0]
        actual = conn.execute("SELECT COUNT(*) FROM term_evidence WHERE term_id='keep'").fetchone()[0]
    return {
        "removed_record_survives": "remove" in ids,
        "empty_replacement_advertised_count": advertised,
        "empty_replacement_actual_count": actual,
    }


def probe_cache(root: Path) -> dict:
    core = SekaiSyncCore(root)
    started = threading.Event()
    release = threading.Event()
    outputs: list[dict] = []
    errors: list[str] = []
    value = {"current": "old"}

    def compute() -> dict:
        captured = value["current"]
        started.set()
        if not release.wait(5):
            raise TimeoutError("fixture release timeout")
        return {"value": captured}

    def worker() -> None:
        try:
            outputs.append(core._cached_aggregate("fixture", compute))
        except Exception as exc:
            errors.append(type(exc).__name__)

    thread = threading.Thread(target=worker)
    thread.start()
    try:
        if not started.wait(5):
            raise TimeoutError("fixture start timeout")
        value["current"] = "new"
        core._bump_data_version()
    finally:
        release.set()
        thread.join(5)
    if thread.is_alive() or errors:
        raise RuntimeError(f"cache fixture failed: {errors}")
    cached = core._cached_aggregate("fixture", lambda: {"value": value["current"]})
    return {"computed": outputs[0], "after_version_bump": cached, "expected": "new"}


def probe_region_facts(root: Path) -> dict:
    stamps = {"jp": 1_700_000_000_000, "en": 1_800_000_000_000}
    for region, stamp in stamps.items():
        path = root / "raw" / region / "source"
        path.mkdir(parents=True)
        (path / "events.json").write_text(
            json.dumps([{"id": 1, "name": f"Fixture {region}", "startAt": stamp}]),
            encoding="utf-8",
        )
    first = build_registry(root, ["jp", "en"])[0]
    second = build_registry(root, ["en", "jp"])[0]
    # P03 lossless contract: region-specific facts live in region_facts; the
    # common `facts` projection only carries fields identical across ALL
    # regions. startAt differs between jp/en, so it must NOT appear in facts
    # (an arbitrary first-region value would be order-dependent).
    def rf(entity, region):
        return entity.region_facts[region].facts
    return {
        "jp_startAt": rf(first, "jp")["startAt"],
        "en_startAt": rf(first, "en")["startAt"],
        "jp_then_en_source": first.region_facts["jp"].source,
        "same_regions": sorted(first.regions) == sorted(second.regions),
        "common_facts_exclude_region_specific_startAt": "startAt" not in first.facts,
        "region_facts_order_independent": rf(first, "jp") == rf(second, "jp")
            and rf(first, "en") == rf(second, "en"),
    }


def probe_factpacks(root: Path) -> dict:
    future = Entity(
        id="event:fixture", type="event", region="jp", regions=["jp"],
        names={"en": "UNRELEASED_FIXTURE"}, facts={"startAt": 1_900_000_000_000},
        source="master_db:jp", trust="A",
    )
    undated = Entity(
        id="event_story:fixture", type="event_story", region="jp", regions=["jp"],
        names={"en": "Fixture story"},
        facts={"outline_ja": "JAPANESE_FIXTURE", "outline_en": "ENGLISH_FIXTURE"},
        source="master_db:jp", trust="A",
    )
    future_result = build_fact_pack_at(future, as_of=1_700_000_000_000)
    undated_result = build_fact_pack_at(undated, as_of=1_700_000_000_000)
    requested_en = build_fact_pack(undated, language="en")
    return {
        "future_state": future_result["state"],
        "future_payload_contains_unreleased_name": "UNRELEASED_FIXTURE" in json.dumps(future_result),
        "undated_state": undated_result["state"],
        "undated_past_nonempty": bool(undated_result["past"]["text"]),
        "english_pack_uses_japanese_outline": "JAPANESE_FIXTURE" in requested_en.text,
    }


def probe_missing_translation(root: Path) -> dict:
    glossary = [GlossaryTerm(
        id="term:fixture", kind="term", canonical="JapaneseOnly",
        names={"ja": "JapaneseOnly"}, source="master_db:jp", official=True, trust="A",
    )]
    result = resolve_name(glossary, "JapaneseOnly", target_language="ko")[0]
    return {"requested": "ko", "target_name": result["target_name"], "target_exists": "ko" in glossary[0].names}


def probe_verification(root: Path) -> dict:
    entity = Entity(
        id="character:fixture", type="character", region="jp", regions=["jp"],
        names={"en": "Fixture Person"}, facts={}, source="master_db:jp", trust="A",
    )
    dbstore.save_entities(root, [entity])
    core = SekaiSyncCore(root)
    result = core.verify_claims([{"claim": "Fixture Person", "expected": "unknown profession"}])[0]
    return {"missing_fact_status": result["status"], "facts": result["matches"][0]["facts"]}


def probe_web(root: Path) -> dict:
    dbstore.initialize(root)
    pages = [
        {"id": "web:fixture:home_line:1", "source": "custom_sv", "source_type": "sekai_viewer",
         "instance": "custom_sv", "language": "ja", "kind": "home_line", "title": "alpha",
         "text": "", "crawled_at": "2026-01-02T00:00:00+00:00"},
        {"id": "web:fixture:event_story:1:1", "source": "custom_sv", "source_type": "sekai_viewer",
         "instance": "custom_sv", "language": "ja", "kind": "event_story", "title": "alpha",
         "text": "alpha body", "crawled_at": "2026-01-01T00:00:00+00:00"},
    ]
    dbstore.upsert_web_pages(root, "custom_sv", pages)
    traced: list[str] = []
    real_connect = dbstore.connect

    @contextlib.contextmanager
    def traced_connect(store_root: Path):
        with real_connect(store_root) as conn:
            conn.set_trace_callback(traced.append)
            yield conn

    with patch.object(dbstore, "connect", traced_connect):
        metadata = dbstore.load_web_index_rows(root)
    home = next(item for item in metadata if item["kind"] == "home_line")
    core = SekaiSyncCore(root)
    filtered = core.web_lookup("alpha", kind="event_story", limit=1)
    browsed = web_browse(root, kind="event_story", limit=1)
    with patch("sekaisync.webindex.flatten_web_pages", side_effect=RuntimeError("full-load-marker")):
        try:
            core.web_lookup("alpha", limit=1)
        except RuntimeError as exc:
            full_load = str(exc) == "full-load-marker"
        else:
            full_load = False
    return {
        "metadata_query": next(sql for sql in traced if "FROM web_pages ORDER BY" in sql),
        "metadata_preserves_source_type": "source_type" in home,
        "stored_trust": home["trust"],
        "recomputed_metadata_trust": trust_for_page(home),
        "search_kind_limit_one_count": len(filtered),
        "browse_same_kind_count": len(browsed),
        "lookup_calls_full_load": full_load,
    }


def probe_schema(root: Path) -> dict:
    """Observe what an unknown schema version does when a reader opens it.

    Astra's baseline recorded the silent downgrade ("99" -> "1") as DEFECT
    EVIDENCE. After the P07 fix the store is refused with SchemaVersionError
    instead, so this probe reports which behaviour is present rather than
    raising — otherwise the verifier aborts and later probes never run.
    """
    dbstore.initialize(root)
    with dbstore.connect(root) as conn:
        conn.execute("UPDATE meta SET value='99' WHERE key='schema_version'")
        conn.commit()
    refused = None
    try:
        dbstore.ensure_store(root)
    except Exception as exc:  # noqa: BLE001 - the refusal IS the observation
        refused = type(exc).__name__
    with dbstore.connect(root) as conn:
        version = conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()[0]
    return {
        "unsupported_version_before": "99",
        "version_after_ensure": version,
        # "True" means the P07 gate is working; "False" means the silent
        # downgrade has regressed.
        "refused_instead_of_downgraded": refused is not None,
        "refusal_error": refused,
    }


def probe_event_relationships(root: Path) -> dict:
    from sekaisync.event_detection import _merge_event_rows
    rows = [{"eventId": number, "musicId": number + 100, "seq": 1} for number in (1, 2, 3)]
    merged = _merge_event_rows([], rows, {"1", "2", "3"})
    return {"input_relationships": len(rows), "merged_relationships": len(merged), "remaining": merged}


def probe_activity_input(root: Path) -> dict:
    from sekaisync.eventalias import resolve_activity
    from sekaisync.worldlink import parse_wl_query
    result = resolve_activity(root, "khn1", regions=["jp"])
    try:
        parse_wl_query("wl" + "9" * 5000)
    except ValueError:
        long_input = "ValueError"
    else:
        long_input = "no error"
    return {"wl0": parse_wl_query("wl0"), "wl1000": parse_wl_query("wl1000"),
            "long_input": long_input, "empty_store_activity": result}


def probe_news_identity(root: Path) -> dict:
    from sekaisync.news import _news_key, merge_news
    first_key = _news_key("ja", "Maintenance", "fixture-first", "1")
    second_key = _news_key("ja", "Maintenance", "fixture-second", "2")
    new = {"id": "1", "canonical_key": first_key, "language": "ja", "source": "altsource_ms", "text": "new"}
    old = {**new, "text": "old and much longer fixture summary"}
    merged = merge_news([new, old])
    return {"distinct_ids_same_title_collide": first_key == second_key,
            "old_longer_revision_wins": merged[0]["text"] == old["text"]}


def probe_integrity_hash(root: Path) -> dict:
    from sekaisync.integrity import verify_web_integrity
    pages = [
        {"id": "fixture:event_story:1:1", "canonical_key": "event_story:ja:1:1", "source": source,
         "kind": "event_story", "language": "ja", "text": text}
        for source, text in (("fixture_a", "first text"), ("fixture_b", "different text"))
    ]
    with patch("sekaisync.integrity.flatten_web_pages", return_value=pages):
        result = verify_web_integrity(root)
    return {"different_text_conflict_groups": result["conflict_groups"], "mirror_duplicates": result["mirror_duplicates"]}


def probe_term_authority(root: Path) -> dict:
    from sekaisync.termindex import merge_terms
    from sekaisync.trinity import _glossary_name_index, arbitrate
    from sekaisync.cli import _names_from_conflict
    from sekaisync.normalize import normalize_name
    old = term("identity")
    old.names["en"] = "WrongName"
    old.source, old.trust = "llm", "C"
    official = term("identity")
    official.names["en"] = "CorrectName"
    official.source, official.trust, official.official = "master_db:jp", "A", True
    merged = merge_terms([old, official])[0]
    area = GlossaryTerm(id="area:fixture", kind="area", canonical="星庭", names={"ja": "星庭", "en": "STAR GARDEN"}, official=True)
    honor = GlossaryTerm(id="honor:fixture", kind="honor", canonical="星庭", names={"ja": "星庭", "en": "CANNED TUNA"}, official=True)
    glossary_result = _glossary_name_index([area, honor])[normalize_name("星庭")]
    conflict = arbitrate("fixture", {"en": {"SEKAI": ["translit"], "SEKAI2": ["translit"]}})["conflicts"][0]
    return {"merged_name": merged.names["en"], "merged_trust": merged.trust,
            "merged_official": merged.official, "merged_source": merged.source,
            "area_index_polluted_name": glossary_result["en"],
            "cli_conflict_proposal": _names_from_conflict(conflict)}


def probe_review_lifecycle(root: Path) -> dict:
    from sekaisync.agent_review import make_review_item, enqueue, submit_judgments, consult
    ordinary_root = root / "ordinary"
    item = make_review_item("FixtureTerm", "en", ["Good"])
    enqueue(ordinary_root, [item])
    ordinary = submit_judgments(ordinary_root, [{"id": item.id, "decision": "accept", "value": "Good"}])
    reused = consult(ordinary_root, "FixtureTerm", "en", ["Good"])
    requeued = enqueue(ordinary_root, [item])
    invalid_root = root / "invalid"
    enqueue(invalid_root, [item])
    invalid = submit_judgments(invalid_root, [{"id": item.id, "decision": "accept", "value": "Good", "generalize": "pattern", "pattern": "re:.*"}])
    replacement_root = root / "replacement"
    wrong = make_review_item("OtherTerm", "en", ["Wrong"])
    enqueue(replacement_root, [wrong])
    replacement = submit_judgments(replacement_root, [{"id": wrong.id, "decision": "replace", "value": "Right", "generalize": "pair"}])
    return {"ordinary_submit": ordinary, "ordinary_reused": reused, "ordinary_requeued": requeued,
            "invalid_pattern_submit": invalid, "replacement_submit": replacement,
            "replacement_lookup_original": consult(replacement_root, "OtherTerm", "en", ["Wrong"]),
            "replacement_lookup_empty": consult(replacement_root, "OtherTerm", "en", [])}


def inventory() -> dict:
    modules = []
    test_symbols: set[str] = set()
    for path in sorted((ROOT / "tests").glob("test_*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8-sig"))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                test_symbols.add(node.module or "")
                test_symbols.update(alias.name for alias in node.names)
            elif isinstance(node, ast.Import):
                test_symbols.update(alias.name for alias in node.names)
    for path in sorted((ROOT / "sekaisync").glob("*.py")):
        content = path.read_bytes()
        modules.append({"path": path.relative_to(ROOT).as_posix(), "lines": len(content.splitlines()), "sha256": hashlib.sha256(content).hexdigest()})
    return {
        "module_count": len(modules),
        "module_lines": sum(item["lines"] for item in modules),
        "test_file_count": len(list((ROOT / "tests").glob("test_*.py"))),
        "direct_trinity_import_in_tests": any("trinity" in symbol for symbol in test_symbols),
        "modules": modules,
    }


def main() -> int:
    probes = {
        "terms_snapshot_and_evidence": probe_terms,
        "aggregate_version_race": probe_cache,
        "region_facts_loss": probe_region_facts,
        "temporal_and_language_contract": probe_factpacks,
        "missing_translation_fallback": probe_missing_translation,
        "verification_absence_as_conflict": probe_verification,
        "web_read_filter_and_provenance": probe_web,
        "unsupported_schema_version": probe_schema,
        "event_relationship_identity": probe_event_relationships,
        "activity_inputs_and_unknowns": probe_activity_input,
        "news_identity_and_revision": probe_news_identity,
        "integrity_missing_hash": probe_integrity_hash,
        "term_authority_and_conflict_shape": probe_term_authority,
        "review_lifecycle": probe_review_lifecycle,
    }
    results = {}
    with patch("urllib.request.urlopen", side_effect=AssertionError("Network disabled in audit fixtures")):
        with tempfile.TemporaryDirectory(prefix="sekaisync-astra-audit-") as directory:
            base = Path(directory)
            for name, probe in probes.items():
                results[name] = probe(base / name)
    report = {
        "purpose": "Offline observations of baseline defects; not a post-fix regression test suite",
        "real_store_accessed": False,
        "settings_read": False,
        "python": platform.python_version(),
        "sqlite": sqlite3.sqlite_version,
        "results": results,
        "source_inventory": inventory(),
    }
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
