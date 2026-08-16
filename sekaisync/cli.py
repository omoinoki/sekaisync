from __future__ import annotations


import argparse
import json
import sys
from pathlib import Path

from sekaisync.config import DEFAULT_REGION_ORDER, SekaiSyncConfig, load_config, region_keys
from sekaisync.eventalias import build_event_alias_map, resolve_event_alias
from sekaisync.layout import (
    factpack_path,
    glossary_path,
    progress_path,
    region_master_dir,
    seed_glossary_path,
    terms_path,
    web_category_dir,
    web_consent_path,
    web_index_path,
    web_pages_path,
    write_manifest,
)
from sekaisync.event_detection import check_events, list_events
from sekaisync.core import SekaiSyncCore
from sekaisync.crawler import crawl_altsource_ms, crawl_altsource_sv, require_tos_consent
from sekaisync.sources import (
    BACKEND_MOESEKAI,
    BACKEND_SEKAI_VIEWER,
    auxiliary_source_for_instance,
)
from sekaisync.fetcher import sync
from sekaisync.fetcher import write_freshness
from sekaisync.http_server import serve_http
from sekaisync.llm_client import LLMClient, load_llm_config
from sekaisync.mcp_server import run_mcp_server
from sekaisync.termindex import (
    TERM_STORY_KINDS,
    extract_terms,
    extract_terms_local,
    build_translation_memory,
    load_pages,
    load_terms,
    lookup_terms,
    merge_terms,
    page_story_key,
    save_terms,
    seed_from_glossary,
    term_status,
    term_to_dict,
)
from sekaisync.webindex import (
    auxiliary_page_summary,
    load_web_category_counts,
    rebuild_web_index,
)


DEMO_CHARACTERS = [
    {
        "id": 1,
        "firstName": "一歌",
        "lastName": "星乃",
        "unit": "Leo/need",
        "birthday": "3月7日",
        "height": "163cm",
        "school": "神山高中",
        "names": {
            "ja": "星乃一歌",
            "en": "Hoshino Ichika",
            "zh_tw": "星乃一歌",
            "zh_hans": "星乃一歌",
            "ko": "호시노 이치카",
        },
    },
    {
        "id": 2,
        "firstName": "咲希",
        "lastName": "天馬",
        "unit": "Leo/need",
        "birthday": "5月9日",
        "height": "153cm",
        "school": "宮益坂女子学院",
        "names": {
            "ja": "天馬咲希",
            "en": "Tenma Saki",
            "zh_tw": "天馬咲希",
            "zh_hans": "天马咲希",
            "ko": "텐마 사키",
        },
    },
]

DEMO_SONGS = [
    {
        "id": 1,
        "name": "Tell Your World",
        "composer": "kz",
        "lyricist": "kz",
        "arranger": "kz",
        "bpm": 150,
        "names": {
            "ja": "Tell Your World",
            "en": "Tell Your World",
            "zh_tw": "Tell Your World",
            "zh_hans": "Tell Your World",
            "ko": "Tell Your World",
        },
    }
]


