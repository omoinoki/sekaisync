"""Lossless region facts and conservative views (Astra P03).

These helpers do not read or write the store. Old aggregate facts are evidence
with unknown coverage, never proof that a region has a particular value.
"""
from __future__ import annotations

import json
from copy import deepcopy
from dataclasses import asdict
from typing import Any, Iterable, Optional

from sekaisync.models import Entity, RegionFacts


def _same_value(left: Any, right: Any) -> bool:
    # JSON types matter: Python alone considers True == 1 == 1.0.
    return json.dumps(left, sort_keys=True, ensure_ascii=False) == json.dumps(
        right, sort_keys=True, ensure_ascii=False)


def project_common_facts(
    region_facts: dict[str, RegionFacts],
    regions: Optional[Iterable[str]] = None,
) -> tuple[dict[str, Any], dict[str, str]]:
    """Return common facts and field states, requiring full entity coverage.

    ``regions`` is the entity's known region set, not every deployed server.
    A missing RegionFacts slot prevents a purported common projection.
    """
    covered = sorted(set(regions or ()) | set(region_facts))
    facts, status = {}, {}
    for field in sorted({key for rf in region_facts.values() for key in rf.facts}):
        values = [region_facts[r].facts[field] for r in covered
                  if r in region_facts and field in region_facts[r].facts]
        if len(values) != len(covered):
            status[field] = 'partial'
        elif all(_same_value(values[0], value) for value in values[1:]):
            facts[field] = deepcopy(values[0])
            status[field] = 'available'
        else:
            status[field] = 'needs_region'
    return facts, status


def region_facts_to_dict(entity: Entity) -> dict[str, dict]:
    """JSON-safe, detached provenance for JSON or the v3 DB serializer."""
    return {name: asdict(rf) for name, rf in sorted(entity.region_facts.items())}


def region_facts_from_dict(data: Optional[dict]) -> dict[str, RegionFacts]:
    """Load provenance; absence means legacy/unknown, malformed data is rejected."""
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise ValueError('region_facts must be an object')
    out = {}
    for name, item in sorted(data.items()):
        if (not isinstance(item, dict) or item.get('region', name) != name
                or not isinstance(item.get('facts'), dict)
                or not isinstance(item.get('retrieval', {}), dict)):
            raise ValueError(f'invalid region facts for {name!r}')
        out[name] = RegionFacts(name, deepcopy(item['facts']),
                                str(item.get('source', '')), item.get('version'),
                                deepcopy(item.get('retrieval', {})))
    return out


def entity_for_region(
    entity: Entity, region: Optional[str] = None, *,
    fields: Optional[Iterable[str]] = None,
) -> dict:
    """Select exact regional facts, or a full-coverage common projection.

    ``coverage`` is available/missing/unknown/needs_region. ``field_status``
    distinguishes missing fields from disagreements and partial coverage.
    With ``fields``, only those fact fields and their statuses are returned.
    Unscoped views always carry each region's provenance, even for common facts.
    """
    wanted = sorted(set(fields)) if fields is not None else None
    view = dict(id=entity.id, region=region, coverage='unknown', needs_region=False,
                facts={}, field_status={}, source=None, version=None, retrieval=None)
    if not entity.region_facts:
        view['legacy_unscoped'] = dict(facts=deepcopy(entity.facts),
                                      source=entity.source, version=entity.version,
                                      retrieval={}, coverage='unknown')
        view['field_status'] = {key: 'unknown' for key in (wanted or entity.facts)}
        return view

    if region is not None:
        rf = entity.region_facts.get(region)
        if rf is None:
            view['coverage'] = 'missing'
            view['field_status'] = {key: 'missing' for key in (wanted or [])}
            return view
        facts = deepcopy(rf.facts)
        status = {key: 'available' for key in facts}
        view.update(coverage='available', source=rf.source, version=rf.version,
                    retrieval=deepcopy(rf.retrieval))
    else:
        facts, status = project_common_facts(entity.region_facts, entity.regions)
        view['region_facts'] = region_facts_to_dict(entity)
        view['coverage'] = 'available'
        if set(entity.regions) - set(entity.region_facts):
            view['coverage'] = 'unknown'
    if wanted is not None:
        facts = {key: facts[key] for key in wanted if key in facts}
        status = {key: status.get(key, 'missing') for key in wanted}
    needs_region = region is None and any(
        value in ('partial', 'needs_region') for value in status.values())
    view.update(facts=facts, field_status=status, needs_region=needs_region)
    if needs_region:
        view['coverage'] = 'needs_region'
    return view
