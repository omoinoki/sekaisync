"""Execute the released scalar scraper in isolation, never synthesize typed output."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--code-root", type=Path, required=True)
    parser.add_argument("--pages", type=Path, required=True)
    parser.add_argument("--queries", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    code = args.code_root.resolve()
    sys.path = [str(code)] + [path for path in sys.path if path and not Path(path).resolve().is_relative_to(code.parents[3])]
    from sekaisync import dbstore, termindex, trinity
    from sekaisync.core import SekaiSyncCore
    import sekaisync
    if not Path(sekaisync.__file__).resolve().is_relative_to(code):
        raise ValueError("baseline imported current rather than released code")
    pages = json.loads(args.pages.read_bytes())
    groups = termindex.group_pages_by_story(pages)
    results, outputs = {}, {}
    languages = ("en", "ja", "ko", "zh_hans", "zh_tw")
    for language in ("en", "ja", "ko", "zh_hans", "zh_tw"):
        store = args.out.parent / language / "store"
        dbstore.initialize_new_store(store, target_version=3)
        for provider in sorted({page["source"] for page in pages}):
            dbstore.upsert_web_pages(store, provider, [page for page in pages if page["source"] == provider])
        targets = tuple(item for item in languages if item != language)
        glossary = list(dbstore.load_glossary_terms(store))
        vocab, idf = termindex.build_alignment_resources(groups, targets, glossary, store / "cache/trinity")
        pair_index = termindex.build_pair_story_index(groups, targets, language)
        candidates, discovered = trinity.build_candidate_pool(groups, list(groups), source_language=language)
        raw = trinity.scrub_trinity(groups, list(groups), candidates, source_language=language,
            target_languages=targets, glossary=glossary, seed=set(), discovered=discovered, vocab=vocab, idf=idf, pair_index=pair_index)
        with dbstore.connect(store) as conn:
            revision = dbstore.current_revision(conn)
        committed = trinity.apply_scrub_result(store, raw, trinity._Corpus(groups, list(groups)),
            expected_revision=revision, source_language=language)
        results[language] = dict(candidates=candidates, raw_scalar_result=raw, normal_commit=committed, store=str(store))
    queries = json.loads(args.queries.read_bytes())
    for source in queries:
        language = source["source_language"]
        core = SekaiSyncCore(Path(results[language]["store"]))
        query = source["source_canonical"]
        with core.request_view():
            penetration = core.term_penetrate(query, story_key=source["story_key"], languages=list(languages))
            generic = core.query(query, include_web=False, limit=200)
            lookup = core.term_lookup(query, source_language=language, languages=list(languages), limit=200)
        outputs[source["unit_id"]] = dict(penetration=penetration, query=generic, lookup=lookup)
    report = dict(released_commit="05d2749", callable="sekaisync.trinity.build_candidate_pool + scrub_trinity",
                  code_root=str(code), pages=len(pages), source_languages=5, semantic_reference_labels_read=False,
                  selected_source_queries_read_after_scrub=True, query_seeds_in_discovery=False,
                  typed_occurrence_supported=False, consumer_outputs=outputs, scalar_results=results,
                  representation_limit="Scalar consumer correctness is scored fairly against the same fixed contexts, independently of absent typed kind/vector receipts.")
    with args.out.open("x", encoding="utf-8") as output:
        output.write(json.dumps(report, ensure_ascii=False, indent=2) + "\n")


if __name__ == "__main__":
    main()
