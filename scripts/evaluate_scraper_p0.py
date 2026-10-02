"""Score the complete, frozen P0 discovery manifest without writing its store.

Only the scorer reads repository annotations. Candidate review ledgers contain
source evidence and provenance, never labels or a claimed semantic verdict.
Existing labels are partial and may have informed prior tuning; accepted agent
receipts are model judgments, not independently adjudicated gold.
"""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from contextlib import contextmanager
import hashlib
import json
from pathlib import Path
import sqlite3
import sys
from types import SimpleNamespace
import unicodedata
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from sekaisync import agent_packets as ap, agent_review as ar, termindex as ti, zhfirst as zh
from sekaisync.line_alignment import (
    _fold, _term_boundaries, _wrapped_term_pattern, strip_speaker_label,
    source_term_occurrences,
)
from sekaisync.normalize import normalize_name


def label_surface(value):
    value = str(value).strip()
    pairs = {'\u300c': '\u300d', '\u300e': '\u300f', '\u201c': '\u201d',
             '\u2018': '\u2019', '"': '"'}
    while len(value) >= 2 and value[0] in pairs and value[-1] == pairs[value[0]]:
        value = value[1:-1].strip()
    return value


def label_key(value):
    return normalize_name(label_surface(value))


def _read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _write_json(path, value):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


@contextmanager
def _readonly(store):
    database = (Path(store) / "kb/sekaisync.db").resolve()
    conn = sqlite3.connect(database.as_uri() + "?mode=ro", uri=True)
    try:
        conn.execute("PRAGMA query_only=ON")
        conn.row_factory = sqlite3.Row
        yield conn
    finally:
        conn.close()


def body_spans(text, surface):
    """Return all exact body spans with original offsets and soft-wrap support."""
    body, positions, offset = [], [], 0
    for line in text.splitlines(keepends=True):
        stripped = strip_speaker_label(line)
        start = offset + len(line) - len(stripped)
        body.extend(stripped)
        positions.extend(range(start, start + len(stripped)))
        offset += len(line)
    body = "".join(body)
    needle = _fold(surface).strip()
    if not needle:
        return []
    folded, starts, ends = [], [], []
    i = 0
    while i < len(body):
        end = i + 1
        while end < len(body) and (unicodedata.combining(body[end]) or body[end] in "\uff9e\uff9f"):
            end += 1
        value = _fold(body[i:end])
        folded.extend(value)
        starts.extend([positions[i]] * len(value))
        ends.extend([positions[end - 1] + 1] * len(value))
        i = end
    comparable = "".join(folded)
    return [(starts[m.start()], ends[m.end() - 1])
            for m in _wrapped_term_pattern(surface).finditer(comparable)
            if m.end() > m.start() and _term_boundaries(comparable, m.start(), m.end(), needle)]


