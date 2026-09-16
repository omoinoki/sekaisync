from __future__ import annotations


import argparse
import hashlib
import json
import sys
from pathlib import Path

from sekaisync import dbstore
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
from sekaisync.crawler import (
    crawl_altsource_ms,
    crawl_altsource_sv,
    probe_instance_health,
    require_tos_consent,
)
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
from sekaisync.trinity import build_candidate_pool, scrub_trinity
from sekaisync.zhfirst import _load_manual_seed
from sekaisync.termindex import (
    TERM_STORY_KINDS,
    build_alignment_resources,
    build_pair_story_index,
    extract_terms,
    extract_terms_local,
    build_translation_memory,
    group_pages_by_story,
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
    from sekaisync.crawler import acquire_crawl_lock, release_crawl_lock

    config = config_from_args(args)
    if not acquire_crawl_lock(config.store_root):
        print(json.dumps({"error": "Another crawl is already running on this store (crawl.lock). "
                                 "Wait for it to finish; the lock releases automatically when the "
                                 "crawling process exits."}, ensure_ascii=False))
        return 1
    import atexit

    atexit.register(release_crawl_lock, config.store_root)
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
    skipped_instances = []
    for instance_id in instances:
        site = config.site_for(instance_id)
        backend = site.backend if site is not None else config.instance_backend(instance_id)
        settings = config.instance_settings(instance_id)
        # P1: instance health probe — a dead instance is skipped so the rest of
        # the crawl keeps going, and the user sees which instance was skipped.
        if not probe_instance_health(instance_id, backend, settings=settings):
            skipped_instances.append(instance_id)
            print(
                f"[crawl] skipping {instance_id}: instance probe failed "
                f"(endpoint unreachable); continuing with the remaining instances",
                file=sys.stderr,
            )
            continue
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
                    settings=settings,
                    instance=instance_id,
                )
            )
        elif backend == BACKEND_SEKAI_VIEWER:
            regions = region_keys(args.regions.split(",") if args.regions else ("jp",))
            summaries.append(
                crawl_altsource_sv(
                    config.store_root,
                    regions=regions,
                    tables=tuple(x.strip() for x in args.sv_tables.split(",") if x.strip()) if args.sv_tables else None,
                    limit=args.limit,
                    accept_tos=True,
                    delay=args.delay,
                    tos_already_checked=True,
                    depth=args.depth,
                    workers=args.workers,
                    resume=not args.no_resume,
                    include_i18n=not args.no_i18n,
                    settings=settings,
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
                "skipped_instances": skipped_instances,
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
    existing = [] if args.reset else dbstore.load_terms_records(config.store_root, include_sentences=True)
    seeded = seed_from_glossary(config.store_root)
    merged = merge_terms([*existing, *seeded])
    dbstore.save_terms_records(config.store_root, merged, replace_evidence=True)
    path = dbstore.db_file(config.store_root)
    print(
        json.dumps(
            {
                "path": str(dbstore.db_file(config.store_root)),
                "seeded": len(seeded),
                "terms": len(merged),
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0





def _names_from_conflict(conflict: dict) -> dict:
    """冲突项 → 各语言的候选集合（取每个语言的首个候选作为提案）。"""
    names = {}
    for lang, values in (conflict.get("candidates") or {}).items():
        cand_values = list(values.keys()) if isinstance(values, dict) else list(values)
        if cand_values:
            names[str(lang)] = cand_values[0]
    return names


def _proposals_from_rows(rows: list) -> dict:
    """待裁决行 → apply_methodology_batch 需要的 {term: {lang: [candidates]} 结构。"""
    proposals = {}
    for row in rows:
        term = str(row.get("term") or "")
        if not term:
            continue
        slot = proposals.setdefault(term, {})
        for lang, value in (row.get("names") or {}).items():
            if not value:
                continue
            values_list = slot.setdefault(str(lang), [])
            if str(value) not in values_list:
                values_list.append(str(value))
    return proposals

def _review_item_id(term: str, language: str, values: tuple) -> str:
    """队列条目 id：稳定、可复现（入队去重与已结算判定都靠它）。

    必须走 hashlib —— 内置 ``hash()`` 对 str 是进程内随机化的（PYTHONHASHSEED）：
    同一术语每次运行会得到不同 id，去重与 ``skipped_settled`` 会永久失效。
    """
    raw = term + "" + language + "" + "".join(values)
    return "rv:" + hashlib.sha1(raw.encode("utf-8")).hexdigest()[:12]



def _review_items_from_trinity(result: dict) -> list:
    """把三位一体的 pending / conflicts 转成干预队列条目。

    * ``pending`` 带 names 的项 → pending（有候选可比）
    * ``pending`` 无 names 的项 → gate_failed（三路皆未产出，留空候选槽让
      智能体用自身知识 ``replace`` 补译名——这是干预环最有价值的场景）
    * ``conflicts`` 项 → conflict（多通道给出不同译名，交智能体裁决）
    """
    from datetime import datetime, timezone

    from sekaisync import agent_review

    now = datetime.now(timezone.utc).isoformat()
    items = []
    for row in result.get("pending") or []:
        term = str(row.get("term") or "")
        if not term:
            continue
        names = row.get("names") or {}
        if names:
            for lang, value in names.items():
                if not value:
                    continue
                items.append(
                    agent_review.ReviewItem(
                        id=_review_item_id(term, str(lang), (str(value),)),
                        kind="pending",
                        term=term,
                        language=str(lang),
                        candidates=[str(value)],
                        chosen_hint=str(value),
                        evidence=[],
                        story_keys=[],
                        channels=list(row.get("channels") or []),
                        reason=str(row.get("reason") or "低置信待裁决")[:200],
                        created_at=now,
                    )
                )
        else:
            items.append(
                agent_review.ReviewItem(
                    id=_review_item_id(term, "en", ()),
                    kind="gate_failed",
                    term=term,
                    language="en",
                    candidates=[],
                    chosen_hint=None,
                    evidence=[],
                    story_keys=[],
                    channels=list(row.get("channels") or []),
                    reason=str(row.get("reason") or "三路皆未产出，需外部知识")[:200],
                    created_at=now,
                )
            )
    for conflict in result.get("conflicts") or []:
        term = str(conflict.get("term") or "")
        raw_candidates = conflict.get("candidates") or {}
        for lang, values in raw_candidates.items():
            if not term or not values:
                continue
            cand_values = list(values.keys()) if isinstance(values, dict) else list(values)
            if not cand_values:
                continue
            items.append(
                agent_review.ReviewItem(
                    id=_review_item_id(term, str(lang), tuple(sorted(cand_values))),
                    kind="conflict",
                    term=term,
                    language=str(lang),
                    candidates=cand_values,
                    chosen_hint=cand_values[0],
                    evidence=[],
                    story_keys=[],
                    channels=[],
                    reason=str(conflict.get("reason") or "通道冲突")[:200],
                    created_at=now,
                )
            )
    return items


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
    existing = dbstore.load_terms_records(config.store_root, include_sentences=True)
    if getattr(args, "layered", False):
        # 三位一体路径（experiment 验证通过后才接入）：主干 ja 分布对齐 +
        # 辅助1 hub 扩散 + 辅助2 片假名音译，合并去重 + 交叉验证增益，
        # 未决项进智能体干预队列，已沉淀方法论直接套用。
        from sekaisync import agent_review
        from sekaisync.trinity import build_candidate_pool, scrub_trinity

        groups = group_pages_by_story(pages)
        layered_cache = config.store_root / "cache" / "trinity"
        layered_cache.mkdir(parents=True, exist_ok=True)
        # 候选池必须从语料构建（片假名串 ∪ 引号词 ∪ 统计发现词），
        # 不能用 existing——那样只会重复刮削已有术语，发现不了新词。
        candidate_pool, discovered = build_candidate_pool(
            groups, sorted(keys), source_language=args.source_language)
        glossary = list(dbstore.load_glossary_terms(config.store_root))
        vocab, idf = build_alignment_resources(groups, target_languages, glossary, layered_cache)
        pair_index = build_pair_story_index(groups, target_languages, args.source_language)
        result = scrub_trinity(
            groups,
            sorted(keys),
            candidate_pool,
            source_language=args.source_language,
            discovered=discovered,
            target_languages=tuple(target_languages),
            glossary=glossary,
            seed=_load_manual_seed(),
            vocab=vocab,
            idf=idf,
            pair_index=pair_index,
        )
        # 方法论只作用于**待裁决项**（pending / conflicts / gate_failed）——
        # accepted 是刮削已确认的结果，对它套用没有意义。做法是：
        #   1) 把待裁决项整理成 proposals，用方法论结算能确定的；
        #   2) 已结算的并入 accepted（补上译名），没结算的才入队。
        pending_rows = [
            row for row in (result.get("pending") or [])
            if str(row.get("term") or "")
        ] + [
            {"term": c.get("term"), "names": _names_from_conflict(c),
             "channels": [], "reason": c.get("reason") or "通道冲突"}
            for c in (result.get("conflicts") or [])
        ]
        proposals = _proposals_from_rows(pending_rows)
        applied = agent_review.apply_methodology_batch(config.store_root, proposals)
        settled = applied.get("settled") or {}
        accepted = result.get("accepted") or {}
        for term, langs in settled.items():
            record = accepted.setdefault(term, {"names": {}, "confidence": 0.9,
                                                "channels": ["methodology"],
                                                "agreement": 1})
            names = record.setdefault("names", {})
            for lang, value in (langs or {}).items():
                if value and not names.get(lang):
                    names[lang] = value
        # 已结算的术语不再入队
        review_items = [
            item for item in _review_items_from_trinity(result)
            if item.term not in settled]
        queued = agent_review.enqueue(config.store_root, review_items)
        print(
            json.dumps(
                {
                    "path": str(dbstore.db_file(config.store_root)),
                    "pipeline": "trinity",
                    "story_key_count": len(keys),
                    "methodology_settled": len(settled),
                    "accepted": len(accepted),
                    "pending": len(result.get("pending") or []),
                    "conflicts": len(result.get("conflicts") or []),
                    "rejected": len(result.get("rejected") or []),
                    "methodology_applied": len(applied.get("settled") or {}),
                    "queued_for_agent": queued,
                    "channel_stats": result.get("stats", {}),
                    "review_next": "sekaisync terms review export --out queue.txt",
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0
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
            cache_dir=config.store_root / "cache",
            do_align=getattr(args, "align", False),
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
    dbstore.save_terms_records(config.store_root, records, replace_evidence=True)
    print(
        json.dumps(
            {
                "path": str(dbstore.db_file(config.store_root)),
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
    terms = dbstore.load_terms_records(config.store_root)
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
        tag=getattr(args, "tag", None),
        sort=getattr(args, "sort", "score"),
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
    terms = dbstore.load_terms_records(config.store_root)
    if args.kind:
        terms = [term for term in terms if term.kind == args.kind]
    tag_filter = getattr(args, "tag", None)
    if tag_filter:
        terms = [term for term in terms if tag_filter in (term.tags or [])]
    sort_key = getattr(args, "sort", "canonical")
    if sort_key == "weight":
        terms.sort(key=lambda term: term.weight, reverse=True)
    else:
        terms.sort(key=lambda term: term.canonical)
    print(
        json.dumps(
            [term_to_dict(term) for term in terms[: args.limit]],
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0




def cmd_terms_export(args: argparse.Namespace) -> int:
    """Deterministic, auditable term export (JSON / CSV), no LLM calls.

    JSON format: a dict of ``{canonical_term: {language: [names], meta}}`` so
    downstream tools can consume it directly.  CSV format: one row per
    language name (wide enough to re-import into a spreadsheet).
    """
    config = config_from_args(args)
    terms = dbstore.load_terms_records(config.store_root)
    languages = [item.strip() for item in args.languages.split(",") if item.strip()]

    # Deterministic ordering: canonical, then trust, then weight.
    terms.sort(
        key=lambda term: (
            term.canonical or "",
            term.trust or "",
            term.weight,
        )
    )
    records = []
    for term in terms:
        names = {k: v for k, v in (term.names or {}).items() if v}
        filtered_names = {k: names[k] for k in languages if k in names}
        if args.tags:
            allowed = set(args.tags.split(","))
            if not (set(term.tags or []) & allowed):
                continue
        records.append(
            {
                "canonical": term.canonical,
                "source_language": term.source_language,
                "kind": term.kind,
                "tags": sorted(term.tags or []),
                "official": bool(term.official),
                "trust": term.trust,
                "weight": round(term.weight, 4),
                "occurrences": term.occurrences or len(term.evidence),
                "names": filtered_names,
            }
        )

    out_path = args.output
    if args.format == "csv":
        import csv
        import io

        buffer = io.StringIO()
        writer = csv.writer(buffer)
        writer.writerow(["canonical", "source_language", "kind", "tag", "official", "trust", "weight", "occurrences", "language", "name"])
        for record in records:
            tags = record["tags"] or [""]
            for tag in tags:
                for lang, name in (record["names"] or {}).items():
                    writer.writerow(
                        [
                            record["canonical"],
                            record["source_language"],
                            record["kind"],
                            tag,
                            int(record["official"]),
                            record["trust"],
                            record["weight"],
                            record["occurrences"],
                            lang,
                            name,
                        ]
                    )
        output_text = buffer.getvalue()
    else:
        output_text = json.dumps({"terms": records}, ensure_ascii=False, indent=2)

    if out_path:
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(output_text, encoding="utf-8")
        print(json.dumps({"exported": len(records), "path": str(out_path)}, ensure_ascii=False))
    else:
        print(output_text)
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


def cmd_terms_review(args: argparse.Namespace) -> int:
    """智能体干预环：导出待裁决队列 / 提交判断 / 查看方法论与复用率。

    设计上不要求用户提供 LLM API Key——主流 AI 订阅限制第三方应用接入，
    而使用 sekaisync 的编码智能体本身就有推理能力。sekaisync 只提供本地
    队列接口，判断由智能体在自己会话里完成，结果沉淀为方法论供后续复用。
    """
    from sekaisync import agent_review

    store = config_from_args(args).store_root
    action = args.review_action
    if action == "list":
        items = agent_review.load_queue(store, limit=args.limit, kind=args.kind)
        print(json.dumps(
            [item.__dict__ for item in items], ensure_ascii=False, indent=2))
        return 0
    if action == "export":
        out_path = Path(args.out) if args.out else Path("review_queue.txt")
        agent_review.export_for_agent(store, out_path, limit=args.limit or 20)
        print(json.dumps({"exported": str(out_path), "size": len(items) if (items := agent_review.load_queue(store)) else 0},
                         ensure_ascii=False, indent=2))
        return 0
    if action == "submit":
        if args.file:
            text = Path(args.file).read_text(encoding="utf-8")
        elif args.text:
            text = args.text
        else:
            print("ERROR: submit needs --file or --text", file=sys.stderr)
            return 2
        result = agent_review.import_judgments_from_text(store, text)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    if action == "stats":
        print(json.dumps(agent_review.review_stats(store), ensure_ascii=False, indent=2))
        return 0
    if action == "methodology":
        entries = agent_review.load_methodology(store)
        print(json.dumps(
            [e.__dict__ for e in entries[: args.limit or len(entries)]],
            ensure_ascii=False, indent=2))
        return 0
    print(f"ERROR: unknown review action {action}", file=sys.stderr)
    return 2


def cmd_terms_status(args: argparse.Namespace) -> int:
    config = config_from_args(args)
    path = terms_path(config.store_root)
    print(
        json.dumps(
            {
                "path": str(dbstore.db_file(config.store_root)),
                **dbstore.term_status_from_db(config.store_root),
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


def cmd_terms_penetrate(args: argparse.Namespace) -> int:
    config = config_from_args(args)
    core = SekaiSyncCore(config.store_root)
    languages = [s.strip() for s in args.languages.split(",") if s.strip()]
    result = core.term_penetrate(args.query, story_key=args.story_key, languages=languages)
    if result is None:
        print(json.dumps({"query": args.query, "error": "No matching term"}, ensure_ascii=False, indent=2))
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


def cmd_terms_zhfirst(args: argparse.Namespace) -> int:
    """简中主位管线：从 zh_hans 正文提取内容词，排除主角/代词/说话人，
    并做同点位四语对齐。输出 ZhFirstTerm 列表。"""
    config = config_from_args(args)
    from sekaisync.layout import glossary_path
    from sekaisync.termindex import load_pages, load_glossary
    from sekaisync.zhfirst import extract_terms_zhfirst

    pages = load_pages(config.store_root)
    glossary = load_glossary(glossary_path(config.store_root))
    targets = [s.strip() for s in args.languages.split(",") if s.strip() and s.strip() != "zh_hans"]
    llm = None
    if getattr(args, "llm_config", None):
        from sekaisync.llm_client import LLMClient, load_llm_config
        llm = LLMClient(load_llm_config(args.llm_config))
    result = extract_terms_zhfirst(
        pages,
        targets,
        glossary,
        min_freq=args.min_freq,
        cache_dir=config.store_root / "cache",
        do_align=getattr(args, "align", False),
        llm=llm,
    )
    # Cache output for merge-zhfirst
    cache_file = config.store_root / "cache" / "zhfirst_terms.json"
    cache_file.parent.mkdir(parents=True, exist_ok=True)
    cache_file.write_text(
        json.dumps([t.to_dict() for t in result], ensure_ascii=False),
        encoding="utf-8",
    )
    limit = args.limit or len(result)
    out = [t.to_dict() for t in result[:limit]]
    print(json.dumps({"count": len(result), "cached": str(cache_file), "terms": out}, ensure_ascii=False, indent=2))
    return 0


def cmd_terms_merge_zhfirst(args: argparse.Namespace) -> int:
    """将 zh-first 管线产出合并进 terms.json，补齐 ja-first 管线漏掉的简中本位词。"""
    config = config_from_args(args)
    core = SekaiSyncCore(config.store_root)
    result = core.merge_zhfirst_terms()
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


def cmd_tag_clouds(args: argparse.Namespace) -> int:
    config = config_from_args(args)
    core = SekaiSyncCore(config.store_root)
    result = core.tag_clouds()
    # Summary only by default; --full dumps ranked term lists
    if not getattr(args, "full", False):
        result = {
            "released": {"count": result["released"]["count"], "story_keys": result["released"]["story_keys"], "top": result["released"]["cloud"]["all"][:20]},
            "unreleased": {"count": result["unreleased"]["count"], "story_keys": result["unreleased"]["story_keys"], "note": result["unreleased"]["note"], "top": result["unreleased"]["cloud"]["all"][:20]},
        }
    print(json.dumps(result, ensure_ascii=False, indent=2))
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
    p_crawl.add_argument("--sv-tables", default=None, help="altsource_sv master tables, comma-separated (e.g. eventStories,cards); default: crawl story text via sitemap instead")
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
        default="jp,en,tc,kr,cn",
        help="Regions to fetch news for (jp/en/tc/kr/cn); ms provides cn+jp, sv provides all five",
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
        "--align",
        action="store_true",
        help="Run four-language same-position alignment after extraction (slow, ~20min on a full store)",
    )
    p_terms_extract.add_argument(
        "--local",
        action="store_true",
        help="Use deterministic local extraction instead of an LLM",
    )
    p_terms_extract.add_argument(
        "--layered",
        action="store_true",
        help="Use the trinity scrubber (trunk + hub + translit channels) instead of the flat pipeline",
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
    p_terms_lookup.add_argument("--tag", default=None, help="Filter by tag (person/location/organization/event/product/other)")
    p_terms_lookup.add_argument("--sort", default="score", choices=["score", "weight"], help="Sort order")
    p_terms_lookup.set_defaults(func=cmd_terms_lookup)

    p_terms_list = terms_sub.add_parser("list", help="List extracted terms")
    p_terms_list.add_argument("--kind", default=None)
    p_terms_list.add_argument("--tag", default=None, help="Filter by tag (person/location/organization/event/product/other)")
    p_terms_list.add_argument("--sort", default="canonical", choices=["canonical", "weight"], help="Sort order")
    p_terms_list.add_argument("--limit", type=int, default=100)
    p_terms_list.set_defaults(func=cmd_terms_list)

    p_terms_status = terms_sub.add_parser("status", help="Show term index status")
    p_terms_status.set_defaults(func=cmd_terms_status)

    p_terms_penetrate = terms_sub.add_parser("penetrate", help="Cross-language per-line penetration for a term at a story position")
    p_terms_penetrate.add_argument("--query", required=True, help="Term to penetrate (any language)")
    p_terms_penetrate.add_argument("--story-key", default=None, help="Story key like event:174:1; auto-picks most frequent if omitted")
    p_terms_penetrate.add_argument("--languages", default="ja,zh_hans,en,zh_tw,ko", help="Comma-separated target languages")
    p_terms_penetrate.set_defaults(func=cmd_terms_penetrate)

    p_terms_review = terms_sub.add_parser(
        "review",
        help="智能体干预环：导出待裁决队列、提交判断、查看方法论（不需要 API Key）",
    )
    p_terms_review.add_argument(
        "review_action",
        choices=["list", "export", "submit", "stats", "methodology"],
    )
    p_terms_review.add_argument("--limit", type=int, default=0)
    p_terms_review.add_argument("--kind", default=None)
    p_terms_review.add_argument("--out", default=None)
    p_terms_review.add_argument("--file", default=None)
    p_terms_review.add_argument("--text", default=None)
    p_terms_review.set_defaults(func=cmd_terms_review)

    p_terms_zhfirst = terms_sub.add_parser(
        "zhfirst",
        help="简中主位提取：排除主角/代词/说话人，同点位对齐四语",
    )
    p_terms_zhfirst.add_argument("--languages", default="ja,en,zh_tw,ko", help="Target languages for alignment")
    p_terms_zhfirst.add_argument("--min-freq", type=int, default=2, help="Minimum word frequency for discovery")
    p_terms_zhfirst.add_argument("--limit", type=int, default=0, help="Max terms to print (0=all)")
    p_terms_zhfirst.add_argument("--align", action="store_true", help="Run cross-language alignment (slow, ~20 min)")
    p_terms_zhfirst.add_argument("--llm-config", type=Path, default=None, help="LLM config path for semantic filtering of fragments")
    p_terms_zhfirst.set_defaults(func=cmd_terms_zhfirst)

    p_terms_merge = terms_sub.add_parser(
        "merge-zhfirst",
        help="Merge zh-first pipeline output (from cache) into terms.json",
    )
    p_terms_merge.set_defaults(func=cmd_terms_merge_zhfirst)

    p_terms_export = terms_sub.add_parser(
        "export",
        help="Deterministically export the term index as JSON or CSV (no LLM)",
    )
    p_terms_export.add_argument(
        "--output", type=Path, default=None, help="Output file path (default: stdout)"
    )
    p_terms_export.add_argument(
        "--format", choices=["json", "csv"], default="json", help="Export format"
    )
    p_terms_export.add_argument(
        "--languages",
        default="ja,zh_hans,zh_tw,en,ko",
        help="Comma-separated language keys to include in names",
    )
    p_terms_export.add_argument(
        "--tags",
        default=None,
        help="Comma-separated tag filter (person/location/organization/event/product/other)",
    )
    p_terms_export.set_defaults(func=cmd_terms_export)

    p_tag_clouds = sub.add_parser("tag-clouds", help="Tag clouds split by released(multi-lang) vs unreleased(ja-only)")
    p_tag_clouds.add_argument("--full", action="store_true", help="Dump full ranked lists per language")
    p_tag_clouds.set_defaults(func=cmd_tag_clouds)

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
    try:
        return args.func(args)
    except dbstore.SchemaVersionError as exc:
        # P07 gate: the store was not written by this build (newer/older/unknown
        # schema, or unreadable). Fail with an actionable message and a non-zero
        # code instead of a traceback — the store is left byte-identical.
        print(json.dumps(
            {
                "error": str(exc),
                "schema_status": exc.status,
                "schema_version_found": exc.found,
                "schema_version_supported": exc.supported,
                "store_db": str(exc.path) if exc.path else None,
            },
            ensure_ascii=False,
            indent=2,
        ))
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())









