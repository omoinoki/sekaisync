"""Recover exactly the sealed unit-opening requests into an isolated store."""
from __future__ import annotations

import argparse
from collections import Counter
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sys
import time
import urllib.error
from urllib.parse import urlsplit, urlunsplit
from urllib.robotparser import RobotFileParser

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from sekaisync import crawler, fetcher as transport
from sekaisync.config import SekaiSyncConfig
from sekaisync.layout import web_consent_path
from sekaisync.runtime import build_runtime
from sekaisync.webindex import load_existing_page_map
from scripts.census_scraper_corpus import LANGUAGES, REGIONS, _unit_page_identity, RawTables, _write_json


class ScopeViolation(RuntimeError):
    pass


def sha(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def bare_url(url: str) -> str:
    parts = urlsplit(url)
    return urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))


def load_requests(path: Path) -> tuple[dict, dict]:
    raw = path.read_bytes()
    manifest = json.loads(raw)
    requests = manifest["requests"]
    keys = [(row["language"], row["logical_key"]) for row in requests]
    if (manifest.get("schema") != "sekaisync/p0-expected-unit-backfill@1" or len(requests) != 25
            or len(set(keys)) != 25 or Counter(row["language"] for row in requests) != Counter({language: 5 for language in LANGUAGES})):
        raise ValueError("Backfill requires exactly the frozen 25 opening locale units")
    definitions, sealed = {}, {}
    for request in requests:
        language, identity = request["language"], request["identity"]
        if request["region"] != REGIONS[language] or identity["episode_no"] != 1 or not request["is_opening_episode"]:
            raise ValueError("Request is not an exact regional opening episode")
        path = Path(request["raw_file"]["path"])
        raw_table = path.read_bytes()
        if sha(raw_table) != request["raw_file"]["sha256"]:
            raise ValueError("Sealed raw inventory changed")
        sealed[str(path)] = sha(raw_table)
        matches = [(story, chapter, episode) for story in json.loads(raw_table)
                   for chapter in story["chapters"] for episode in chapter["episodes"]
                   if episode.get("scenarioId") == identity["scenario_id"]]
        if len(matches) != 1:
            raise ValueError("Missing or ambiguous exact scenario in sealed master table")
        story, chapter, episode = matches[0]
        actual = {"unit": story["unit"], "story_seq": story["seq"], "chapter_id": chapter["id"],
                  "chapter_no": chapter["chapterNo"], "episode_id": episode["id"], "episode_no": episode["episodeNo"],
                  "episode_group_id": episode["unitStoryEpisodeGroupId"], "scenario_id": episode["scenarioId"],
                  "assetbundle_name": chapter.get("assetbundleName") or episode.get("assetbundleName")}
        if actual != identity or request["logical_key"] != "unit_story:" + identity["scenario_id"]:
            raise ValueError("Request raw nested identity mismatch")
        definitions[language, identity["scenario_id"]] = {"identity": identity, "record": episode,
            "story": story, "chapter": chapter, "path": str(path), "file": request["raw_file"]}
    return manifest, {"request_sha256": sha(raw), "sealed_files": sealed, "definitions": definitions}


def isolation(store: Path, production: Path, out: Path) -> None:
    store, production, out = store.resolve(), production.resolve(), out.resolve()
    if store == production or store.is_relative_to(production) or production.is_relative_to(store):
        raise ValueError("Backfill output must not overlap the production store")
    if not store.is_relative_to(out):
        raise ValueError("Isolated backfill store must stay inside its explicit output directory")


