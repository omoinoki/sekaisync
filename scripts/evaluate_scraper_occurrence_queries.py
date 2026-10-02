"""Exercise the unchanged public query against frozen contextual proposals.

This checks interface consumption, not independent semantic truth. Proposals
and independent reviews remain separate artifacts with separate denominators.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def _code_hashes(root):
    paths = [root / "scripts/evaluate_scraper_occurrence_queries.py",
             *sorted((root / "sekaisync").rglob("*.py"))]
    return {path.relative_to(root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in paths}


_IMPORT_ROOT = ROOT
_IMPORT_HASHES = _code_hashes(ROOT)

from sekaisync import dbstore
from sekaisync.core import SekaiSyncCore


def _evaluate(store, proposal):
    core = SekaiSyncCore(store)
    with core.request_view():
        return _evaluate_snapshot(store, proposal, core)


def _evaluate_snapshot(store, proposal, core):
    requirements, queries = [], []
    for concept in proposal["concepts"]:
        for source, surface in concept["surfaces"].items():
            languages = list(concept["surfaces"])
            result = core.term_penetrate(surface, story_key=proposal["story_key"], languages=languages)
            queries.append(dict(concept=concept["key"], source=source, query=surface, result=result))
            for target, expected in concept["surfaces"].items():
                if source == target:
                    continue
                nonlexical = "paraphrase" in {concept.get("relations", {}).get(source),
                                               concept.get("relations", {}).get(target)}
                entry = result["per_language"].get(target, {}) if result else {}
                actual = entry.get("term", "")
                status = ("typed_nonlexical" if nonlexical and not actual and entry.get("missing")
                          and "paraphrase" in entry.get("note", "") else
                          "covered" if not nonlexical and actual == expected and not entry.get("missing") else
                          "incorrect_scalar" if actual else "unavailable")
                requirements.append(dict(concept=concept["key"], source=source, target=target,
                                         expected=expected, expected_kind="paraphrase" if nonlexical else "lexical",
                                         actual=actual, status=status, note=entry.get("note", ""),
                                         returned_source_language=result["term"]["source_language"] if result else None))
    with dbstore.connect(store) as conn:
        ledger_count = conn.execute("SELECT COUNT(*) FROM scraper_relations").fetchone()[0]
        terms = conn.execute("SELECT COUNT(*) FROM terms").fetchone()[0]
        revision = dbstore.current_revision(conn)
    return dict(schema="sekaisync/occurrence-public-query-evaluation@1",
                scope="Frozen proposals through the unchanged Core.term_penetrate endpoint, one full-language response per source",
                semantic_gold=False, story_key=proposal["story_key"], store=str(store.resolve()),
                data_revision=revision, ledger_relations=ledger_count, global_terms=terms,
                source_queries=len(queries), requirements=len(requirements),
                lexical_requirements=sum(row["expected_kind"] == "lexical" for row in requirements),
                nonlexical_requirements=sum(row["expected_kind"] != "lexical" for row in requirements),
                covered=sum(row["status"] == "covered" for row in requirements),
                typed_nonlexical=sum(row["status"] == "typed_nonlexical" for row in requirements),
                unavailable=sum(row["status"] == "unavailable" for row in requirements),
                incorrect_scalar=sum(row["status"] == "incorrect_scalar" for row in requirements),
                requirements_detail=requirements, queries=queries)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--store", type=Path, required=True)
    parser.add_argument("--proposal", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists():
        raise FileExistsError("query evaluation output is frozen; choose a new output path")
    proposal_bytes = args.proposal.read_bytes()
    proposal = json.loads(proposal_bytes.decode("utf-8"))
    code_hashes = _code_hashes(ROOT)
    if ROOT == _IMPORT_ROOT and code_hashes != _IMPORT_HASHES:
        raise RuntimeError("query implementation changed after import; restart the evaluator")
    report = _evaluate(args.store, proposal)
    report["proposal_sha256"] = hashlib.sha256(proposal_bytes).hexdigest()
    if _code_hashes(ROOT) != code_hashes:
        raise RuntimeError("query implementation changed during evaluation; no mixed-code report is published")
    if args.proposal.read_bytes() != proposal_bytes:
        raise RuntimeError("frozen proposal changed during evaluation; no report is published")
    report["code_sha256"] = code_hashes
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("x", encoding="utf-8") as output:
        output.write(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps({key: value for key, value in report.items()
                      if key not in {"requirements_detail", "queries", "code_sha256"}}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