def create_demo_store(store_root: Path) -> Path:
    demo_root = region_master_dir(store_root, "demo")
    demo_root.mkdir(parents=True, exist_ok=True)
    (demo_root / "gameCharacters.json").write_text(
        json.dumps(DEMO_CHARACTERS, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    (demo_root / "musics.json").write_text(
        json.dumps(DEMO_SONGS, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    seed = [
        {
            "id": "game_title",
            "kind": "game",
            "canonical": "Hatsune Miku: Colorful Stage!",
            "names": {
                "ja": "プロジェクトセカイ カラフルステージ！ feat. 初音ミク",
                "en": "Hatsune Miku: Colorful Stage!",
                "zh_tw": "世界計畫 繽紛舞台！ feat. 初音未來",
                "zh_hans": "世界计划 缤纷舞台！ feat. 初音未来",
                "ko": "프로젝트 세카이 컬러풀 스테이지! feat. 하츠네 미쿠",
            },
            "official": True,
            "source": "official_game_title",
            "demo": True,
        },
        {
            "id": "unit:leo_need",
            "kind": "unit",
            "canonical": "Leo/need",
            "names": {
                "ja": "Leo/need",
                "en": "Leo/need",
                "zh_tw": "Leo/need",
                "zh_hans": "Leo/need",
                "ko": "Leo/need",
            },
            "official": True,
            "source": "official_unit_name",
            "demo": True,
        },
    ]
    seed_glossary_path(store_root).parent.mkdir(parents=True, exist_ok=True)
    (seed_glossary_path(store_root)).write_text(
        json.dumps(seed, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    (demo_root / "README.md").write_text(
        "# Demo data\n\nThese files are synthetic fixtures for local testing. Run `sekaisync sync` to import real master data.\n",
        encoding="utf-8",
    )
    return demo_root


def config_from_args(args: argparse.Namespace) -> SekaiSyncConfig:
    return load_config(
        args.config,
        Path.cwd(),
        store_override=getattr(args, "store", None),
    )


def cmd_init(args: argparse.Namespace) -> int:
    config = config_from_args(args)
    config.store_root.mkdir(parents=True, exist_ok=True)
    write_manifest(config.store_root)
    if args.demo:
        demo_root = create_demo_store(config.store_root)
        sync(config, ["demo"])
        print(f"Demo store initialized at {demo_root}")
    else:
        print(f"Store root initialized at {config.store_root}")
    return 0
def cmd_sync(args: argparse.Namespace) -> int:
    config = config_from_args(args)
    regions = region_keys(args.regions.split(",") if args.regions else DEFAULT_REGION_ORDER)
    local_mirrors = {}
    for item in args.local or []:
        key, _, value = item.partition("=")
        local_mirrors[key.strip()] = Path(value.strip())
    result = sync(config, regions, local_mirrors=local_mirrors)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


def cmd_lookup(args: argparse.Namespace) -> int:
    config = config_from_args(args)
    core = SekaiSyncCore(config.store_root)
    results = core.lookup(
        args.query,
        type=args.type,
        region=args.region,
        language=args.language,
        limit=args.limit,
    )
    print(json.dumps(results, ensure_ascii=False, indent=2))
    return 0


def cmd_resolve(args: argparse.Namespace) -> int:
    config = config_from_args(args)
    core = SekaiSyncCore(config.store_root)
    results = core.resolve_name(
        args.query,
        target_language=args.target_language,
        source_language=args.source_language,
        kind=args.kind,
    )
    print(json.dumps(results, ensure_ascii=False, indent=2))
    return 0


def cmd_factpack(args: argparse.Namespace) -> int:
    config = config_from_args(args)
    core = SekaiSyncCore(config.store_root)
    pack = core.fact_pack(args.id, language=args.language)
    if pack is None:
        print(json.dumps({"error": f"Entity not found: {args.id}"}, ensure_ascii=False))
        return 1
    print(json.dumps(pack, ensure_ascii=False, indent=2))
    return 0


def cmd_stats(args: argparse.Namespace) -> int:
    config = config_from_args(args)
    from sekaisync.factpacks import load_fact_packs

    packs = load_fact_packs(factpack_path(config.store_root, "en"))
    total_raw = sum(p.raw_json_tokens for p in packs)
    total_pack = sum(p.fact_pack_tokens for p in packs)
    print(
        json.dumps(
            {
                "fact_packs": len(packs),
                "raw_json_tokens": total_raw,
                "fact_pack_tokens": total_pack,
                "token_ratio": round(total_pack / total_raw, 3) if total_raw else 1.0,
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


def web_status_from_store(store_root: Path) -> dict:
    consent_path = web_consent_path(store_root)
    index_path = web_index_path(store_root)
    consent = False
    if consent_path.exists():
        data = json.loads(consent_path.read_text(encoding="utf-8"))
        consent = bool(data)
    sources = {}
    if index_path.exists():
        sources = json.loads(index_path.read_text(encoding="utf-8")).get("sources", {})
    return {
        "enabled": bool(sources) and consent,
        "consent": consent,
        "sources": sources,
        "category_counts": load_web_category_counts(store_root),
        "auxiliary": auxiliary_page_summary(store_root),
    }


def cmd_crawl(args: argparse.Namespace) -> int:
    config = config_from_args(args)
    requested = (
        [item.strip().lower() for item in args.sources.split(",") if item.strip()]
        if args.sources
        else []
    )
    # Instance IDs in profile order; type selectors were expanded by resolve_sources.
    instances = config.resolve_sources(requested)

    require_tos_consent(args.accept_tos)
    if args.no_resume:
        # Remove per-instance page files (and their auxiliary counterparts)
        # plus legacy dirs so a forced re-crawl cannot merge with old data.
        for instance_id in instances:
            for source in (
                instance_id,
                auxiliary_source_for_instance(instance_id, config.instance_backend(instance_id)),
            ):
                pages_path = web_pages_path(config.store_root, source)
                if pages_path.exists():
                    pages_path.unlink()
        for legacy in ("altsource", "altsource_translation", "sekai_viewer", "sekai_viewer_i18n"):
            pages_path = web_pages_path(config.store_root, legacy)
            if pages_path.exists():
                pages_path.unlink()
    summaries = []
    for instance_id in instances:
        site = config.site_for(instance_id)
        backend = site.backend if site is not None else config.instance_backend(instance_id)
        if backend == BACKEND_MOESEKAI:
            summaries.append(
                crawl_altsource_ms(
                    config.store_root,
                    locales=[item.strip() for item in args.locales.split(",") if item.strip()],
                    limit=args.limit,
                    accept_tos=True,
                    delay=args.delay,
                    tos_already_checked=True,
                    depth=args.depth,
                    workers=args.workers,
                    resume=not args.no_resume,
                    include_overlay=not args.no_overlay,
                    settings=config.instance_settings(instance_id),
                    instance=instance_id,
                )
            )
        elif backend == BACKEND_SEKAI_VIEWER:
            regions = region_keys(args.regions.split(",") if args.regions else ("jp",))
            summaries.append(
                crawl_altsource_sv(
                    config.store_root,
                    regions=regions,
                    limit=args.limit,
                    accept_tos=True,
                    delay=args.delay,
                    tos_already_checked=True,
                    depth=args.depth,
                    workers=args.workers,
                    resume=not args.no_resume,
                    include_i18n=not args.no_i18n,
                    settings=config.instance_settings(instance_id),
                    instance=instance_id,
                )
            )

    from sekaisync.postprocess import mark_untranslated_pages

    postprocess = mark_untranslated_pages(config.store_root)
    web_status = web_status_from_store(config.store_root)
    from sekaisync.news import news_available

    write_freshness(
        config,
        config.regions,
        web_status=web_status,
        news_available=news_available(config.store_root),
    )
    print(
        json.dumps(
            {
                "store": str(config.store_root.resolve()),
                "web": web_status,
                "crawl": summaries,
                "postprocess": postprocess,
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


def cmd_web_rebuild(args: argparse.Namespace) -> int:
    config = config_from_args(args)
    result = rebuild_web_index(config.store_root)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


def cmd_postprocess(args: argparse.Namespace) -> int:
    config = config_from_args(args)
    from sekaisync.postprocess import mark_untranslated_pages

    result = mark_untranslated_pages(config.store_root, placeholder=args.placeholder)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


def cmd_news_sync(args: argparse.Namespace) -> int:
    config = config_from_args(args)
    from sekaisync.fetcher import write_freshness
    from sekaisync.news import sync_news

    regions = tuple(
        item.strip()
        for item in args.regions.split(",")
        if item.strip()
    )
    sources = tuple(
        item.strip()
        for item in (args.sources or "").split(",")
        if item.strip()
    )
    resolved = config.resolve_sources(sources)
    result = sync_news(
        config.store_root,
        regions=regions,
        sources=resolved,
        source_priority=config.source_priority(),
        sites=config.sites,
    )
    web_status = web_status_from_store(config.store_root)
    write_freshness(
        config,
        config.regions,
        web_status=web_status,
        news_available=True,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


def cmd_news_list(args: argparse.Namespace) -> int:
    config = config_from_args(args)
    from sekaisync.news import load_news

    records = load_news(config.store_root)
    if args.language:
        records = [
            record
            for record in records
            if record.get("language") == args.language
        ]
    print(
        json.dumps(
            records[: args.limit],
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


def cmd_web_search(args: argparse.Namespace) -> int:
    config = config_from_args(args)
    core = SekaiSyncCore(config.store_root)
    results = core.web_lookup(
        args.query,
        source=args.source,
        language=args.language,
        limit=args.limit,
        include_text=args.full,
        include_overlay=args.include_overlay,
        source_priority=config.source_priority(),
    )
    print(json.dumps(results, ensure_ascii=False, indent=2))
    return 0


def cmd_query(args: argparse.Namespace) -> int:
    config = config_from_args(args)
    core = SekaiSyncCore(config.store_root)
    result = core.query(
        args.query,
        type=args.type,
        region=args.region,
        language=args.language,
        limit=args.limit,
        include_overlay=args.include_overlay,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    config = config_from_args(args)
    core = SekaiSyncCore(config.store_root)
    web_status = web_status_from_store(config.store_root)
    from sekaisync.news import news_available

    write_freshness(
        config,
        config.regions,
        web_status=web_status,
        news_available=news_available(config.store_root),
    )
    print(
        json.dumps(
            {
                "store": str(config.store_root.resolve()),
                "master": core.store_stats(),
                "web": web_status,
                "terms": core.term_status(),
                "trust": core.trust_summary(),
                "progress": core.progress(),
                "event_check": getattr(args, "auto_event_check", None),
                "news": core.news(),
                "freshness": core.freshness(),
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


def cmd_progress(args: argparse.Namespace) -> int:
    config = config_from_args(args)
    core = SekaiSyncCore(config.store_root)
    regions = region_keys(args.regions.split(",") if args.regions else DEFAULT_REGION_ORDER)
    result = core.progress(regions=list(regions), live=args.live, master_base=config.viewer.master_base)
    from sekaisync.progress import save_progress

    save_progress(config.store_root, result)
    if args.plain:
        rows = [
            ["region", "current_event", "released_events", "fact%", "text%", "overall%"]
        ]
        for region in regions:
            data = result["regions"][region]
            current = (data["activity"].get("current_event") or {}).get("id")
            rows.append(
                [
                    region,
                    str(current or "-"),
                    str(data["activity"]["released_events"]),
                    str(data["fact"]["pct"]),
                    str(data["text"]["pct"]),
                    str(data["overall"]["pct"]),
                ]
            )
        overall = result["overall"]
        rows.append(
            [
                "ALL",
                "-",
                "-",
                str(overall["fact"]["pct"]),
                str(overall["text"]["pct"]),
                str(overall["pct"]),
            ]
        )
        width = max(len(cell) for row in rows for cell in row)
        for row in rows:
            print("  ".join(cell.rjust(width) for cell in row))
        print()
        print(result["caveat"])
        return 0
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


def cmd_integrity(args: argparse.Namespace) -> int:
    config = config_from_args(args)
    from sekaisync.integrity import run_integrity_check

    result = run_integrity_check(config.store_root, limit=args.limit)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


def cmd_terms_init(args: argparse.Namespace) -> int:
    config = config_from_args(args)
    path = terms_path(config.store_root)
    existing = [] if args.reset else load_terms(path)
    seeded = seed_from_glossary(config.store_root)
    merged = merge_terms([*existing, *seeded])
    save_terms(merged, path, compact_evidence=True)
    print(
        json.dumps(
            {
                "path": str(path),
                "seeded": len(seeded),
                "terms": len(merged),
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


def cmd_terms_extract(args: argparse.Namespace) -> int:
    config = config_from_args(args)
    pages = load_pages(
        config.store_root,
        args.input,
        include_overlay=args.include_overlay,
    )
    if not pages:
        raise ValueError("No local web pages found; prepare a store or pass --input JSON.")

    keys: set[str] = set()
    if args.event is not None:
        prefix = f"event:{args.event}:"
        keys = {
            key
            for page in pages
            if str(page.get("kind", "")) in TERM_STORY_KINDS
            and (key := page_story_key(page))
            and key.startswith(prefix)
        }
        if args.episode is not None:
            exact = f"event:{args.event}:{args.episode}"
            keys = {key for key in keys if key == exact}
    elif args.all:
        keys = {
            key
            for page in pages
            if str(page.get("kind", "")) in TERM_STORY_KINDS
            and (key := page_story_key(page))
        }
    else:
        raise ValueError("Use --event/--episode or --all to select story pages.")
    if not keys:
        raise ValueError("No matching story pages found in the local store.")

    selected = [page for page in pages if page_story_key(page) in keys]
    target_languages = [
        item.strip()
        for item in args.languages.split(",")
        if item.strip()
    ]
    path = terms_path(config.store_root)
    existing = load_terms(path)
    if args.local:
        memory = {}
        if len(pages) > 1:
            memory = build_translation_memory(pages, args.source_language, target_languages)
        records = extract_terms_local(
            selected,
            args.source_language,
            target_languages,
            existing=existing,
            max_terms_per_page=args.max_terms,
            translation_memory=memory,
        )
        llm_model = "local"
    else:
        llm = LLMClient(load_llm_config(args.llm_config))
        records = extract_terms(
            selected,
            args.source_language,
            target_languages,
            llm,
            existing=existing,
            max_terms_per_page=args.max_terms,
            include_translations=not args.no_translate,
        )
        llm_model = llm.config.model
    records = merge_terms(records)
    save_terms(records, path, compact_evidence=True)
    print(
        json.dumps(
            {
                "path": str(path),
                "story_key_count": len(keys),
                "story_keys": sorted(keys)[:20],
                "pages": len(selected),
                "source_language": args.source_language,
                "target_languages": target_languages,
                "terms": len(records),
                "llm_model": llm_model,
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


def cmd_terms_lookup(args: argparse.Namespace) -> int:
    config = config_from_args(args)
    path = terms_path(config.store_root)
    terms = load_terms(path)
    languages = [
        item.strip()
        for item in args.languages.split(",")
        if item.strip()
    ]
    results = lookup_terms(
        terms,
        args.query,
        source_language=args.language,
        languages=languages,
        limit=args.limit,
    )
    print(
        json.dumps(
            {
                "query": args.query,
                "language": args.language,
                "results": results,
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


def cmd_terms_list(args: argparse.Namespace) -> int:
    config = config_from_args(args)
    path = terms_path(config.store_root)
    terms = load_terms(path)
    if args.kind:
        terms = [term for term in terms if term.kind == args.kind]
    terms.sort(key=lambda term: term.canonical)
    print(
        json.dumps(
            [term_to_dict(term) for term in terms[: args.limit]],
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0




def rebuild_indexes_after_event_check(config: SekaiSyncConfig) -> None:
    """Refresh registry/glossary/factpacks after incremental event data imports.

    Delegates to the same index pipeline used by ``sync`` so the incremental
    event check and a full sync always produce identical indexes.
    """
    from sekaisync.fetcher import rebuild_indexes
    from sekaisync.registry import data_files_for_region

    regions = tuple(r for r in config.regions if r == "demo" or data_files_for_region(config.store_root, r))
    rebuild_indexes(config, regions)


def auto_event_check(
    config: SekaiSyncConfig,
    regions: list[str],
    timeout: int = 20,
    force: bool = False,
) -> dict:
    """Run the no-crawl new-event check before a CLI command."""
    from sekaisync.event_detection import apply_master_base

    apply_master_base(config.viewer.master_base)
    daily_limit = bool(config.extra.get("event_check_daily_limit", True)) and not force
    return check_events(
        config.store_root,
        regions=[r for r in regions if r in {"jp", "en", "tc", "kr", "cn"}],
        timeout=timeout,
        daily_limit=daily_limit,
    )


def cmd_events_check(args: argparse.Namespace) -> int:
    config = config_from_args(args)
    from sekaisync.event_detection import apply_master_base

    apply_master_base(config.viewer.master_base)
    regions = [
        item.strip()
        for item in args.regions.split(",")
        if item.strip()
    ]
    result = check_events(config.store_root, regions=regions, timeout=args.timeout, allow_initial=True)
    if result["detected_total"]:
        rebuild_indexes_after_event_check(config)
        result["indexes_rebuilt"] = True
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


def cmd_events_list(args: argparse.Namespace) -> int:
    config = config_from_args(args)
    regions = [
        item.strip()
        for item in args.regions.split(",")
        if item.strip()
    ]
    result = list_events(config.store_root, regions=regions, limit=args.limit)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0
def cmd_event_alias(args: argparse.Namespace) -> int:
    config = config_from_args(args)
    regions = [
        item.strip()
        for item in args.regions.split(",")
        if item.strip()
    ]
    if args.list:
        result = build_event_alias_map(config.store_root, regions=regions)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    result = resolve_event_alias(config.store_root, args.query, regions=regions)
    if result is None:
        print(
            json.dumps(
                {
                    "query": args.query,
                    "error": "No event alias match; expected forms such as khn3, 豆三箱, 小豆泽心羽三箱",
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0
def cmd_activity(args: argparse.Namespace) -> int:
    config = config_from_args(args)
    from sekaisync.eventalias import resolve_activity

    regions = [
        item.strip()
        for item in args.regions.split(",")
        if item.strip()
    ]
    result = resolve_activity(config.store_root, args.query, regions=regions)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


def cmd_wl(args: argparse.Namespace) -> int:
    config = config_from_args(args)
    from sekaisync.worldlink import build_wl_map, resolve_wl

    regions = [
        item.strip()
        for item in args.regions.split(",")
        if item.strip()
    ]
    if args.list:
        result = build_wl_map(config.store_root, regions=regions)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    result = resolve_wl(config.store_root, args.query, regions=regions)
    if result is None:
        print(
            json.dumps(
                {
                    "query": args.query,
                    "error": "No World Link match; expected forms such as vbs wl2, vs wl, finale, wl3第2组, wl2g7, wl3(=round3)",
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


def cmd_terms_status(args: argparse.Namespace) -> int:
    config = config_from_args(args)
    path = terms_path(config.store_root)
    print(
        json.dumps(
            {
                "path": str(path),
                **term_status(load_terms(path)),
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


def cmd_kb_status(args: argparse.Namespace) -> int:
    config = config_from_args(args)
    from sekaisync.layout import (
        events_archive_path,
        factpack_path,
        freshness_path,
        glossary_path,
        load_manifest,
        progress_path,
        registry_path,
        terms_path,
        web_index_path,
    )
    manifest = load_manifest(config.store_root)
    print(
        json.dumps(
            {
                "store": str(config.store_root.resolve()),
                "layout": manifest.get("layout", "v2"),
                "manifest": manifest,
                "paths": {
                    "registry": str(registry_path(config.store_root)),
                    "glossary": str(glossary_path(config.store_root)),
                    "terms": str(terms_path(config.store_root)),
                    "events": str(events_archive_path(config.store_root)),
                    "web_index": str(web_index_path(config.store_root)),
                    "freshness": str(freshness_path(config.store_root)),
                    "progress": str(progress_path(config.store_root)),
                },
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


def cmd_serve_mcp(args: argparse.Namespace) -> int:
    config = config_from_args(args)
    core = SekaiSyncCore(config.store_root)
    return run_mcp_server(core)


def cmd_serve_http(args: argparse.Namespace) -> int:
    config = config_from_args(args)
    core = SekaiSyncCore(config.store_root)
    serve_http(core, host=args.host, port=args.port, sites=config.sites)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="sekaisync", description="Project Sekai local knowledge sync")
    parser.add_argument("--config", type=Path, default=None)
    parser.add_argument(
        "--store",
        type=Path,
        default=None,
        help="Override the local store root (default: ./store)",
    )
    parser.add_argument(
        "--no-event-check",
        action="store_true",
        help="Skip the automatic new-event detection that runs before every command",
    )
    parser.add_argument(
        "--force-event-check",
        action="store_true",
        help="Bypass the once-per-Tokyo-day automatic event check limit",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_init = sub.add_parser("init", help="Initialize empty store or demo store")
    p_init.add_argument("--demo", action="store_true", help="Create synthetic demo data")
    p_init.set_defaults(func=cmd_init)

    p_kb_status = sub.add_parser("kb-status", help="Show the active store layout and JSON paths")
    p_kb_status.set_defaults(func=cmd_kb_status)

    p_sync = sub.add_parser("sync", help="Sync master data and rebuild indexes")
    p_sync.add_argument("--regions", default=",".join(DEFAULT_REGION_ORDER), help="Comma-separated region keys")
    p_sync.add_argument("--local", action="append", help="Local mirror as REGION=PATH; repeatable")
    p_sync.set_defaults(func=cmd_sync)

    p_lookup = sub.add_parser("lookup", help="Look up entities")
    p_lookup.add_argument("--query", required=True)
    p_lookup.add_argument("--type", default=None)
    p_lookup.add_argument("--region", default=None)
    p_lookup.add_argument("--language", default=None)
    p_lookup.add_argument("--limit", type=int, default=8)
    p_lookup.set_defaults(func=cmd_lookup)

    p_resolve = sub.add_parser("resolve", help="Resolve official localized names")
    p_resolve.add_argument("--query", required=True)
    p_resolve.add_argument("--target-language", default="zh_tw")
    p_resolve.add_argument("--source-language", default=None)
    p_resolve.add_argument("--kind", default=None)
    p_resolve.set_defaults(func=cmd_resolve)

    p_pack = sub.add_parser("factpack", help="Render a compact fact pack")
    p_pack.add_argument("--id", required=True)
    p_pack.add_argument("--language", default="en")
    p_pack.set_defaults(func=cmd_factpack)

    p_stats = sub.add_parser("stats", help="Show fact-pack token savings")
    p_stats.set_defaults(func=cmd_stats)

    p_crawl = sub.add_parser(
        "crawl",
        help="Crawl text-only data from registered altsource_sv / altsource_ms instances",
    )
    p_crawl.add_argument(
        "--sources",
        default=None,
        help=(
            "Comma-separated instance IDs or backend class IDs (altsource_sv / "
            "altsource_ms); default: all enabled instances in settings order"
        ),
    )
    p_crawl.add_argument("--locales", default="zh-cn", help="altsource_ms locales, comma-separated")
    p_crawl.add_argument("--regions", default="jp", help="altsource_sv regions, comma-separated")
    p_crawl.add_argument("--limit", type=int, default=10, help="Maximum pages/records to crawl per source")
    p_crawl.add_argument("--delay", type=float, default=0.5, help="Seconds between requests")
    p_crawl.add_argument("--workers", type=int, default=4, help="Concurrent text fetches per source")
    p_crawl.add_argument("--no-resume", action="store_true", help="Ignore locally crawled pages and redownload everything")
    p_crawl.add_argument("--accept-tos", action="store_true", help="Confirm TOS compliance without interactive prompt")
    p_crawl.add_argument(
        "--depth",
        type=int,
        default=1,
        choices=[1, 2, 3, 4],
        help="Crawl depth: 1=main+event, 2=+card, 3=+virtual live+home dialogue, 4=all text",
    )
    p_crawl.add_argument(
        "--no-overlay",
        action="store_true",
        help="Skip altsource sentence-level translation reference pages during the crawl",
    )
    p_crawl.add_argument(
        "--no-i18n",
        action="store_true",
        help="Skip Sekai Viewer i18n title/name reference pages during the crawl",
    )
    p_crawl.add_argument(
        "--store",
        type=Path,
        default=argparse.SUPPRESS,
        help="Override the local store root (default: ./store)",
    )
    p_crawl.set_defaults(func=cmd_crawl)

    p_web = sub.add_parser("web-search", help="Search the local crawled web index")
    p_web.add_argument("--query", required=True)
    p_web.add_argument(
        "--source",
        default=None,
        help=(
            "Instance ID or backend class ID (altsource_sv / altsource_ms, "
            "matching every instance of that class); legacy names are accepted"
        ),
    )
    p_web.add_argument("--language", default=None)
    p_web.add_argument("--limit", type=int, default=8)
    p_web.add_argument("--full", action="store_true", help="Include the full crawled text in results")
    p_web.add_argument(
        "--include-overlay",
        action="store_true",
        help="Also search auxiliary translation reference pages stored with the story crawl",
    )
    p_web.add_argument(
        "--store",
        type=Path,
        default=argparse.SUPPRESS,
        help="Override the local store root (default: ./store)",
    )
    p_web.set_defaults(func=cmd_web_search)

    p_query = sub.add_parser("query", help="Unified metadata and crawled text query")
    p_query.add_argument("--query", required=True)
    p_query.add_argument("--type", default=None)
    p_query.add_argument("--region", default=None)
    p_query.add_argument("--language", default=None)
    p_query.add_argument("--limit", type=int, default=8)
    p_query.add_argument(
        "--include-overlay",
        action="store_true",
        help="Also return auxiliary translation reference matches from the web index",
    )
    p_query.set_defaults(func=cmd_query)

    p_status = sub.add_parser("status", help="Show store layer and web category status")
    p_status.set_defaults(func=cmd_status)

    p_progress = sub.add_parser(
        "progress",
        help="Show per-region fact/text completeness as integer percentages",
    )
    p_progress.add_argument(
        "--regions",
        default=",".join(DEFAULT_REGION_ORDER),
        help="Comma-separated region keys",
    )
    p_progress.add_argument(
        "--live",
        action="store_true",
        help="Refresh activity schedules from sekai-world.github.io before computing",
    )
    p_progress.add_argument(
        "--plain",
        action="store_true",
        help="Print a compact human-readable percentage table",
    )
    p_progress.set_defaults(func=cmd_progress)

    p_integrity = sub.add_parser(
        "integrity",
        help="Check knowledge base entries for duplicates, hash integrity and source fidelity metadata",
    )
    p_integrity.add_argument(
        "--limit",
        type=int,
        default=20,
        help="Maximum issue/sample entries per layer",
    )
    p_integrity.set_defaults(func=cmd_integrity)

    p_web_rebuild = sub.add_parser(
        "web-rebuild",
        help="Rebuild web canonical keys, category files and merged index",
    )
    p_web_rebuild.add_argument(
        "--store",
        type=Path,
        default=argparse.SUPPRESS,
        help="Override the local store root (default: ./store)",
    )
    p_web_rebuild.set_defaults(func=cmd_web_rebuild)

    p_postprocess = sub.add_parser(
        "postprocess",
        help="Replace non-JP pages whose text exactly matches the JP original with an untranslated placeholder",
    )
    p_postprocess.add_argument(
        "--placeholder",
        default="[未翻译]",
        help="Placeholder text for untranslated copies",
    )
    p_postprocess.set_defaults(func=cmd_postprocess)

    p_news = sub.add_parser("news", help="Sync and list official news/announcements")
    news_sub = p_news.add_subparsers(dest="news_command", required=True)

    p_news_sync = news_sub.add_parser(
        "sync",
        help="Fetch news/announcements without requiring crawler TOS consent",
    )
    p_news_sync.add_argument(
        "--regions",
        default="jp,cn",
        help="altsource_ms regions to fetch (cn/jp)",
    )
    p_news_sync.add_argument(
        "--sources",
        default=None,
        help=(
            "Comma-separated instance IDs or backend class IDs; "
            "default: all enabled instances in settings order"
        ),
    )
    p_news_sync.set_defaults(func=cmd_news_sync)

    p_news_list = news_sub.add_parser("list", help="List local news/announcements")
    p_news_list.add_argument("--language", default=None)
    p_news_list.add_argument("--limit", type=int, default=100)
    p_news_list.set_defaults(func=cmd_news_list)

    p_terms = sub.add_parser("terms", help="Terminology extraction and cross-language lookup")
    terms_sub = p_terms.add_subparsers(dest="terms_command", required=True)

    p_terms_init = terms_sub.add_parser("init", help="Seed term index from the official glossary")
    p_terms_init.add_argument("--reset", action="store_true", help="Replace the current term index before seeding")
    p_terms_init.set_defaults(func=cmd_terms_init)

    p_terms_extract = terms_sub.add_parser("extract", help="Extract terms from local story text using an LLM")
    p_terms_extract.add_argument("--event", type=int, default=None)
    p_terms_extract.add_argument("--episode", type=int, default=None)
    p_terms_extract.add_argument("--all", action="store_true")
    p_terms_extract.add_argument("--source-language", default="ja")
    p_terms_extract.add_argument("--languages", default="ja,zh_hans,en,zh_tw,ko")
    p_terms_extract.add_argument("--llm-config", type=Path, default=None)
    p_terms_extract.add_argument("--input", type=Path, default=None)
    p_terms_extract.add_argument("--max-terms", type=int, default=20)
    p_terms_extract.add_argument("--no-translate", action="store_true")
    p_terms_extract.add_argument(
        "--local",
        action="store_true",
        help="Use deterministic local extraction instead of an LLM",
    )
    p_terms_extract.add_argument(
        "--include-overlay",
        action="store_true",
        help="Include auxiliary translation reference pages when selecting story text",
    )
    p_terms_extract.set_defaults(func=cmd_terms_extract)

    p_terms_lookup = terms_sub.add_parser("lookup", help="Look up a term and its cross-language names")
    p_terms_lookup.add_argument("--query", required=True)
    p_terms_lookup.add_argument("--language", default=None)
    p_terms_lookup.add_argument("--languages", default="ja,zh_hans,en,zh_tw,ko")
    p_terms_lookup.add_argument("--limit", type=int, default=8)
    p_terms_lookup.set_defaults(func=cmd_terms_lookup)

    p_terms_list = terms_sub.add_parser("list", help="List extracted terms")
    p_terms_list.add_argument("--kind", default=None)
    p_terms_list.add_argument("--limit", type=int, default=100)
    p_terms_list.set_defaults(func=cmd_terms_list)

    p_terms_status = terms_sub.add_parser("status", help="Show term index status")
    p_terms_status.set_defaults(func=cmd_terms_status)

    p_events = sub.add_parser("events", help="Detect, classify and archive new events without starting the crawler")
    events_sub = p_events.add_subparsers(dest="events_command", required=True)

    p_events_check = events_sub.add_parser("check", help="Compare remote master events, fetch base data for new events, classify and archive")
    p_events_check.add_argument("--regions", default=",".join(DEFAULT_REGION_ORDER), help="Comma-separated region keys")
    p_events_check.add_argument("--timeout", type=int, default=30, help="Seconds per remote request")
    p_events_check.add_argument(
        "--store",
        type=Path,
        default=argparse.SUPPRESS,
        help="Override the local store root (default: ./store)",
    )
    p_events_check.set_defaults(func=cmd_events_check)

    p_events_list = events_sub.add_parser("list", help="List archived event classifications")
    p_events_list.add_argument("--regions", default=",".join(DEFAULT_REGION_ORDER), help="Comma-separated region keys")
    p_events_list.add_argument("--limit", type=int, default=None, help="Maximum events per region")
    p_events_list.add_argument(
        "--store",
        type=Path,
        default=argparse.SUPPRESS,
        help="Override the local store root (default: ./store)",
    )
    p_events_list.set_defaults(func=cmd_events_list)
    p_alias = sub.add_parser(
        "alias",
        help="Resolve community event shorthand such as khn3 / 豆三箱 to box events",
    )
    p_alias.add_argument("--query", default=None, help="Shorthand to resolve (khn3, 豆三箱, ...)")
    p_alias.add_argument("--list", action="store_true", help="Print the full character box-event mapping")
    p_alias.add_argument("--regions", default=",".join(DEFAULT_REGION_ORDER), help="Comma-separated region keys")
    p_alias.add_argument(
        "--store",
        type=Path,
        default=argparse.SUPPRESS,
        help="Override the local store root (default: ./store)",
    )
    p_alias.set_defaults(func=cmd_event_alias)
    p_activity = sub.add_parser(
        "activity",
        help="Unified activity resolution: World Link shorthand first, then character box shorthand",
    )
    p_activity.add_argument("--query", required=True, help="Shorthand to resolve (wl2g7, vbs wl2, finale, wl3第2组, khn3, 豆三箱, ...)")
    p_activity.add_argument("--regions", default=",".join(DEFAULT_REGION_ORDER), help="Comma-separated region keys")
    p_activity.add_argument(
        "--store",
        type=Path,
        default=argparse.SUPPRESS,
        help="Override the local store root (default: ./store)",
    )
    p_activity.set_defaults(func=cmd_activity)
    p_wl = sub.add_parser(
        "wl",
        help="Resolve World Link shorthand such as vbs wl2 / finale / wl3第2组",
    )
    p_wl.add_argument("--query", default=None, help="Shorthand to resolve (vbs wl2, vs wl, group3, finale, wl1, ...)")
    p_wl.add_argument("--list", action="store_true", help="Print the full World Link mapping with rounds and groups")
    p_wl.add_argument("--regions", default=",".join(DEFAULT_REGION_ORDER), help="Comma-separated region keys")
    p_wl.add_argument(
        "--store",
        type=Path,
        default=argparse.SUPPRESS,
        help="Override the local store root (default: ./store)",
    )
    p_wl.set_defaults(func=cmd_wl)
    p_mcp = sub.add_parser("serve-mcp", help="Run MCP stdio server")
    p_mcp.set_defaults(func=cmd_serve_mcp)

    p_http = sub.add_parser("serve-http", help="Run local HTTP/OpenAPI server")
    p_http.add_argument("--host", default="127.0.0.1")
    p_http.add_argument("--port", type=int, default=8787)
    p_http.set_defaults(func=cmd_serve_http)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    args.auto_event_check = None
    command = getattr(args, "command", None)
    if not getattr(args, "no_event_check", False) and command not in {"init", "events", "kb-status"}:
        try:
            config = config_from_args(args)
            result = auto_event_check(config, list(config.regions), force=getattr(args, "force_event_check", False))
            args.auto_event_check = result
            if result.get("detected_total"):
                rebuild_indexes_after_event_check(config)
        except Exception as exc:
            args.auto_event_check = {"status": "error", "reason": str(exc)}
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())









