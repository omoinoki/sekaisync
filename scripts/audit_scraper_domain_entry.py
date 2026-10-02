"""Read-only raw-identity and narrative-domain acquisition diagnostics.

No production writes, network fetches, gold/reference reads, or release claims.
The optional report is created exclusively, never overwriting an earlier audit.
"""
from __future__ import annotations

import argparse
from collections import defaultdict
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import sqlite3
import sys


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from sekaisync import termindex


REGIONS = {"jp": "ja", "en": "en", "cn": "zh_hans", "tc": "zh_tw", "kr": "ko"}
DOMAINS = ("self_intro", "mysekai_tweet", "mysekai_talk", "special_story", "virtual_live")
EDGE_TABLES = (
    "mysekaiCharacterTalkFixtureCommonTweetGroups", "mysekaiCharacterTalkPreActions",
    "mysekaiCharacterTalkTweetWithoutRelatedTalks", "mysekaiTutorialTalks",
)


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def indexed(rows, field):
    result = {}
    for row in rows:
        key = str(row[field])
        if key in result:
            raise ValueError(f"duplicate raw {field}: {key}")
        result[key] = row
    return result


def usable(page, language):
    return not page.get("aux_flag") and page.get("trust") != "D" and termindex._page_usable(page, language)


def tweet_bindings(talks, edge_rows):
    bindings, identities, full_edges = defaultdict(set), {}, defaultdict(set)
    for table, rows in edge_rows.items():
        for row in rows:
            if row.get("mysekaiCharacterTalkTweetId") is None:
                continue
            tweet = str(row["mysekaiCharacterTalkTweetId"])
            if table == "mysekaiCharacterTalkPreActions":
                talk_id = str(row["mysekaiCharacterTalkId"])
                talk = talks.get(talk_id)
                binding = ("talk", talk_id, talk.get("assetbundleName") if talk else None,
                           talk.get("lua") if talk else None)
            elif table == "mysekaiTutorialTalks":
                binding = ("tutorial", row.get("assetbundleName"), row.get("lua"))
            elif table == "mysekaiCharacterTalkTweetWithoutRelatedTalks":
                binding = ("character_unit", row.get("gameCharacterUnitId"))
            else:
                binding = ("fixture_tweet_group", row.get("groupId"))
            # A dangling talk reference is not evidence of a shared asset family.
            if all(value is not None and value != "" for value in binding[1:]):
                bindings[tweet].add(binding)
            edge_key = (table, str(row["id"]))
            if edge_key in identities:
                raise ValueError(f"duplicate inbound edge identity: {edge_key}")
            identities[edge_key] = (tweet, binding)
            full_edges[tweet].add(canonical(dict(table=table, record=row)))
    return bindings, identities, full_edges