class ExactFetcher:
    def __init__(self, runtime, source: str, requests: list[dict], definitions: dict, out: Path):
        self.out, self.source = out, source
        self.log_path = out / "fetch-records.jsonl"
        self.robots, self.allowed, self.metadata, self.locales, self.auxiliary_files = {}, {}, {}, {}, {}
        self.raw_dir = out / "raw-responses"
        self.raw_dir.mkdir(parents=True, exist_ok=True)
        self.log_path.touch(exist_ok=True)
        endpoints = runtime.endpoints_for(source)
        site = runtime.site(source)
        if site is None or site.backend not in {"sekai_viewer", "moesekai"}:
            raise ValueError("Opening backfill requires a configured enabled narrative source instance")
        self.backend = site.backend
        if self.backend == "moesekai":
            for language in LANGUAGES:
                matches = [locale for locale, item_language in endpoints.ALTSOURCE_MS_LOCALE_LANGUAGES.items()
                           if item_language == language]
                if len(matches) != 1:
                    raise ValueError("Missing or ambiguous configured MS locale")
                self.locales[language] = matches[0]
        for request in requests:
            if self.backend == "sekai_viewer":
                bucket = endpoints.ALTSOURCE_SV_ASSET_BUCKETS.get(request["region"])
                if not bucket:
                    raise ValueError("Configured asset bucket missing")
                for path in request["sv_asset_paths"]:
                    url = endpoints.ALTSOURCE_SV_ASSET_BASE.rstrip("/") + "/" + bucket + "/" + path
                    self.allowed[bare_url(url)] = request
            else:
                server = endpoints.ALTSOURCE_MS_LOCALE_SERVERS[self.locales[request["language"]]]
                for base in endpoints.ALTSOURCE_MS_ASSET_BASES:
                    url = base.rstrip("/") + "/sekai-" + server + "-assets/" + request["ms_scenario_path"]
                    self.allowed[bare_url(url)] = request
                if endpoints.ALTSOURCE_MS_FALLBACK_TO_VIEWER_CDN:
                    bucket = "tc" if server == "tw" else server
                    for path in request["sv_asset_paths"]:
                        url = endpoints.ALTSOURCE_SV_ASSET_BASE.rstrip("/") + "/sekai-" + bucket + "-assets/" + path
                        self.allowed[bare_url(url)] = request
        with runtime.activate(source):
            for language in LANGUAGES:
                region = REGIONS[language]
                stories = {}
                profiles = []
                for (item_language, scenario), definition in definitions.items():
                    if item_language != language:
                        continue
                    unit = definition["identity"]["unit"]
                    copied = deepcopy(definition["story"])
                    copied["chapters"] = [deepcopy(definition["chapter"])]
                    copied["chapters"][0]["episodes"] = [deepcopy(definition["record"])]
                    if unit in stories:
                        raise ValueError("More than one opening request per unit is unsupported")
                    stories[unit] = copied
                    if self.backend == "moesekai":
                        profile_path = Path(definition["path"]).with_name("unitProfiles.json")
                        profile_raw = profile_path.read_bytes()
                        self.auxiliary_files[str(profile_path)] = sha(profile_raw)
                        matching = [item for item in json.loads(profile_raw) if item.get("unit") == unit]
                        if len(matching) != 1 or matching[0].get("seq") != definition["identity"]["story_seq"]:
                            raise ValueError("MS local unit profile does not match sealed nested story sequence")
                        profiles.append(matching[0])
                if self.backend == "sekai_viewer":
                    self.metadata[bare_url(crawler.altsource_sv_master_json_url(region, "eventStories"))] = "[]"
                    self.metadata[bare_url(crawler.altsource_sv_master_json_url(region, "unitStories"))] = json.dumps(list(stories.values()), ensure_ascii=False)
                else:
                    server = endpoints.ALTSOURCE_MS_LOCALE_SERVERS[self.locales[language]]
                    values = {"eventStories": [], "unitStories": list(stories.values()), "unitProfiles": profiles}
                    for base in endpoints.ALTSOURCE_MS_METADATA_BASES:
                        for name, value in values.items():
                            self.metadata[bare_url(base.rstrip("/") + "/" + server + "/master/" + name + ".json")] = json.dumps(value, ensure_ascii=False)

    def record(self, value: dict) -> None:
        with self.log_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(value, ensure_ascii=False) + "\n")

    def read_response(self, url: str, role: str) -> tuple[bytes, dict]:
        started = now()
        try:
            with transport.open_validated(url, timeout=30, headers={"User-Agent": crawler.USER_AGENT,
                    "Accept": "application/json,text/plain;q=0.9,*/*;q=0.8", "Cache-Control": "no-cache"}) as response:
                raw = transport.read_bounded(response, transport.BUDGET_JSON_BYTES, what=role)
                status = response.getcode()
                headers = {key.lower(): value for key, value in response.headers.items()}
                record = {"url": url, "final_url": response.geturl(), "role": role, "started_at_utc": started,
                          "finished_at_utc": now(), "http_status": status, "response_bytes": len(raw),
                          "raw_response_sha256": sha(raw), "content_type": headers.get("content-type"),
                          "x_robots_tag": headers.get("x-robots-tag", "")}
                suffix = ".txt" if role == "robots" else ".asset"
                local = self.raw_dir / (sha(raw) + suffix)
                if not local.exists():
                    local.write_bytes(raw)
                record["raw_response_file"] = str(local.resolve())
                self.record(record)
                return raw, record
        except urllib.error.HTTPError as exc:
            self.record({"url": url, "role": role, "started_at_utc": started, "finished_at_utc": now(),
                         "http_status": exc.code, "error": type(exc).__name__,
                         "retry_after": exc.headers.get("Retry-After", "") if exc.headers else ""})
            raise
        except Exception as exc:
            self.record({"url": url, "role": role, "started_at_utc": started, "finished_at_utc": now(),
                         "http_status": None, "error": type(exc).__name__, "message": str(exc)})
            raise

    def check_robots(self, url: str) -> None:
        parts = urlsplit(url)
        origin = urlunsplit((parts.scheme, parts.netloc, "", "", ""))
        if origin not in self.robots:
            robots_url = origin + "/robots.txt"
            try:
                raw, record = self.read_response(robots_url, "robots")
                parser = RobotFileParser(robots_url)
                parser.parse(raw.decode("utf-8-sig").splitlines())
                self.robots[origin] = parser
            except urllib.error.HTTPError as exc:
                if exc.code in {404, 410}:
                    self.robots[origin] = None
                else:
                    raise ScopeViolation("Robots policy unavailable or denied: " + robots_url) from exc
        parser = self.robots[origin]
        if parser is not None and not parser.can_fetch(crawler.USER_AGENT, url):
            self.record({"url": url, "role": "scenario", "policy": "robots_disallow", "finished_at_utc": now()})
            raise ScopeViolation("robots.txt disallows the exact opening scenario")
        if parser is not None:
            delay = parser.crawl_delay(crawler.USER_AGENT)
            if delay:
                time.sleep(delay)

    def __call__(self, url: str) -> str:
        key = bare_url(url)
        if key in self.metadata:
            text = self.metadata[key]
            self.record({"url": url, "role": "local_filtered_masterdata", "origin": "sealed_local_inventory_filter",
                         "finished_at_utc": now(), "http_status": None, "filtered_payload_sha256": sha(text.encode())})
            return text
        if key not in self.allowed:
            raise ScopeViolation("Public crawler attempted a URL outside the exact opening request set: " + url)
        self.check_robots(url)
        raw, record = self.read_response(url, "scenario")
        if bare_url(record["final_url"]) not in self.allowed:
            raise ScopeViolation("Scenario redirect escaped the exact request scope")
        directives = record["x_robots_tag"].lower().replace(",", " ").split()
        if any(value in directives for value in ("noai", "notrain", "noindex", "none")):
            raise ScopeViolation("Scenario response prohibits this content use")
        text = raw.decode("utf-8-sig")
        data = json.loads(text)
        request = self.allowed[key]
        if not isinstance(data, dict):
            raise ScopeViolation("Opening scenario is not a JSON object")
        mismatch = crawler._scenario_mismatch_reason(data, request["identity"]["scenario_id"])
        extracted = crawler.scenario_json_to_text(data)
        matched = crawler.text_matches_language(request["language"], extracted) if extracted else False
        self.record({"url": url, "role": "scenario_content_validation", "finished_at_utc": now(),
                     "language": request["language"], "logical_key": request["logical_key"],
                     "raw_response_sha256": sha(raw), "extracted_text_sha256": sha(extracted.encode()),
                     "extracted_text_characters": len(extracted), "scenario_id_mismatch": mismatch,
                     "language_matches": matched, "nonempty_text": bool(extracted),
                     "scenario_identity_evidence": {field: data.get(field) for field in ("ScenarioId", "scenarioId", "id")},
                     "top_level_keys": sorted(data)})
        if mismatch or extracted and not matched:
            raise ScopeViolation("Scenario identity or content language differs from the exact request")
        return text


