"""Backfill exactly four sealed op_02area locale assets via the public MS crawler."""
from __future__ import annotations

import argparse
from collections import Counter
from contextlib import closing
import json
from pathlib import Path
import re
import sqlite3
import sys
import urllib.error
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts import backfill_scraper_unit_openings as opening
from scripts.census_scraper_area_talk import REGIONS, _audit_page
from scripts.census_scraper_corpus import _write_json
from sekaisync import crawler
from sekaisync.config import load_config
from sekaisync.layout import web_consent_path
from sekaisync.postprocess import DEFAULT_PLACEHOLDER
from sekaisync.runtime import build_runtime
from sekaisync.webindex import load_existing_page_map

LANGUAGES = ("en", "zh_hans", "zh_hant", "ko")
SCENARIO = "op_02area"
MASTER_TABLES = (
    "eventStories", "unitProfiles", "unitStories", "cards", "cardEpisodes",
    "virtualLives", "virtualLiveSetlists", "virtualLiveCheerMessages",
    "virtualLivePamphlets", "paidVirtualLives", "actionSets",
    "characterArchiveVoices", "systemLive2ds",
)
ScopeViolation = opening.ScopeViolation
sha, now, bare_url = opening.sha, opening.now, opening.bare_url


def tutorial_language_matches(language: str, text: str) -> bool:
    if not text or not crawler.text_matches_language(language, text):
        return False
    latin = len(re.findall(r"[A-Za-z]", text))
    han = len(re.findall(r"[\u3400-\u4dbf\u4e00-\u9fff]", text))
    kana = len(re.findall(r"[\u3040-\u309f\u30a1-\u30fa\u30fd-\u30ff]", text))
    hangul = len(re.findall(r"[\uac00-\ud7af]", text))
    if language == "en":
        return latin > 0 and han + kana + hangul <= latin * 0.3
    if language == "ko":
        return hangul > 0 and kana == 0
    return language in {"zh_hans", "zh_hant"} and han > 0 and hangul == 0


def sealed_bytes(evidence: dict) -> bytes:
    raw = Path(evidence["path"]).read_bytes()
    if sha(raw) != evidence["sha256"]:
        raise ValueError("Sealed acquisition inventory changed: " + evidence["path"])
    return raw


def prepare_requests(source_path: Path) -> dict:
    raw = source_path.read_bytes()
    sources = json.loads(raw)
    requests = []
    for language in LANGUAGES:
        source = sources["raw_sources"][language]
        if source["region"] != REGIONS[language]:
            raise ValueError("Sealed source language/region mismatch")
        table = json.loads(sealed_bytes(source["actions"]))
        matches = [row for row in table if row.get("scenarioId") == SCENARIO]
        if len(matches) != 1 or type(matches[0].get("id")) is not int or matches[0]["id"] < 1:
            raise ValueError("Missing or ambiguous regional tutorial actionSets identity")
        record = matches[0]
        if sum(row.get("id") == record["id"] for row in table) != 1:
            raise ValueError("Ambiguous region-local tutorial actionSetId")
        if record.get("scriptId") != "tutorial" or type(record.get("areaId")) is not int:
            raise ValueError("Expected regional tutorial definition is not the sealed actionSet")
        requests.append(dict(language=language, region=REGIONS[language],
            logical_key="area_talk:" + SCENARIO, action_set_record=record, raw_file=source["actions"],
            ms_scenario_path=f"scenario/actionset/group{record['id'] // 100}/{SCENARIO}.json"))
    census = sources["frozen_census"]
    sealed_bytes(census)
    with closing(sqlite3.connect(Path(census["path"]).resolve().as_uri() + "?mode=ro", uri=True)) as conn:
        references = [dict(source=source, page_id=page_id, version_hash=version,
                           text_sha256=json.loads(metadata).get("text_hash"))
            for source, page_id, version, metadata in conn.execute(
                "SELECT source,id,version_hash,metadata_json FROM pages "
                "WHERE language='ja' AND logical_key=? AND primary_good=1", ("area_talk:" + SCENARIO,))]
    if not references or any(not row["text_sha256"] for row in references):
        raise ValueError("Frozen census has no hash-backed primary Japanese tutorial reference")
    return dict(schema="sekaisync/p0-exact-area-tutorial-requests@1", scenario_id=SCENARIO,
        prepared_at_utc=now(), inventory_source=dict(path=str(source_path.resolve()), sha256=sha(raw)),
        frozen_census=census, japanese_primary_references=references, requests=requests)


