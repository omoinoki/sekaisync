from __future__ import annotations

import json
import shutil
import tarfile
import tempfile
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Optional

from sekaisync.config import REGIONS, SekaiSyncConfig
from sekaisync.layout import (
    factpack_path,
    freshness_path,
    glossary_path,
    registry_path,
    region_source_dir,
    seed_glossary_path,
)
from sekaisync.coverage import build_region_coverage, build_source_manifest
from sekaisync.factpacks import build_fact_packs, save_fact_packs
from sekaisync.glossary import merge_glossary, save_glossary
from sekaisync.registry import build_registry, data_files_for_region, save_registry


USER_AGENT = "SekaiSync/0.1 (+local knowledge sync)"


def download_file(url: str, dest: Path, timeout: int = 60) -> Path:
    dest.parent.mkdir(parents=True, exist_ok=True)
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        dest.write_bytes(response.read())
    return dest


def extract_tarball(tarball: Path, dest: Path) -> Path:
    dest.mkdir(parents=True, exist_ok=True)
    with tarfile.open(tarball, "r:gz") as archive:
        members = []
        for member in archive.getmembers():
            member_path = Path(member.name)
            if member_path.is_absolute() or ".." in member_path.parts:
                continue
            members.append(member)
        archive.extractall(dest, members=members)
    return dest


def _staging_dir(source_dir: Path) -> Path:
    """A same-filesystem staging directory beside ``source_dir``."""
    return source_dir.parent / f".{source_dir.name}.staging"


def _commit_staging(staging: Path, target: Path) -> None:
    """Replace ``target`` with ``staging``, restoring the old data on failure."""
    backup = target.parent / f".{target.name}.bak"
    if backup.exists():
        shutil.rmtree(backup, ignore_errors=True)
    moved_old = False
    if target.exists():
        target.rename(backup)
        moved_old = True
    try:
        staging.rename(target)
    except Exception:
        if moved_old and backup.exists():
            backup.rename(target)
        raise
    if moved_old:
        shutil.rmtree(backup, ignore_errors=True)


def fetch_region_from_tarball(region_key: str, config: SekaiSyncConfig) -> Path:
    region = REGIONS[region_key]
    repo = region.repo_slug
    if not repo:
        raise ValueError(f"Region {region_key} has no GitHub repository configured")
    url = f"{config.github_tarball_base}/{repo}/archive/refs/heads/main.tar.gz"
    source_dir = region_source_dir(config.store_root, region_key)
    source_dir.parent.mkdir(parents=True, exist_ok=True)
    staging = _staging_dir(source_dir)
    if staging.exists():
        shutil.rmtree(staging, ignore_errors=True)
    staging.mkdir(parents=True, exist_ok=True)
    try:
        with tempfile.TemporaryDirectory() as tmp:
            tarball = Path(tmp) / "master.tar.gz"
            download_file(url, tarball)
            extract_tarball(tarball, staging)
        _commit_staging(staging, source_dir)
    except Exception:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    return source_dir


def fetch_region_from_local(region_key: str, local_mirror: Path, config: SekaiSyncConfig) -> Path:
    if not local_mirror.exists():
        raise FileNotFoundError(f"Local mirror not found: {local_mirror}")
    source_dir = region_source_dir(config.store_root, region_key)
    source_dir.parent.mkdir(parents=True, exist_ok=True)
    staging = _staging_dir(source_dir)
    if staging.exists():
        shutil.rmtree(staging, ignore_errors=True)
    staging.mkdir(parents=True, exist_ok=True)
    try:
        shutil.copytree(local_mirror, staging, dirs_exist_ok=True)
        _commit_staging(staging, source_dir)
    except Exception:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    return source_dir


def fetch_region(
    region_key: str,
    config: SekaiSyncConfig,
    local_mirror: Optional[Path] = None,
) -> Path:
    if local_mirror is not None:
        return fetch_region_from_local(region_key, local_mirror, config)
    return fetch_region_from_tarball(region_key, config)


def write_freshness(
    config: SekaiSyncConfig,
    regions: Iterable[str],
    web_status: Optional[dict] = None,
    news_available: bool = False,
) -> Path:
    region_info = {}
    for region in regions:
        if region == "demo":
            region_info[region] = {
                "language": "zh_hans",
                "official_translation": False,
                "launch_date": None,
                "source": "demo",
            }
        else:
            region_info[region] = {
                "language": REGIONS[region].language,
                "official_translation": REGIONS[region].official_translation,
                "launch_date": REGIONS[region].launch_date,
                "source": f"master_db:{region}",
            }
    master_available = {
        region: region == "demo" or bool(data_files_for_region(config.store_root, region))
        for region in regions
    }
    freshness = {
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "regions": region_info,
        "coverage": build_region_coverage(
            regions,
            web_status=web_status,
            news_available=news_available,
            master_available=master_available,
        ),
        "sources": build_source_manifest(config.sites),
        "web": web_status or {
            "enabled": False,
            "consent": False,
            "sources": {},
        },
    }
    path = freshness_path(config.store_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(freshness, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def sync(
    config: SekaiSyncConfig,
    regions: Iterable[str],
    local_mirrors: Optional[dict[str, Path]] = None,
) -> dict:
    regions = tuple(regions)
    local_mirrors = local_mirrors or {}
    for region_key in regions:
        if region_key == "demo":
            continue
        fetch_region(region_key, config, local_mirror=local_mirrors.get(region_key))

    all_regions: list[str] = []
    for region_key in regions:
        if region_key not in all_regions:
            all_regions.append(region_key)
    for region_key in config.regions:
        if region_key in all_regions:
            continue
        if region_key == "demo" or data_files_for_region(config.store_root, region_key):
            all_regions.append(region_key)

    entities = build_registry(config.store_root, all_regions)
    registry_file = registry_path(config.store_root)
    save_registry(entities, registry_file)

    seed_path = seed_glossary_path(config.store_root)
    seed = []
    if seed_path.exists():
        seed = json.loads(seed_path.read_text(encoding="utf-8"))
    terms = merge_glossary(entities, seed)
    glossary_file = glossary_path(config.store_root)
    save_glossary(terms, glossary_file)

    for language in ("ja", "en", "zh_tw", "zh_hans", "ko"):
        packs = build_fact_packs(entities, language=language)
        save_fact_packs(packs, factpack_path(config.store_root, language))

    write_freshness(config, all_regions)
    return {
        "regions": all_regions,
        "entities": len(entities),
        "terms": len(terms),
        "coverage": build_region_coverage(all_regions),
        "registry": str(registry_file),
        "glossary": str(glossary_file),
    }