def run(request_path: Path, out: Path, production: Path, source: str = "altsource_sv", delay: float = 0.5) -> dict:
    out = out.resolve()
    store = out / "store"
    isolation(store, production, out)
    if (out / "report.json").exists():
        raise ValueError("Backfill receipt is frozen; use a new explicit output directory for another trial")
    manifest, inventory = load_requests(request_path)
    consent_path = web_consent_path(production)
    consent_raw = consent_path.read_bytes()
    consent = json.loads(consent_raw).get(source, {})
    if consent.get("accepted") is not True or consent.get("tos_version") != 1:
        raise ValueError("Existing explicit source TOS consent is missing; no network request made")
    out.mkdir(parents=True, exist_ok=True)
    config = SekaiSyncConfig.from_dict({"store_root": str(store)}, ROOT)
    runtime = build_runtime(config)
    fetcher = ExactFetcher(runtime, source, manifest["requests"], inventory["definitions"], out)
    receipt = {"schema": "sekaisync/p0-isolated-unit-opening-backfill@1", "started_at_utc": now(),
               "request_file": str(request_path.resolve()), "request_sha256": inventory["request_sha256"],
               "source": source, "store": str(store), "production_store": str(production.resolve()),
               "production_store_mutated": False, "runtime_status": "explicit_enabled_instance_bound_to_isolated_store",
               "consent_evidence": {"path": str(consent_path.resolve()), "sha256": sha(consent_raw), "source_record": consent},
               "auxiliary_local_profiles": {"retrieval_sealed": False, "files": fetcher.auxiliary_files},
               "robots_and_content_checks_preserved": True, "requested_locale_units": 25, "crawl_results": [], "errors": []}
    try:
        for language, region in REGIONS.items():
            if fetcher.backend == "sekai_viewer":
                result = crawler.crawl_altsource_sv(store, regions=(region,), depth=1, limit=0,
                    accept_tos=False, tos_already_checked=True, delay=delay, workers=1, resume=True,
                    include_i18n=False, fetcher=fetcher, instance=source, runtime=runtime)
            else:
                result = crawler.crawl_altsource_ms(store, locales=(fetcher.locales[language],), depth=1, limit=0,
                    accept_tos=False, tos_already_checked=True, delay=delay, workers=1, resume=True,
                    include_overlay=False, fetcher=fetcher, instance=source, runtime=runtime)
            receipt["crawl_results"].append(dict(region=region, **result))
    except Exception as exc:
        receipt["errors"].append({"type": type(exc).__name__, "message": str(exc)})
    pages = load_existing_page_map(store, source) if (store / "kb/sekaisync.db").exists() else {}
    tables, details = RawTables(), []
    expected = {(request["language"], request["logical_key"]): request for request in manifest["requests"]}
    for request in manifest["requests"]:
        candidates = [page for page in pages.values() if page.get("language") == request["language"]
                      and page.get("kind") == "unit_story" and page.get("id", "").endswith(":" + request["identity"]["scenario_id"])]
        if len(candidates) != 1:
            details.append({"language": request["language"], "logical_key": request["logical_key"],
                            "status": "missing" if not candidates else "ambiguous", "candidate_pages": len(candidates)})
            continue
        page = candidates[0]
        definition = inventory["definitions"][request["language"], request["identity"]["scenario_id"]]
        audit = _unit_page_identity(page, definition, page.get("url", ""), tables)
        flags = {field: page.get(field) for field in ("asset_mismatch", "scenario_id_mismatch", "content_language_mismatch", "untranslated")}
        text = str(page.get("text") or "")
        matched = crawler.text_matches_language(request["language"], text) if text else False
        valid = audit["status"] == "verified" and matched and bool(text) and not any(flags.values())
        details.append({"language": request["language"], "logical_key": request["logical_key"],
                        "status": "accepted_identity_verified_text" if valid else "rejected_content_or_identity",
                        "page_id": page["id"], "url": page.get("url"), "text_sha256": sha(text.encode()),
                        "characters": len(text), "content_language_matches": matched, "identity_audit": audit, "flags": flags})
    extras = [page["id"] for page in pages.values() if (page.get("language"), "unit_story:" + page.get("id", "").split(":")[-1]) not in expected]
    if extras:
        raise ScopeViolation("Isolated crawler persisted unexpected pages: " + str(extras))
    if sha(request_path.read_bytes()) != inventory["request_sha256"] or any(sha(Path(path).read_bytes()) != value for path, value in inventory["sealed_files"].items()):
        raise ScopeViolation("Frozen request or raw inventory changed during backfill")
    if any(sha(Path(path).read_bytes()) != value for path, value in fetcher.auxiliary_files.items()):
        raise ScopeViolation("Local profile sequence evidence changed during backfill")
    receipt.update(finished_at_utc=now(), accepted_locale_units=sum(row["status"] == "accepted_identity_verified_text" for row in details),
                   status_counts=dict(Counter(row["status"] for row in details)), details=details, persisted_pages=len(pages),
                   fetch_records=str(fetcher.log_path.resolve()), fetch_records_sha256=sha(fetcher.log_path.read_bytes()),
                   script_sha256=sha(Path(__file__).read_bytes()))
    receipt["status"] = "complete" if receipt["accepted_locale_units"] == 25 and not receipt["errors"] else "partial"
    _write_json(out / "report.json", receipt)
    return receipt


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--requests", type=Path, default=ROOT / "work/p0-exhaustive-20261001/census/expected-unit-backfill-requests.json")
    parser.add_argument("--out", type=Path, default=ROOT / "work/p0-exhaustive-20261001/census/unit-opening-backfill-01")
    parser.add_argument("--production-store", type=Path, default=ROOT / "store")
    parser.add_argument("--source", default="altsource_sv")
    parser.add_argument("--delay", type=float, default=0.5)
    args = parser.parse_args()
    result = run(args.requests, args.out, args.production_store, args.source, args.delay)
    print(json.dumps({key: value for key, value in result.items() if key != "details"}, ensure_ascii=False, indent=2))
    if result["status"] != "complete":
        raise SystemExit(2)


if __name__ == "__main__":
    main()