def assess_tasks(tasks, receipts, *, conn=None, store=None):
    """Receipt presence alone is not completion: verify identity and evidence."""
    seen, rows, scope_cache, page_cache = set(), [], {}, {}
    for task in tasks:
        identity = task.get("id")
        if not identity or identity in seen:
            raise ValueError("manifest task IDs must be nonempty and unique")
        seen.add(identity)
        context = task.get("review_context") or {}
        if task.get("kind") != "discovery" or context.get("task") != "discovery":
            raise ValueError("P0 manifest must contain complete discovery ReviewItems only")
        status, reason, terms = "missing", None, []
        receipt = receipts.get(identity)
        try:
            expected_id = "arp:" + ap._digest([task["term"], task["language"],
                                               sorted(set(task.get("candidates", []))), context])[:32]
            if identity != expected_id:
                raise ValueError("manifest task identity does not match context")
            if not context.get("rows") or context.get("schema") != ap._SCHEMA:
                raise ValueError("manifest has no valid source evidence")
        except (KeyError, TypeError, ValueError) as exc:
            status, reason = "invalid", str(exc)
        if status != "invalid" and conn is not None:
            try:
                scope_id = context["scope_id"]
                if scope_id not in scope_cache:
                    scope = ap._read_scope(store, scope_id)
                    scope_cache[scope_id] = {row["id"]: row for row in scope["windows"]}
                for row in context["rows"]:
                    if row != scope_cache[scope_id].get(row["id"]):
                        raise ValueError("task row differs from immutable scope")
                    ap._validate_view(conn, row["source"], page_cache)
                if context.get("continuation"):
                    ap._validate_discovery_continuation(store, ar.ReviewItem.from_dict(task), conn=conn)
            except (KeyError, TypeError, ValueError, OSError) as exc:
                status, reason = "stale", str(exc)
        if status not in {"invalid", "stale"} and receipt is not None:
            if not isinstance(receipt, dict):
                status, reason = "invalid", "receipt is not an object"
            elif receipt.get("scope_id") != context.get("scope_id"):
                status, reason = "stale", "receipt refers to another scope"
            else:
                try:
                    terms = json.loads(receipt["value"])
                    if receipt.get("decision") != "accept" or not isinstance(terms, list):
                        raise ValueError("discovery receipt must accept a source-surface array")
                    if len(terms) > 200 or any(not ap._valid_surface(term) for term in terms):
                        raise ValueError("receipt contains invalid discovery surfaces")
                    for term in terms:
                        if ap._discovery_surface_key(term) in {
                                ap._discovery_surface_key(value) for value in
                                (context.get("continuation") or {}).get("excluded_terms", [])}:
                            raise ValueError("receipt repeats a continuation exclusion")
                        if not any(source_term_occurrences(row["source"]["text"].splitlines(), term)
                                   for row in context["rows"]):
                            raise ValueError("receipt surface is not grounded in task body: " + term)
                    status = "completed"
                    terms = list(dict.fromkeys(terms))
                except (KeyError, TypeError, ValueError) as exc:
                    status, reason, terms = "invalid", str(exc), []
        if status != "completed":
            terms = []
        rows.append(dict(id=identity, status=status, reason=reason, terms=terms,
                         receipt_present=receipt is not None,
                         story_keys=sorted({row["story_key"] for row in context.get("rows", [])}),
                         source_windows=len(context.get("rows", [])),
                         continuation_parent_id=(context.get("continuation") or {}).get("parent_id"),
                         continuation_root_id=(context.get("continuation") or {}).get("root_id")))
    return rows


def expand_continuations(tasks, receipts):
    """Reconstruct due continuation IDs even after a completed child left queue."""
    expanded = list(tasks)
    seen = {task["id"] for task in expanded}
    for task in expanded:
        context = task["review_context"]
        receipt = receipts.get(task["id"])
        if (not isinstance(receipt, dict) or receipt.get("decision") != "accept"
                or receipt.get("scope_id") != context.get("scope_id")):
            continue
        try:
            terms = json.loads(receipt["value"])
        except (KeyError, TypeError, ValueError):
            continue
        if (not isinstance(terms, list) or len(terms) != ap._DISCOVERY_TERMS
                or any(not ap._valid_surface(term) for term in terms)
                or len({ap._discovery_surface_key(term) for term in terms}) != ap._DISCOVERY_TERMS):
            continue
        excluded = {ap._discovery_surface_key(term) for term in
                    (context.get("continuation") or {}).get("excluded_terms", [])}
        if any(ap._discovery_surface_key(term) in excluded or not any(source_term_occurrences(
                row["source"]["text"].splitlines(), term) for row in context["rows"]) for term in terms):
            continue
        child_context = ap._continuation_context(context, task["id"], terms)
        child = ap._item(task["term"], task["language"], [], "discovery", child_context, "")
        if child.id not in seen:
            expanded.append(child.to_dict())
            seen.add(child.id)
    return expanded


def task_group(statuses, identities):
    wanted = set(identities)
    indexed = {row["id"]: row for row in statuses}
    if not wanted <= indexed.keys():
        raise ValueError("task group refers to IDs outside the complete manifest")
    selected = [indexed[identity] for identity in sorted(wanted)]
    counts = Counter(row["status"] for row in selected)
    return dict(total=len(selected), **{status: counts[status] for status in
                                      ("completed", "missing", "stale", "invalid")},
                discovered_surfaces=len({label_key(term) for row in selected for term in row["terms"]}))


