"""Read-only, reproducible term-quality evaluation with explicit denominators.

Run from the repository root: python scripts/evaluate_term_quality.py
Add --store store to measure withheld-annotation recall on a local corpus.
--code-root permits running the same cases against an archived implementation.
No production caches, slots, review decisions or source pages are written.
Synthetic regression accuracy is not an estimate of full-corpus accuracy.
"""
from __future__ import annotations

import argparse
import hashlib
import itertools
import json
from pathlib import Path
import sqlite3
import sys
import time
from types import SimpleNamespace
from unittest.mock import patch


def synthetic_cases():
    from sekaisync import termindex as ti, zhfirst as zh
    from sekaisync.normalize import normalize_name

    cases = []

    def check(name, actual, expected):
        cases.append(dict(case=name, passed=actual == expected,
                          actual=actual, expected=expected))

    for name in ("星砂薄荷巧克力", "蓝羽小夜曲纪念馆", "黎明之丘天文台"):
        got = {v for v, _ in zh.extract_zh_candidates_from_story(
            "我们参观" + name + "。", "synthetic", set(), set(), seed={name})}
        check("long_seed:" + name, name in got, True)
    for name in ("地下通道", "未来都市", "森之宫歌剧团"):
        check("internal_function_char:" + name,
              ti._source_candidate_acceptable(name, "zh_hans"), True)
    for text, expected in (("ORION时间要开始了", "ORION时间"),
                           ("「月虹音乐节」", "月虹音乐节")):
        got = {v for v, _ in zh.extract_zh_candidates_from_story(
            text, "synthetic", set(), set(), seed=set())}
        check("clean_surface:" + text, sorted(got), [expected])
    lexicon = {normalize_name("RAD"): dict(surface="RAD", kind="organization",
                                         names={"ja": "RAD"}, official=True)}
    check("latin_substring", ti.tokenize_ja("RADICAL", lexicon), [])

    # Twenty directed language pairs: the target mentions the same name in
    # an earlier, unrelated introduction. Returning its first occurrence is
    # a wrong sentence even though its spelling is correct.
    names = dict(ja="月虹音楽祭", en="Moonbow Festival", zh_hans="月虹音乐节",
                 zh_tw="月虹音樂節", ko="달무리 음악제")
    bodies = dict(ja="今日は{}へ行こう。", en="We will attend {} today.",
                  zh_hans="今天去参加{}。", zh_tw="今天去參加{}。",
                  ko="오늘은 {}에 가자.")
    for source in names:
        for target in names:
            if source == target:
                continue
            story = "event:99001:1"
            pages = []
            for language, name in names.items():
                lines = ["A1 START", "B2 " + bodies[language].format(name), "C3 END"]
                if language == target:
                    lines.insert(0, "INTRO99 " + name)
                pages.append(dict(id="fixture:" + language, story_key=story,
                                  canonical_key="event_story:" + language + ":99001:1",
                                  kind="event_story", language=language,
                                  text="\n".join(lines), trust="C"))
            groups = {story: {p["language"]: p for p in pages}}
            record = ti.TermRecord(id="term:fixture", canonical=names[source],
                                   source_language=source, names=names,
                                   evidence=[dict(story_key=story, language=source)])
            result = ti.term_penetrate([record], names[source], story_key=story,
                                       languages=[target], grouped=groups)
            sentence = result.get("per_language", {}).get(target, {}).get("sentence", "")
            check("same_position:" + source + "->" + target,
                  "B2 " in sentence and "INTRO99" not in sentence, True)

    # A target sentence with two equally supported unrelated names is not a
    # translation dictionary. The old longest-candidate tiebreak picked one.
    groups = {str(i): {"ja": {"text": "ネットパラダイスへ行こう。"},
                       "en": {"text": "Visit MoonHall and StarGallery."}}
              for i in range(2)}
    chosen = ti.align_term_by_frequency("ネットパラダイス", "ja", "en", groups,
                                        {("en", "MoonHall"): 1., ("en", "StarGallery"): 1.},
                                        vocab={"en": {"moonhall", "stargallery"}})
    check("ambiguous_translation", chosen, "")
    return dict(cases=len(cases), passed=sum(r["passed"] for r in cases),
                failed=sum(not r["passed"] for r in cases), results=cases)


