"""Offline, exact-debt home-voice recovery through the unchanged public MS crawler."""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from datetime import datetime, timezone
import json
from pathlib import Path
import sys
from urllib.parse import urlsplit, urlunsplit

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts import census_scraper_home_lines as census
from scripts.backfill_scraper_unit_openings import isolation
from sekaisync import crawler
from sekaisync.config import load_config
from sekaisync.layout import web_consent_path
from sekaisync.runtime import build_runtime
from sekaisync.webindex import load_existing_page_map

sha = census.sha
MASTER_TABLES = (
    "eventStories", "unitProfiles", "unitStories", "cards", "cardEpisodes", "virtualLives",
    "virtualLiveSetlists", "virtualLiveCheerMessages", "virtualLivePamphlets", "paidVirtualLives",
    "actionSets", "characterArchiveVoices", "systemLive2ds",
)


class OfflineScopeViolation(RuntimeError):
    pass


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def bare_url(url: str) -> str:
    parts = urlsplit(url)
    return urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))


def write_json(path: Path, value) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _strict_identity_equal(left: dict, right: dict) -> bool:
    # Python equality aliases integer/float and boolean/integer JSON identities.
    return (json.dumps(left, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
            == json.dumps(right, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False))


def prepare_requests(inventory: Path, per_language: int = 5) -> dict:
    if type(per_language) is not int or per_language < 0:
        raise ValueError("per_language must be zero for all debts or a positive exact regional batch")
    summary_path = inventory / "home-voice-inventory-summary.json"
    summary_raw = summary_path.read_bytes()
    summary = json.loads(summary_raw)
    if summary.get("schema") != "sekaisync/p0-composite-home-voice-census@1":
        raise ValueError("Expected the frozen composite home-voice inventory")
    tables, local_ids, sources = {}, {}, summary["raw_sources"]
    for language, region in census.REGIONS.items():
        evidence = sources[language]
        raw = Path(evidence["path"]).read_bytes()
        if sha(raw) != evidence["sha256"] or evidence["region"] != region:
            raise ValueError("Sealed regional voice table changed")
        rows = json.loads(raw)
        tables[language] = rows
        ids = defaultdict(list)
        for record in rows:
            if not census.identity_errors(record):
                ids[record["id"]].append(record)
        local_ids[language] = ids
    debt_evidence = summary["files"]["home-voice-acquisition-debts"]
    debts_raw = Path(debt_evidence["path"]).read_bytes()
    if sha(debts_raw) != debt_evidence["sha256"]:
        raise ValueError("Frozen home-voice acquisition debts changed")
    by_language, seen = defaultdict(list), set()
    for line in debts_raw.decode("utf-8").splitlines():
        debt = json.loads(line)
        language, identity = debt["language"], debt["local_voice_id"]
        if type(identity) is not int or identity < 1:
            raise ValueError("Regional acquisition debt local_voice_id must be a positive exact integer")
        key = language, identity
        if language not in census.REGIONS or debt["region"] != census.REGIONS[language] or key in seen:
            raise ValueError("Unexpected or duplicate regional acquisition debt identity")
        seen.add(key)
        matches = local_ids[language].get(identity, [])
        if len(matches) != 1 or not _strict_identity_equal(census.raw_identity(matches[0]), debt["composite_identity"]):
            raise ValueError("Debt does not resolve to its exact sealed regional composite record")
        record = matches[0]
        fields = [field["field"] for field in census.expected_fields(record) if field["nonempty_expected"]]
        if (debt["expected_nonempty_fields"] != fields or not fields
                or debt["acquisition_status"] in {"primary_full_fields_present", "no_nonempty_field_obligation"}
                or debt["release_status"] != "unknown_release"):
            raise ValueError("Unexpected field obligations or promoted release state in debt")
        by_language[language].append(dict(language=language, region=census.REGIONS[language],
            local_voice_id=identity, composite_identity=debt["composite_identity"], raw_record=record,
            debt=debt, raw_file=sources[language], expected_full_text_sha256=sha(census.rendered_text(record).encode()),
            expected_public_labeled_text_sha256=sha(census.rendered_text(record, field_labels=True).encode())))
    requests = []
    for language in census.REGIONS:
        candidates = sorted(by_language[language], key=lambda request: request["local_voice_id"])
        if per_language and len(candidates) < per_language:
            raise ValueError("Exact batch size is unavailable for language: " + language)
        requests.extend(candidates[:per_language] if per_language else candidates)
    if not requests or set(request["language"] for request in requests) != set(census.REGIONS):
        raise ValueError("Offline home recovery requires nonempty obligations in all five regions")
    return dict(schema="sekaisync/p0-offline-home-debt-requests@1", prepared_at_utc=now(),
        inventory_summary=dict(path=str(summary_path.resolve()), sha256=sha(summary_raw)),
        debt_file=debt_evidence, raw_sources=sources, per_language=per_language,
        selection_basis="all debts" if not per_language else "lowest regional local IDs, exact count per language",
        selected_records=len(requests), selected_records_by_language=dict(Counter(request["language"] for request in requests)),
        release_status="unknown_release", requests=requests)


class OfflineHomeFetcher:
    def __init__(self, runtime, source: str, requests: list[dict], out: Path):
        site = runtime.site(source)
        if site is None or not site.enabled or site.backend != "moesekai":
            raise ValueError("Offline recovery requires a configured enabled MS source")
        endpoints = runtime.endpoints_for(source)
        self.values, self.locales, self.calls, self.phase = {}, {}, [], "first_pass"
        self.log_path = out / "offline-fetch-records.jsonl"
        self.log_path.touch(exist_ok=False)
        payload_dir = out / "offline-filtered-masterdata"
        payload_dir.mkdir()
        self.payloads = []
        for language in census.REGIONS:
            locales = [locale for locale, local_language in endpoints.ALTSOURCE_MS_LOCALE_LANGUAGES.items() if local_language == language]
            if len(locales) != 1:
                raise ValueError("Missing or ambiguous configured regional MS locale")
            self.locales[language] = locale = locales[0]
            server = endpoints.ALTSOURCE_MS_LOCALE_SERVERS[locale]
            rows = [request["raw_record"] for request in requests if request["language"] == language]
            payload = json.dumps(rows, ensure_ascii=False)
            path = payload_dir / (language + "-characterArchiveVoices.json")
            path.write_bytes(payload.encode())
            evidence = dict(language=language, regional_server=server, records=len(rows),
                path=str(path.resolve()), sha256=sha(path.read_bytes()), origin="exact_filtered_sealed_local_masterdata",
                actual_network_requests=0)
            self.payloads.append(evidence)
            for base in endpoints.ALTSOURCE_MS_METADATA_BASES:
                for name in MASTER_TABLES:
                    url = base.rstrip("/") + "/" + server + "/master/" + name + ".json"
                    self.values[bare_url(url)] = dict(text=payload if name == "characterArchiveVoices" else "[]",
                        language=language, table=name, payload=evidence if name == "characterArchiveVoices" else None)

    def __call__(self, url: str) -> str:
        if bare_url(url) not in self.values:
            raise OfflineScopeViolation("Offline public crawler attempted an unlisted URL; no network transport permitted: " + url)
        value = self.values[bare_url(url)]
        record = dict(url=url, role="offline_filtered_masterdata", origin="sealed_local_file_filter",
            phase=self.phase, finished_at_utc=now(), language=value["language"], table=value["table"],
            http_status=None, actual_network_requests=0, payload_sha256=sha(value["text"].encode()),
            payload_file=value["payload"]["path"] if value["payload"] else None,
            explicit_empty_scope_filter=value["payload"] is None)
        self.calls.append(record)
        with self.log_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")
        return value["text"]


def stored_pages(store: Path, source: str) -> dict:
    return load_existing_page_map(store, source) if (store / "kb/sekaisync.db").exists() else {}


def verify_pages(store: Path, source: str, requests: list[dict], fetcher: OfflineHomeFetcher) -> dict:
    pages = stored_pages(store, source)
    expected_ids, details = set(), []
    local_ids = {language: {} for language in census.REGIONS}
    for request in requests:
        local_ids[request["language"]][request["local_voice_id"]] = [request["raw_record"]]
    for request in requests:
        language, identity = request["language"], request["local_voice_id"]
        page_id = f"web:{source}:{fetcher.locales[language]}:home_line:{identity}"
        expected_ids.add(page_id)
        page = pages.get(page_id)
        if page is None:
            details.append(dict(language=language, local_voice_id=identity, page_id=page_id, status="missing"))
            continue
        audit = census.audit_page(page, local_ids)
        field_labeled = (audit["current_public_field_rendering_matches"]
            and all(row.get("rendering") == "field_labeled" for row in audit["field_coverage"] if row["nonempty_expected"]))
        details.append(dict(language=language, local_voice_id=identity, page_id=page_id,
            status="accepted_full_field_replay" if audit["complete_primary_text"] and field_labeled else "rejected_field_or_identity",
            expected_public_labeled_text_sha256=sha(census.rendered_text(request["raw_record"], field_labels=True).encode()),
            field_values_complete_after_one_structural_label_removal=field_labeled,
            structural_labels_credited_as_content=False,
            upstream_url_semantics="configured metadata origin address, not an HTTP request in this offline run",
            release_status="unknown_release", audit=audit))
    extras = set(pages) - expected_ids
    if extras:
        raise OfflineScopeViolation("Offline public crawler persisted unrequested pages: " + str(sorted(extras)[:10]))
    return dict(persisted_pages=len(pages), accepted_records=sum(row["status"] == "accepted_full_field_replay" for row in details),
        status_counts=dict(Counter(row["status"] for row in details)), details=details,
        page_text_identity_sha256=sha(json.dumps(sorted((page["id"], sha(str(page.get("text") or "").encode()))
            for page in pages.values()), ensure_ascii=False).encode()),
        page_content_and_provenance_sha256=sha(json.dumps(sorted((page["id"],
            {field: page.get(field) for field in ("text", "language", "kind", "canonical_key", "crawled_at",
                "source_hash", "asset_mismatch", "scenario_id_mismatch", "content_language_mismatch", "untranslated")})
            for page in pages.values()), ensure_ascii=False, sort_keys=True).encode()))


def run(inventory: Path, out: Path, production: Path, config_path: Path | None = None,
        source: str = "altsource_ms", per_language: int = 5, verify_resume: bool = True) -> dict:
    inventory, out, production = inventory.resolve(), out.resolve(), production.resolve()
    store = out / "store"
    isolation(store, production, out)
    if out == inventory or out.is_relative_to(inventory) or inventory.is_relative_to(out):
        raise ValueError("Offline recovery output must not overlap the frozen inventory")
    if out.exists() and any(out.iterdir()):
        raise ValueError("Offline recovery receipt is frozen or nonempty; use another output directory")
    manifest = prepare_requests(inventory, per_language)
    consent_path = web_consent_path(production)
    consent_raw = consent_path.read_bytes()
    consent = json.loads(consent_raw).get(source, {})
    if consent.get("accepted") is not True or consent.get("tos_version") != 1:
        raise ValueError("Existing explicit source TOS consent is missing; no production consent is rewritten")
    runtime = build_runtime(load_config(config_path, ROOT, store_override=store))
    code_paths = (Path(__file__), ROOT / "scripts/census_scraper_home_lines.py", ROOT / "sekaisync/crawler.py",
                  ROOT / "sekaisync/webindex.py", ROOT / "sekaisync/runtime.py")
    code_hashes = {str(path.resolve()): sha(path.read_bytes()) for path in code_paths}
    out.mkdir(parents=True, exist_ok=True)
    request_path = out / "exact-offline-home-requests.json"
    write_json(request_path, manifest)
    request_hash = sha(request_path.read_bytes())
    fetcher = OfflineHomeFetcher(runtime, source, manifest["requests"], out)
    receipt = dict(schema="sekaisync/p0-offline-isolated-home-backfill@1", started_at_utc=now(), source=source,
        production_store=str(production), production_store_mutated=False, original_census_mutated=False,
        isolated_store=str(store), actual_network_requests=0, acquisition_mode="offline_exact_sealed_masterdata_replay",
        robots_status="not_requested_no_remote_transport_or_assets", metadata_url_semantics="upstream origin address only; every fetch is logged as local injected payload",
        consent_evidence=dict(path=str(consent_path), sha256=sha(consent_raw), source_record=consent),
        configured_runtime=dict(config_path=str(config_path.resolve()) if config_path else None,
            config_sha256=sha(config_path.read_bytes()) if config_path else None),
        selected_records=manifest["selected_records"], selected_records_by_language=manifest["selected_records_by_language"],
        request_file=dict(path=str(request_path), sha256=request_hash), input_code_sha256=code_hashes,
        inventory_summary=manifest["inventory_summary"], debt_file=manifest["debt_file"], raw_sources=manifest["raw_sources"],
        release_status="unknown_release", local_filtered_payloads=fetcher.payloads, crawl_results=[], errors=[])
    passes = ("first_pass", "resume_same_master") if verify_resume else ("first_pass",)
    for phase in passes:
        fetcher.phase = phase
        for language in census.REGIONS:
            try:
                result = crawler.crawl_altsource_ms(store, locales=(fetcher.locales[language],), depth=3, limit=0,
                    accept_tos=False, tos_already_checked=True, delay=0, workers=1, resume=True,
                    include_overlay=False, fetcher=fetcher, instance=source, runtime=runtime)
                receipt["crawl_results"].append(dict(phase=phase, language=language, **result))
            except Exception as exc:
                receipt["errors"].append(dict(phase=phase, language=language, type=type(exc).__name__, message=str(exc)))
                break
        verification = verify_pages(store, source, manifest["requests"], fetcher)
        receipt[phase] = verification
        if receipt["errors"] or verification["accepted_records"] != manifest["selected_records"]:
            break
    current = prepare_requests(inventory, per_language)
    if any(current[field] != manifest[field] for field in ("inventory_summary", "debt_file", "raw_sources", "requests")):
        raise OfflineScopeViolation("Sealed inventory or exact requests changed during offline recovery")
    if (sha(request_path.read_bytes()) != request_hash or consent_path.read_bytes() != consent_raw
            or any(sha(Path(path).read_bytes()) != value for path, value in code_hashes.items())):
        raise OfflineScopeViolation("Execution inputs, code or production consent changed during offline recovery")
    receipt["resume_idempotent"] = (not verify_resume or ("resume_same_master" in receipt
        and receipt["first_pass"]["page_text_identity_sha256"] == receipt["resume_same_master"]["page_text_identity_sha256"]
        and receipt["first_pass"]["page_content_and_provenance_sha256"] == receipt["resume_same_master"]["page_content_and_provenance_sha256"]
        and receipt["first_pass"]["persisted_pages"] == receipt["resume_same_master"]["persisted_pages"]))
    receipt.update(finished_at_utc=now(), offline_fetch_calls=len(fetcher.calls),
        offline_fetch_log=dict(path=str(fetcher.log_path), sha256=sha(fetcher.log_path.read_bytes())))
    last_phase = receipt.get("resume_same_master", receipt.get("first_pass", {}))
    receipt["status"] = "complete" if (not receipt["errors"] and receipt["resume_idempotent"]
        and last_phase.get("accepted_records") == manifest["selected_records"]) else "partial"
    write_json(out / "report.json", receipt)
    return receipt


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    base = ROOT / "work/p0-exhaustive-20261001"
    parser.add_argument("--inventory", type=Path, default=base / "home-voice-inventory-b-01")
    parser.add_argument("--out", type=Path, default=base / "home-voice-backfill-sample-ms-01")
    parser.add_argument("--production-store", type=Path, default=ROOT / "store")
    parser.add_argument("--config", type=Path, default=ROOT / "settings.local.json")
    parser.add_argument("--source", default="altsource_ms")
    parser.add_argument("--per-language", type=int, default=5, help="Exact sample per language, or 0 for every frozen acquisition debt")
    args = parser.parse_args()
    result = run(args.inventory, args.out, args.production_store, args.config, args.source, args.per_language)
    print(json.dumps({key: value for key, value in result.items() if key not in {"first_pass", "resume_same_master", "local_filtered_payloads", "raw_sources"}},
                     ensure_ascii=False, indent=2))
    if result["status"] != "complete":
        raise SystemExit(2)


if __name__ == "__main__":
    main()