def _candidate_sets(tasks, statuses, automatic):
    baseline, baseline_by_story = set(), defaultdict(set)
    for record in automatic:
        key = label_key(record["canonical"])
        baseline.add(key)
        for story in record.get("stories", []):
            baseline_by_story[story].add(key)
    current, current_by_story = set(), defaultdict(set)
    indexed = {task["id"]: task for task in tasks}
    for status in statuses:
        if status["status"] != "completed":
            continue
        for surface in status["terms"]:
            key = label_key(surface)
            current.add(key)
            for row in indexed[status["id"]]["review_context"]["rows"]:
                if source_term_occurrences(row["source"]["text"].splitlines(), surface):
                    current_by_story[row["story_key"]].add(key)
    return baseline, baseline_by_story, current, current_by_story


def score_labels(tasks, statuses, automatic, source_pages, annotations, legacy_ids=()):
    baseline, baseline_by_story, current, current_by_story = _candidate_sets(tasks, statuses, automatic)
    gold_by_story, variants, strict_spans, excluded = {}, {}, {}, []
    for story, page in sorted(source_pages.items()):
        wanted, spellings = set(), defaultdict(set)
        for surface in annotations.get(story, []):
            key = label_key(surface)
            raw = label_surface(surface)
            if not key or key not in normalize_name(page["text"]):
                continue
            wanted.add(key)
            spellings[key].add(raw)
        gold_by_story[story] = wanted
        variants[story] = spellings
        for key in wanted:
            spans = sorted({span for raw in spellings[key] for span in body_spans(page["text"], raw)})
            strict_spans[story, key] = spans
            if not spans:
                excluded.append(dict(story_key=story, label=key, surfaces=sorted(spellings[key]),
                                     reason="legacy_normalized_match_has_no_exact_source_body_span"))
    gold = set().union(*gold_by_story.values()) if gold_by_story else set()
    pairs = sum(len(values) for values in gold_by_story.values())
    before_pairs = sum(len(values & baseline_by_story[story]) for story, values in gold_by_story.items())
    after_pairs = sum(len(values & (baseline_by_story[story] | current_by_story[story]))
                      for story, values in gold_by_story.items())
    strict_gold = {key for (story, key), spans in strict_spans.items() if spans}
    strict_pairs = sum(bool(spans) for spans in strict_spans.values())
    strict_before_pairs = sum(bool(spans) and key in baseline_by_story[story]
                              for (story, key), spans in strict_spans.items())
    strict_after_pairs = sum(bool(spans) and key in (baseline_by_story[story] | current_by_story[story])
                             for (story, key), spans in strict_spans.items())
    legacy_statuses = [row for row in statuses if row["id"] in set(legacy_ids)]
    legacy_found = {label_key(term) for row in legacy_statuses for term in row["terms"]}

    def metrics(expected, baseline_found, agent_found, expected_pairs, first_pairs, final_pairs):
        return dict(labelled_surface_types=len(expected), baseline_found=len(expected & baseline_found),
                    agent_additional_labelled=len((expected & agent_found) - baseline_found),
                    combined_found=len(expected & (baseline_found | agent_found)),
                    baseline_recall=len(expected & baseline_found) / len(expected) if expected else None,
                    combined_recall=len(expected & (baseline_found | agent_found)) / len(expected) if expected else None,
                    labelled_story_term_pairs=expected_pairs, baseline_story_term_pairs=first_pairs,
                    combined_story_term_pairs=final_pairs,
                    baseline_story_term_recall=first_pairs / expected_pairs if expected_pairs else None,
                    combined_story_term_recall=final_pairs / expected_pairs if expected_pairs else None)

    coverage = defaultdict(list)
    status_by_id = {row["id"]: row for row in statuses}
    for task in tasks:
        for row in task["review_context"]["rows"]:
            view = row["source"]
            coverage[row["story_key"]].append((task["id"], view))
    residuals = []
    for story, values in sorted(gold_by_story.items()):
        for key in sorted(values - (baseline_by_story[story] | current_by_story[story])):
            spans = strict_spans[story, key]
            page = source_pages[story]
            matching = sorted({identity for identity, view in coverage[story]
                               if view["page_id"] == page["id"] and view["source"] == page["source"]
                               and any(view["start"] <= start and end <= view["end"] for start, end in spans)})
            observations = ["automatic_extractor_did_not_emit_label_for_story"]
            if not spans:
                observations.append("legacy_label_has_no_exact_source_body_span")
            elif not matching:
                observations.append("source_span_not_covered_by_manifest_window")
            else:
                states = {status_by_id[identity]["status"] for identity in matching}
                observations.extend("covered_by_" + status + "_task" for status in sorted(states))
                if "completed" in states:
                    observations.append("completed_covering_task_did_not_emit_label")
            if key in current:
                observations.append("agent_discovered_label_in_other_story_only")
            residuals.append(dict(story_key=story, label=key, surfaces=sorted(variants[story][key]),
                                 source_page_id=page["id"], source_spans=[dict(start=s, end=e,
                                 text=page["text"][s:e]) for s, e in spans], covering_task_ids=matching,
                                 observations=observations, root_cause="unknown",
                                 type_recovered_elsewhere=key in (baseline | current)))
    report = dict(
        legacy_compatible=metrics(gold, baseline, current, pairs, before_pairs, after_pairs),
        exact_source_body=metrics(strict_gold, baseline, current, strict_pairs,
                                  strict_before_pairs, strict_after_pairs),
        legacy_six_sample=dict(labelled_surface_types=len(gold), baseline_found=len(gold & baseline),
                               agent_additional_labelled=len((gold & legacy_found) - baseline),
                               combined_found=len(gold & (baseline | legacy_found)),
                               combined_recall=len(gold & (baseline | legacy_found)) / len(gold) if gold else None),
        missing_surface_types=len(gold - (baseline | current)),
        gold_surface_type_sha256=ap._digest(sorted(gold)),
        missing_story_term_pairs=len(residuals),
        mechanical_residual_observations=dict(Counter(observation for row in residuals
                                                     for observation in row["observations"])),
        unknown_root_cause_story_term_pairs=len(residuals),
        legacy_denominator_without_exact_body_span=len(excluded),
        precision=None, cross_language_semantic_accuracy=None,
        model_judgments_are_independent_gold=False,
        labels_are_exhaustive=False, prior_tuning_may_have_seen_labels=True,
        lexical_surface_normalization="normalize_name after paired typography removal; not sense identity",
        story_term_unit="one labelled surface type in one story, not a count of physical occurrences")
    return report, residuals