def structural_coverage(groups, complete):
    """Measure available mappings, never mislabel them semantic correctness."""
    from sekaisync import termindex as ti
    try:
        from sekaisync.line_alignment import align_lines, alignment_session
    except ImportError:
        return dict(measured=False, reason="Implementation has no shared structural alignment API.")
    languages = ("ja", "en", "zh_hans", "zh_tw", "ko")
    rows = []
    started = time.perf_counter()
    with alignment_session():
        for key in sorted(complete):
            lines = {lang: tuple(line.strip() for line in ti._group_page(groups[key], lang)["text"].splitlines()
                                 if line.strip()) for lang in languages}
            for source, target in itertools.permutations(languages, 2):
                aligned = align_lines(lines[source], lines[target], source, target)
                rows.append(dict(story_key=key, source=source, target=target,
                                 source_lines=len(lines[source]),
                                 mapped_source_lines=sum(bool(indices) for indices in aligned.targets),
                                 speaker_turn_lines=sum(reason == "speaker_turn_sequence"
                                                        for reason in aligned.reasons)))
    total = sum(row["source_lines"] for row in rows)
    mapped = sum(row["mapped_source_lines"] for row in rows)
    return dict(measured=True, directed_story_language_pairs=len(rows),
                source_lines=total, mapped_source_lines=mapped,
                coverage=mapped / total if total else None,
                fully_mapped_pairs=sum(row["source_lines"] == row["mapped_source_lines"] for row in rows),
                elapsed_seconds=round(time.perf_counter() - started, 3),
                semantic_accuracy=None,
                note="Counts available structural mappings only; no independent translation gold and no correctness claim.",
                incomplete_pairs=[row for row in rows if row["source_lines"] != row["mapped_source_lines"]])