def load_requests(path: Path) -> tuple[dict, dict]:
    raw = path.read_bytes()
    manifest = json.loads(raw)
    expected = prepare_requests(Path(manifest["inventory_source"]["path"]))
    if manifest.get("schema") != expected["schema"] or manifest.get("scenario_id") != SCENARIO:
        raise ValueError("Unexpected exact tutorial request manifest")
    for field in ("inventory_source", "frozen_census", "japanese_primary_references", "requests"):
        if manifest.get(field) != expected[field]:
            raise ValueError("Request identity differs from frozen acquisition inventory: " + field)
    if len(manifest["requests"]) != 4 or Counter(row["language"] for row in manifest["requests"]) != Counter(LANGUAGES):
        raise ValueError("Exactly four distinct overseas tutorial requests are required")
    return manifest, {"request_sha256": sha(raw)}


class ExactAreaFetcher(opening.ExactFetcher):
    def __init__(self, runtime, source: str, manifest: dict, out: Path):
        self.out, self.source = out, source
        self.log_path = out / "fetch-records.jsonl"
        self.raw_dir = out / "raw-responses"
        self.raw_dir.mkdir(parents=True, exist_ok=True)
        self.log_path.touch(exist_ok=False)
        self.robots, self.allowed, self.metadata, self.locales = {}, {}, {}, {}
        self.attempted, self.validations = set(), {}
        self.japanese_hashes = {row["text_sha256"] for row in manifest["japanese_primary_references"]}
        site = runtime.site(source)
        if site is None or not site.enabled or site.backend != "moesekai":
            raise ValueError("Tutorial backfill requires a configured enabled MS source")
        endpoints = runtime.endpoints_for(source)
        if not endpoints.ALTSOURCE_MS_ASSET_BASES:
            raise ValueError("Configured MS asset base missing")
        self.asset_base = endpoints.ALTSOURCE_MS_ASSET_BASES[0]
        for request in manifest["requests"]:
            language = request["language"]
            locales = [locale for locale, lang in endpoints.ALTSOURCE_MS_LOCALE_LANGUAGES.items() if lang == language]
            if len(locales) != 1:
                raise ValueError("Missing or ambiguous configured MS locale")
            locale = self.locales[language] = locales[0]
            server = endpoints.ALTSOURCE_MS_LOCALE_SERVERS[locale]
            url = self.asset_base.rstrip("/") + "/sekai-" + server + "-assets/" + request["ms_scenario_path"]
            if bare_url(url) in self.allowed:
                raise ValueError("Configured locale asset URLs are not distinct")
            self.allowed[bare_url(url)] = request
            for base in endpoints.ALTSOURCE_MS_METADATA_BASES:
                for name in MASTER_TABLES:
                    value = [request["action_set_record"]] if name == "actionSets" else []
                    self.metadata[bare_url(base.rstrip("/") + "/" + server + "/master/" + name + ".json")] = json.dumps(value)

    def read_response(self, url: str, role: str) -> tuple[bytes, dict]:
        started = now()
        try:
            with opening.transport.open_validated(url, timeout=30, max_redirects=0,
                    headers={"User-Agent": crawler.USER_AGENT, "Accept": "application/json,text/plain;q=0.9,*/*;q=0.8",
                             "Cache-Control": "no-cache"}) as response:
                raw = opening.transport.read_bounded(response, opening.transport.BUDGET_JSON_BYTES, what=role)
                headers = {key.lower(): value for key, value in response.headers.items()}
                record = dict(url=url, final_url=response.geturl(), role=role, started_at_utc=started,
                    finished_at_utc=now(), http_status=response.getcode(), response_bytes=len(raw),
                    raw_response_sha256=sha(raw), content_type=headers.get("content-type"),
                    x_robots_tag=headers.get("x-robots-tag", ""), redirect_budget=0)
                local = self.raw_dir / (sha(raw) + (".txt" if role == "robots" else ".asset"))
                if not local.exists():
                    local.write_bytes(raw)
                record["raw_response_file"] = str(local.resolve())
                self.record(record)
                return raw, record
        except Exception as exc:
            self.record(dict(url=url, role=role, started_at_utc=started, finished_at_utc=now(),
                http_status=exc.code if isinstance(exc, urllib.error.HTTPError) else None,
                error=type(exc).__name__, message=str(exc), redirect_budget=0,
                retry_after=exc.headers.get("Retry-After", "") if isinstance(exc, urllib.error.HTTPError) and exc.headers else ""))
            raise

    def __call__(self, url: str) -> str:
        key = bare_url(url)
        if key in self.metadata:
            value = self.metadata[key]
            self.record(dict(url=url, role="local_filtered_masterdata", origin="sealed_local_inventory_filter",
                finished_at_utc=now(), filtered_payload_sha256=sha(value.encode())))
            return value
        if key not in self.allowed:
            raise ScopeViolation("Public crawler attempted a URL outside the four exact tutorial assets: " + url)
        request = self.allowed[key]
        language = request["language"]
        if language in self.attempted:
            raise ScopeViolation("Repeated tutorial body request prohibited: " + language)
        try:
            self.check_robots(url)
        except ScopeViolation:
            raise
        except Exception as exc:
            raise ScopeViolation("Exact tutorial robots check failed without retry: " + type(exc).__name__) from exc
        self.attempted.add(language)
        try:
            raw, record = self.read_response(url, "scenario")
        except Exception as exc:
            # Stop the crawler's transport retries and mirror fallback at this exact boundary.
            raise ScopeViolation("Exact tutorial request failed without retry: " + type(exc).__name__) from exc
        if bare_url(record["final_url"]) != key:
            raise ScopeViolation("Tutorial response redirected away from its exact regional asset")
        directives = record["x_robots_tag"].lower().replace(",", " ").split()
        if any(value in directives for value in ("noai", "notrain", "noindex", "none")):
            raise ScopeViolation("Scenario response prohibits this content use")
        data = json.loads(raw.decode("utf-8-sig"))
        if not isinstance(data, dict):
            raise ScopeViolation("Tutorial scenario is not a JSON object")
        identifiers = {field: data[field] for field in ("ScenarioId", "m_Name") if data.get(field) not in (None, "")}
        identity_ok = bool(identifiers) and all(value == SCENARIO for value in identifiers.values())
        text = crawler.scenario_json_to_text(data)
        text_hash = sha(text.encode())
        language_ok = tutorial_language_matches(language, text)
        placeholder = (text.strip() == DEFAULT_PLACEHOLDER or text_hash in self.japanese_hashes
                       or bool(data.get("untranslated")) or bool(data.get("untranslated_placeholder")))
        reasons = []
        if not identity_ok:
            reasons.append("response_scenario_identity_missing_or_mismatched")
        if not text:
            reasons.append("empty_response_text")
        elif not language_ok:
            reasons.append("response_language_mismatch")
        if placeholder:
            reasons.append("untranslated_placeholder_or_japanese_copy")
        validation = dict(url=url, role="scenario_content_validation", finished_at_utc=now(), language=language,
            logical_key=request["logical_key"], local_action_set_id=request["action_set_record"]["id"],
            raw_response_sha256=sha(raw), extracted_text_sha256=text_hash, extracted_text_characters=len(text),
            scenario_identity_evidence=identifiers, scenario_identity_verified=identity_ok,
            content_language_matches=language_ok, untranslated_or_placeholder=placeholder,
            status="usable_response" if not reasons else "rejected_response", reasons=reasons,
            top_level_keys=sorted(data))
        self.validations[language] = validation
        self.record(validation)
        if reasons:
            raise ScopeViolation("Exact tutorial response rejected: " + ", ".join(reasons))
        return raw.decode("utf-8-sig")