def candidate_ledger(tasks, statuses, automatic, source_pages, legacy_ids=()):
    """Blind adjudication input: candidates and spans, with no annotation match."""
    entries, legacy = {}, set(legacy_ids)

    def add(surface, story, origin, task_id=None, view=None):
        page = source_pages.get(story)
        if not page:
            return
        identity = (label_key(surface), story)
        entry = entries.setdefault(identity, dict(surface_key=identity[0], surfaces=[], story_key=story,
                                  source_page_id=page["id"], source_language=page["language"],
                                  page_sha256=ap._page_hash(page), origins=[], task_ids=[], source_spans=[],
                                  adjudication="unreviewed", judgment_is_independent_gold=False))
        if surface not in entry["surfaces"]:
            entry["surfaces"].append(surface)
        if origin not in entry["origins"]:
            entry["origins"].append(origin)
        if task_id and task_id not in entry["task_ids"]:
            entry["task_ids"].append(task_id)
        text = view["text"] if view else page["text"]
        base = view["start"] if view else 0
        spans = body_spans(text, surface)
        for start, end in spans:
            start, end = start + base, end + base
            span = dict(start=start, end=end, text=page["text"][start:end])
            if span not in entry["source_spans"]:
                entry["source_spans"].append(span)

    for record in automatic:
        for story in record.get("stories", []):
            add(record["canonical"], story, "automatic_baseline")
    indexed = {task["id"]: task for task in tasks}
    for status in statuses:
        if status["status"] != "completed":
            continue
        if status.get("continuation_parent_id"):
            origin = "agent_discovery_continuation"
        else:
            origin = "legacy_six_agent_discovery" if status["id"] in legacy else "new_agent_discovery"
        for term in status["terms"]:
            for row in indexed[status["id"]]["review_context"]["rows"]:
                if source_term_occurrences(row["source"]["text"].splitlines(), term):
                    add(term, row["story_key"], origin, status["id"], row["source"])
    for entry in entries.values():
        entry["surfaces"].sort()
        entry["origins"].sort()
        entry["task_ids"].sort()
        entry["source_spans"].sort(key=lambda span: (span["start"], span["end"]))
    return [entries[key] for key in sorted(entries)]