def audit(store):
    store = Path(store).resolve()
    database = store / "kb" / "sekaisync.db"
    conn = sqlite3.connect(database.as_uri() + "?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        raw_generation = conn.execute("SELECT value FROM meta WHERE key='active_raw_generation'").fetchone()
        if raw_generation is None:
            raise ValueError("no active raw generation")
        generations = json.loads(raw_generation[0])
        pages = [dict(row) for row in conn.execute(
            "SELECT * FROM web_pages WHERE kind IN (" + ",".join("?" for _ in DOMAINS) + ")", DOMAINS)]
    finally:
        conn.close()

    report = dict(
        schema="sekaisync-domain-entry-audit-v1", created_at=datetime.now(timezone.utc).isoformat(),
        store=str(store), generation_by_region=generations,
        policy=dict(release_proof=False, semantic_translation_gold=False,
                    independently_retrieved_table_seals=False, production_writes=False,
                    expected_identities="definitions in the current local immutable snapshot only",
                    usable="primary non-D pages passing the actual text-language and mismatch checks"),
        raw_inputs=[], page_domains={}, expected={}, family_identity={},
    )
    page_by_identity = defaultdict(list)
    for page in pages:
        language = termindex._term_language(page["language"])
        page_by_identity[(language, termindex.page_story_key(page))].append(page)
    usable_sets = {}
    for domain in DOMAINS:
        raw_sets, accepted = {}, {}
        for region, language in REGIONS.items():
            keys = {key for (lang, key), values in page_by_identity.items()
                    if lang == language and any(page["kind"] == domain for page in values)}
            raw_sets[region] = keys
            accepted[region] = {key for key in keys if any(usable(page, language)
                                                          for page in page_by_identity[(language, key)])}
        usable_sets[domain] = accepted
        report["page_domains"][domain] = dict(
            allowlisted=domain in termindex.TERM_STORY_KINDS,
            distinct_all={region: len(keys) for region, keys in raw_sets.items()},
            distinct_usable_primary={region: len(keys) for region, keys in accepted.items()},
            five_language_usable_intersection=len(set.intersection(*accepted.values())),
        )

    all_tweets, all_talks, all_profiles, all_bindings, all_edges, all_full_edges = {}, {}, {}, {}, {}, {}
    expected = defaultdict(dict)
    def read_table(region, base, table):
        path = base / (table + ".json")
        payload = path.read_bytes()
        rows = json.loads(payload)
        if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
            raise ValueError(f"malformed raw table: {path}")
        report["raw_inputs"].append(dict(region=region, table=table, path=str(path),
                                         sha256=hashlib.sha256(payload).hexdigest(), records=len(rows)))
        return rows

    for region, language in REGIONS.items():
        generation = generations[region]
        if not isinstance(generation, str) or not re.fullmatch(r"[A-Za-z0-9_-]+", generation):
            raise ValueError("unsafe raw generation identity")
        directories = list((store / "raw" / "generations" / generation / region / "source").iterdir())
        if len(directories) != 1 or not directories[0].is_dir():
            raise ValueError(f"ambiguous raw source directory: {region}")
        base = directories[0]
        profiles = indexed(read_table(region, base, "characterProfiles"), "scenarioId")
        tweets = indexed(read_table(region, base, "mysekaiCharacterTalkTweets"), "id")
        talks = indexed(read_table(region, base, "mysekaiCharacterTalks"), "id")
        edges = {table: read_table(region, base, table) for table in EDGE_TABLES}
        bindings, identities, full_edges = tweet_bindings(talks, edges)
        all_profiles[region], all_tweets[region], all_talks[region] = profiles, tweets, talks
        all_bindings[region], all_edges[region], all_full_edges[region] = bindings, identities, full_edges
        expected["self_intro"][region] = {"self_intro:" + key for key in profiles}
        expected["mysekai_talk"][region] = {"mysekai_talk:" + key for key, talk in talks.items()
                                             if talk.get("assetbundleName") and talk.get("lua")}
        texts = {"mysekai_tweet:" + key: str(tweet.get("text") or "").strip()
                 for key, tweet in tweets.items() if str(tweet.get("text") or "").strip()}
        expected["mysekai_tweet"][region] = set(texts)
        report["expected"][region] = dict(
            self_intro_definitions=len(profiles), mysekai_talk_asset_definitions=len(expected["mysekai_talk"][region]),
            mysekai_tweet_nonempty_definitions=len(texts),
            tweet_missing_pages=sorted(key for key in texts if not page_by_identity[(language, key)]),
            tweet_changed_text=sorted(key for key, text in texts.items()
                                      if page_by_identity[(language, key)] and
                                      not any(page["text"] == text for page in page_by_identity[(language, key)])),
            tweet_raw_language_rejected=sorted(key for key, text in texts.items()
                                               if not termindex._page_usable(dict(text=text), language)),
        )

    common_tweets = set.intersection(*(set(tweets) for tweets in all_tweets.values()))
    supported = {key for key in common_tweets
                 if set.intersection(*(all_bindings[region][key] for region in REGIONS))}
    current_common = {key.partition(":")[2] for key in
                      set.intersection(*usable_sets["mysekai_tweet"].values())}
    edge_common = set.intersection(*(set(edges) for edges in all_edges.values()))
    common_talks = set.intersection(*(set(talks) for talks in all_talks.values()))
    common_profiles = set.intersection(*(set(profiles) for profiles in all_profiles.values()))
    report["family_identity"] = dict(
        common_raw_tweets=len(common_tweets), common_raw_talks=len(common_talks),
        tweets_with_at_least_one_common_topological_binding=len(supported),
        unsupported_common_tweet_ids=sorted(common_tweets - supported, key=int),
        existing_usable_five_language_tweets=len(current_common),
        existing_without_common_binding=sorted(current_common - supported, key=int),
        common_edge_binding_conflicts=[list(key) for key in sorted(edge_common)
                                       if len({all_edges[region][key] for region in REGIONS}) > 1],
        talk_asset_identity_conflicts=sorted(key for key in common_talks
            if len({(all_talks[region][key].get("assetbundleName"), all_talks[region][key].get("lua"))
                    for region in REGIONS}) > 1),
        profile_character_binding_conflicts=sorted(key for key in common_profiles
            if len({all_profiles[region][key]["characterId"] for region in REGIONS}) > 1),
        tweet_nontext_metadata_difference_ids=sorted(key for key in common_tweets
            if len({canonical({name: value for name, value in all_tweets[region][key].items() if name != "text"})
                    for region in REGIONS}) > 1),
        tweet_full_inbound_metadata_difference_ids=sorted(key for key in common_tweets
            if len({tuple(sorted(all_full_edges[region][key])) for region in REGIONS}) > 1),
        evidence_limit="Shared structural family binding is not semantic gold or release proof.",
    )
    report["common_expected"] = {}
    for domain, localized in expected.items():
        common = set.intersection(*localized.values())
        report["common_expected"][domain] = dict(
            identities=len(common),
            missing_usable={region: sorted(common - usable_sets[domain][region]) for region in REGIONS})
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--store", type=Path, default=ROOT / "store")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    report = audit(args.store)
    payload = json.dumps(report, ensure_ascii=False, indent=2) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        with args.output.open("x", encoding="utf-8", newline="\n") as stream:
            stream.write(payload)
    print(json.dumps(dict(page_domains=report["page_domains"], family_identity=report["family_identity"],
                          expected_refresh={region: {name: len(value) if isinstance(value, list) else value
                                                    for name, value in diagnostic.items()}
                                            for region, diagnostic in report["expected"].items()},
                          report_path=str(args.output.resolve()) if args.output else None,
                          report_sha256=hashlib.sha256(payload.encode("utf-8")).hexdigest()),
                     ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