def run(request_path: Path, out: Path, production: Path, config_path: Path | None = None,
        source: str = "altsource_ms", delay: float = 0.5) -> dict:
    out = out.resolve()
    store = out / "store"
    opening.isolation(store, production, out)
    if out.exists() and any(out.iterdir()):
        raise ValueError("Backfill output is frozen or nonempty; use a new explicit output directory")
    manifest, inventory = load_requests(request_path)
    consent_path = web_consent_path(production)
    consent_raw = consent_path.read_bytes()
    consent = json.loads(consent_raw).get(source, {})
    if consent.get("accepted") is not True or consent.get("tos_version") != 1:
        raise ValueError("Existing explicit source TOS consent is missing; no network request made")
    runtime = build_runtime(load_config(config_path, ROOT, store_override=store))
    out.mkdir(parents=True, exist_ok=True)
    fetcher = ExactAreaFetcher(runtime, source, manifest, out)
    receipt = dict(schema="sekaisync/p0-isolated-area-tutorial-backfill@1", started_at_utc=now(),
        request_file=str(request_path.resolve()), request_sha256=inventory["request_sha256"], source=source,
        store=str(store), production_store=str(production.resolve()), production_store_mutated=False,
        configured_runtime=dict(config_path=str(config_path.resolve()) if config_path else None,
            config_sha256=sha(config_path.read_bytes()) if config_path else None, asset_base=fetcher.asset_base),
        consent_evidence=dict(path=str(consent_path.resolve()), sha256=sha(consent_raw), source_record=consent),
        requested_locale_assets=4, body_request_budget=4, body_request_limit_per_language=1,
        robots_and_content_checks_preserved=True, crawl_results=[], errors=[])
    for request in manifest["requests"]:
        language = request["language"]
        try:
            result = crawler.crawl_altsource_ms(store, locales=(fetcher.locales[language],), depth=3, limit=1,
                accept_tos=False, tos_already_checked=True, delay=delay, workers=1, resume=True,
                include_overlay=False, fetcher=fetcher, instance=source, runtime=runtime)
            receipt["crawl_results"].append(dict(language=language, region=request["region"], **result))
        except Exception as exc:
            receipt["errors"].append(dict(language=language, type=type(exc).__name__, message=str(exc)))
    pages = load_existing_page_map(store, source) if (store / "kb/sekaisync.db").exists() else {}
    definitions = {SCENARIO: {row["language"]: [row["action_set_record"]] for row in manifest["requests"]}}
    details = []
    for request in manifest["requests"]:
        language, locale = request["language"], fetcher.locales[request["language"]]
        page_id = f"web:{source}:{locale}:area_talk:{SCENARIO}"
        page = pages.get(page_id)
        validation = fetcher.validations.get(language)
        detail = dict(language=language, logical_key=request["logical_key"],
            local_action_set_id=request["action_set_record"]["id"], response_validation=validation)
        if page is None:
            detail.update(status="missing_usable_page", page_id=page_id)
        else:
            view = dict(page, logical_key=request["logical_key"], metadata=page)
            audit = _audit_page(view, definitions)
            text = str(page.get("text") or "")
            url_ok = urlsplit(str(page.get("url") or "")).path.rstrip("/") == f"/{locale}/story/area/{request['action_set_record']['areaId']}/{SCENARIO}"
            flags = {field: page.get(field) for field in ("asset_mismatch", "scenario_id_mismatch",
                "content_language_mismatch", "untranslated", "untranslated_placeholder")}
            valid = (audit["identity_status"] == "expected" and url_ok and not any(flags.values())
                     and validation is not None and validation["status"] == "usable_response"
                     and sha(text.encode()) == validation["extracted_text_sha256"])
            detail.update(status="accepted_identity_verified_text" if valid else "rejected_persisted_page",
                page_id=page_id, url=page.get("url"), text_sha256=sha(text.encode()), characters=len(text),
                identity_audit=audit, canonical_area_url_verified=url_ok, flags=flags)
        details.append(detail)
    expected_ids = {f"web:{source}:{fetcher.locales[language]}:area_talk:{SCENARIO}" for language in LANGUAGES}
    if set(pages) - expected_ids:
        raise ScopeViolation("Isolated crawler persisted unexpected pages")
    load_requests(request_path)
    if sha(request_path.read_bytes()) != inventory["request_sha256"] or consent_path.read_bytes() != consent_raw:
        raise ScopeViolation("Frozen requests or production consent changed during backfill")
    records = [json.loads(line) for line in fetcher.log_path.read_text(encoding="utf-8").splitlines()]
    actual_requests = sum(row["role"] == "scenario" and "started_at_utc" in row for row in records)
    if actual_requests > 4 or len(fetcher.attempted) > 4:
        raise ScopeViolation("Exact four-body request budget exceeded")
    receipt.update(finished_at_utc=now(), attempted_locale_assets=len(fetcher.attempted),
        actual_body_requests=actual_requests, persisted_pages=len(pages), details=details,
        accepted_locale_assets=sum(row["status"] == "accepted_identity_verified_text" for row in details),
        status_counts=dict(Counter(row["status"] for row in details)), fetch_records=str(fetcher.log_path.resolve()),
        fetch_records_sha256=sha(fetcher.log_path.read_bytes()), script_sha256=sha(Path(__file__).read_bytes()))
    receipt["status"] = "complete" if receipt["accepted_locale_assets"] == 4 and not receipt["errors"] else "partial"
    _write_json(out / "report.json", receipt)
    return receipt


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    base = ROOT / "work/p0-exhaustive-20261001"
    parser.add_argument("--inventory-sources", type=Path, default=base / "area-talk-inventory-b-01/area-talk-inventory-sources.json")
    parser.add_argument("--requests", type=Path, default=base / "census/expected-area-tutorial-backfill-requests.json")
    parser.add_argument("--out", type=Path, default=base / "census/area-tutorial-backfill-ms-01")
    parser.add_argument("--production-store", type=Path, default=ROOT / "store")
    parser.add_argument("--config", type=Path, default=ROOT / "settings.local.json")
    parser.add_argument("--source", default="altsource_ms")
    parser.add_argument("--delay", type=float, default=0.5)
    args = parser.parse_args()
    if not args.requests.exists():
        _write_json(args.requests, prepare_requests(args.inventory_sources))
    result = run(args.requests, args.out, args.production_store, args.config, args.source, args.delay)
    print(json.dumps({key: value for key, value in result.items() if key != "details"}, ensure_ascii=False, indent=2))
    if result["status"] != "complete":
        raise SystemExit(2)


if __name__ == "__main__":
    main()