def automatic_baseline(pages, glossary, output, *, recompute=False):
    fingerprint = ap._digest(dict(
        pages=[[p["id"], p["text"]] for p in sorted(pages, key=lambda p: p["id"])],
        glossary=[[g.kind, g.canonical, g.official, g.names] for g in glossary],
        implementation={str(path.relative_to(ROOT)): hashlib.sha256(path.read_bytes()).hexdigest()
                        for path in sorted((ROOT / "sekaisync").rglob("*.py"))}))
    path = Path(output) / "automatic-baseline.json"
    if path.exists() and not recompute:
        cached = _read_json(path)
        if cached.get("fingerprint") == fingerprint:
            return cached["records"], fingerprint
    with patch.object(zh, "_load_manual_seed", return_value=set()):
        terms = zh.extract_terms_zhfirst(pages, [], glossary, do_align=False)
    records = [dict(canonical=t.canonical, stories=sorted(t.stories), source_language="zh_hans",
                    official=t.official) for t in terms]
    _write_json(path, dict(fingerprint=fingerprint, manual_annotation_seeds_used=False, records=records))
    return records, fingerprint


def run(evaluation, output, *, manifest_path=None, baseline_store=None, annotations_path=None,
        recompute_baseline=False):
    evaluation, output = Path(evaluation), Path(output)
    manifest_path = Path(manifest_path or evaluation / "discovery-manifest.json")
    manifest = _read_json(manifest_path)
    if manifest.get("schema") != "sekaisync/p0-discovery-manifest@1":
        raise ValueError("unsupported P0 manifest schema")
    original_tasks = manifest["tasks"]
    store = evaluation / "evaluation-store"
    receipts = ap._receipts(store)
    tasks = expand_continuations(original_tasks, receipts)
    with _readonly(store) as conn:
        pages = [dict(row) for row in conn.execute("SELECT * FROM web_pages")]
        statuses = assess_tasks(tasks, receipts, conn=conn, store=store)
        table_names = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        evidence_count = conn.execute("SELECT count(*) FROM term_evidence").fetchone()[0] if "term_evidence" in table_names else 0
        names = [json.loads(row[0]) for row in conn.execute("SELECT names_json FROM terms")] if "terms" in table_names else []
    with _readonly(baseline_store or ROOT / "store") as conn:
        glossary = [SimpleNamespace(**{**dict(row), "names": json.loads(row["names_json"])})
                    for row in conn.execute("SELECT kind,canonical,official,names_json FROM glossary_terms "
                                            "WHERE official=1 AND COALESCE(demo,0)=0")]
    story_keys = {row["story_key"] for task in tasks for row in task["review_context"]["rows"]}
    groups = ti.group_pages_by_story(pages)
    source_language = tasks[0]["review_context"]["source_language"] if tasks else "zh_hans"
    source_pages = {story: ti._group_page(groups.get(story, {}), source_language) for story in story_keys}
    if any(not page for page in source_pages.values()):
        raise ValueError("a manifest story has no current source page")
    scoped_pages = [page for story in sorted(story_keys) for page in groups[story].values()]
    current_corpus_hash = hashlib.sha256(json.dumps(
        [[p["id"], p["text"]] for p in sorted(scoped_pages, key=lambda p: p["id"])],
        ensure_ascii=False).encode()).hexdigest()
    if (manifest.get("corpus_sha256") is not None
            and manifest["corpus_sha256"] != current_corpus_hash):
        raise ValueError("current corpus differs from frozen manifest; refusing labelled recall on a changed denominator")
    automatic, baseline_fingerprint = automatic_baseline(scoped_pages, glossary, output,
                                                         recompute=recompute_baseline)
    state_path = evaluation / "scoring-state.json"
    state_sha256 = None
    if state_path.exists():
        state = _read_json(state_path)
        if {label_key(record["canonical"]) for record in automatic} != set(state["automatic"]):
            raise ValueError("automatic baseline differs from frozen scoring-state; do not silently change the P0 baseline")
        state_sha256 = hashlib.sha256(state_path.read_bytes()).hexdigest()
    annotations_path = Path(annotations_path or ROOT / "data/term-annotations.json")
    annotations = _read_json(annotations_path)["stories"]
    legacy_ids = manifest.get("legacy_sampled_ids", [])
    labels, residuals = score_labels(tasks, statuses, automatic, source_pages, annotations, legacy_ids)
    if state_path.exists() and labels["gold_surface_type_sha256"] != ap._digest(sorted(state["gold"])):
        raise ValueError("annotation denominator differs from frozen scoring-state")
    ledger = candidate_ledger(tasks, statuses, automatic, source_pages, legacy_ids)
    original_ids = {task["id"] for task in original_tasks}
    continuation_ids = [task["id"] for task in tasks if task["id"] not in original_ids]
    report = dict(schema="sekaisync/p0-scraper-evaluation@1", model_mode="current_host_agent",
                  model_api_calls=0, production_store_written=False, evaluation_store_written=False,
                  complete_stories=len(story_keys), pages=len(scoped_pages),
                  manifest_sha256=hashlib.sha256(manifest_path.read_bytes()).hexdigest(),
                  annotations_sha256=hashlib.sha256(annotations_path.read_bytes()).hexdigest(),
                  automatic_baseline_fingerprint=baseline_fingerprint,
                  frozen_scoring_state_sha256=state_sha256,
                  frozen_corpus_sha256=manifest.get("corpus_sha256"), current_corpus_sha256=current_corpus_hash,
                  corpus_matches_frozen_manifest=manifest.get("corpus_sha256") == current_corpus_hash,
                  discovery=dict(all_tasks=task_group(statuses, original_ids),
                                 legacy_six_sample=task_group(statuses, legacy_ids),
                                 remaining_tasks=task_group(statuses, [task["id"] for task in original_tasks
                                                                       if task["id"] not in set(legacy_ids)]),
                                 continuation_tasks=task_group(statuses, continuation_ids),
                                 all_receipt_tasks=task_group(statuses, [task["id"] for task in tasks]),
                                 source_windows=sum(len(task["review_context"]["rows"]) for task in original_tasks)),
                  labelled_recall=labels, candidate_review_rows=len(ledger),
                  candidate_review_rows_without_exact_span=sum(not row["source_spans"] for row in ledger),
                  actual_target_slots_written=sum(sum(language != source_language for language in row) for row in names),
                  evidence_rows=evidence_count, target_slot_count_is_semantic_accuracy=False,
                  independent_semantic_accuracy=None, zero_errors_demonstrated=False,
                  scope_note="Frozen local five-language corpus, not an independently verified five-server release census.",
                  gold_note="Existing partial repository labels, scorer-only. Model-written discoveries and prior model reviews are not independent gold.")
    _write_json(output / "evaluation-report.json", report)
    _write_json(output / "task-statuses.json", statuses)
    _write_json(output / "gold-residuals-scorer-only.json", dict(items=residuals, never_feed_to_discovery=True))
    output.mkdir(parents=True, exist_ok=True)
    (output / "candidate-review-ledger.jsonl").write_text(
        "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in ledger), encoding="utf-8")
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--evaluation", type=Path, default=ROOT / "work/p0-exhaustive-20261001")
    parser.add_argument("--manifest", type=Path)
    parser.add_argument("--baseline-store", type=Path, default=ROOT / "store")
    parser.add_argument("--annotations", type=Path, default=ROOT / "data/term-annotations.json")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--recompute-baseline", action="store_true")
    args = parser.parse_args()
    report = run(args.evaluation, args.output or args.evaluation / "evaluator",
                 manifest_path=args.manifest, baseline_store=args.baseline_store,
                 annotations_path=args.annotations, recompute_baseline=args.recompute_baseline)
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