def local_annotation_recall(store: Path, annotations_path: Path):
    from sekaisync import termindex as ti, zhfirst as zh
    from sekaisync.normalize import normalize_name

    def label_key(value):
        # Annotation typography is not part of a lexical surface. Apply this
        # only to the scorer, consistently to both implementations and gold;
        # do not change runtime normalization or feed labels into extraction.
        text = str(value).strip()
        pairs = {'「': '」', '『': '』', '“': '”', '‘': '’', '"': '"'}
        while len(text) >= 2 and text[0] in pairs and text[-1] == pairs[text[0]]:
            text = text[1:-1].strip()
        return normalize_name(text)

    annotations = json.loads(annotations_path.read_text(encoding="utf-8"))
    gold = {key: values for key, values in annotations["stories"].items()
            if not key.endswith(":TITLE")}
    events = sorted({key.split(":")[1] for key in gold})
    database = (store / "kb" / "sekaisync.db").resolve()
    with sqlite3.connect(database.as_uri() + "?mode=ro", uri=True) as conn:
        conn.execute("PRAGMA query_only=ON")
        conn.row_factory = sqlite3.Row
        clauses = " OR ".join("id LIKE ?" for _ in events)
        pages = [dict(row) for row in conn.execute(
            "SELECT * FROM web_pages WHERE kind='event_story' AND auxiliary=0 AND (" + clauses + ")",
            tuple("%:event_story:" + event + ":%" for event in events))]
        glossary = [SimpleNamespace(**{**dict(row), "names": json.loads(row["names_json"])})
                    for row in conn.execute("SELECT kind,canonical,official,names_json FROM glossary_terms "
                                            "WHERE official=1 AND COALESCE(demo,0)=0")]
        slot_counts = dict(conn.execute("SELECT status,count(*) FROM term_slots GROUP BY status"))
    groups = ti.group_pages_by_story(pages)
    required = {"ja", "en", "zh_hans", "zh_tw", "ko"}
    complete = {key for key, by in groups.items()
                if all(ti._group_page(by, language) is not None for language in required)}
    # Deliberately withhold all manual annotation seeds, not just the current
    # story's seeds (names repeat across chapters). Official metadata remains
    # available as a documented production input. Overall recall includes its
    # dictionary hits; report their labelled subset separately from discovery.
    complete_pages = [page for key, by in groups.items() if key in complete for page in by.values()]
    with patch.object(zh, "_load_manual_seed", return_value=set()):
        found = zh.extract_terms_zhfirst(complete_pages, [], glossary, do_align=False)
    actual = {label_key(t.canonical) for t in found}
    actual_by_story = {}
    for term in found:
        for key in term.stories:
            actual_by_story.setdefault(key, set()).add(label_key(term.canonical))
    expected = set()
    expected_by_story = {}
    missing_stories = []
    for key, values in gold.items():
        if key not in complete:
            continue
        page = ti._group_page(groups.get(key, {}), "zh_hans")
        if page is None:
            missing_stories.append(key)
            continue
        text = normalize_name(page["text"])
        expected_by_story[key] = {label_key(v) for v in values if label_key(v) in text}
        expected.update(expected_by_story[key])
    present = actual & expected
    official_metadata_labels = {label_key(entry.names.get("zh_hans", "")) for entry in glossary} & expected
    official_labels = {label_key(entry.names.get("zh_hans", "")) for entry in glossary
                       if entry.kind in ti.NOUN_KINDS} & expected
    story_terms = sum(len(values) for values in expected_by_story.values())
    recovered_story_terms = sum(len(values & actual_by_story.get(key, set()))
                                for key, values in expected_by_story.items())
    digest = hashlib.sha256(json.dumps(
        [[key, {lang: p["text"] for lang, p in sorted(by.items())}]
         for key, by in sorted(groups.items())], ensure_ascii=False).encode()).hexdigest()
    return dict(annotated_stories=len(gold), loaded_stories=len(groups),
                five_language_complete_stories=len(complete), missing_stories=missing_stories,
                evaluation_scope="Only stories with usable local text in all five languages.",
                excluded_incomplete_stories=sorted(set(gold) - complete),
                manual_seeds_used=False, official_metadata_used=True,
                labelled_terms_present_in_source=len(expected), recovered_terms=len(present),
                recall=len(present) / len(expected) if expected else None,
                recall_unit="distinct labelled lexical surfaces, ignoring paired surrounding quotes",
                official_dictionary_labelled_terms=len(official_labels),
                official_dictionary_recovered_terms=len(official_labels & actual),
                official_metadata_surface_matches=len(official_metadata_labels),
                other_labelled_terms=len(expected - official_labels),
                other_recovered_terms=len(present - official_labels),
                labelled_story_term_pairs=story_terms,
                recovered_story_term_pairs=recovered_story_terms,
                story_term_recall=recovered_story_terms / story_terms if story_terms else None,
                candidate_terms=len(actual), unlabelled_terms=len(actual - expected),
                missing_terms=sorted(expected - actual),
                precision=None, precision_note="Annotations are not exhaustive; extras are unlabelled.",
                independence_note="Existing repository labels; direct seed leakage is withheld, but prior tuning may have seen these labels.",
                cross_language_semantic_accuracy=None,
                cross_language_note="No exhaustive independently labelled five-language term-span gold is available.",
                structural_alignment=structural_coverage(groups, complete),
                annotations_sha256=hashlib.sha256(annotations_path.read_bytes()).hexdigest(),
                glossary_sha256=hashlib.sha256(json.dumps([
                    [entry.kind, entry.canonical, list(entry.names.items())] for entry in glossary
                ], ensure_ascii=False).encode()).hexdigest(),
                corpus_sha256=digest, existing_slot_counts=slot_counts)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--code-root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--store", type=Path)
    parser.add_argument("--annotations", type=Path,
                        default=Path(__file__).resolve().parents[1] / "data/term-annotations.json")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    sys.path.insert(0, str(args.code_root.resolve()))
    started = time.perf_counter()
    report = dict(schema="sekaisync/term-quality-evaluation@1",
                  implementation=str(args.code_root.resolve()),
                  synthetic=synthetic_cases(),
                  claim="Regression and labelled recall only; not proof of zero corpus errors.")
    if args.store is not None:
        report["annotation_recall"] = local_annotation_recall(args.store, args.annotations)
    report["full_coverage_zero_error_acceptance"] = dict(
        demonstrated=False,
        reason="Targeted regressions and partial source-language labels cannot certify every term across all twenty directed language pairs.")
    report["elapsed_seconds"] = round(time.perf_counter() - started, 3)
    output = json.dumps(report, ensure_ascii=False, indent=2)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(output + "\n", encoding="utf-8")
    print(output)


if __name__ == "__main__":
    main()
