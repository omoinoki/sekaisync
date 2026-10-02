"""Private persisted identity and exact body provenance for UI wording values."""
from __future__ import annotations

from copy import deepcopy
import hashlib
import json
import re
import unicodedata

from sekaisync.config import REGIONS


_SCHEMA = "sekaisync/private-wording-identity@2"
_POLICY = "whole-parsed-wording-value@1"
_VIEW_SCHEMA = "sekaisync/private-wording-body@1"


def _digest(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _record_hash(record):
    cleaned = {key: value for key, value in record.items() if not key.startswith("__")}
    return _digest(json.dumps(cleaned, sort_keys=True, ensure_ascii=False))


def _validate_records(records, region):
    seen = set()
    for record in records:
        if not isinstance(record, dict):
            raise ValueError("wording debt: record must be an object")
        key, value = record.get("wordingKey"), record.get("value")
        if not isinstance(key, str) or not key.strip():
            raise ValueError("wording debt: missing/empty/non-string wordingKey")
        if value is not None and not isinstance(value, str):
            raise ValueError("wording debt: non-string/non-null value")
        if key in seen:
            raise ValueError("wording debt: duplicate key in " + str(region) + ": " + key)
        seen.add(key)


def _value_state(record):
    value = record.get("value")
    body = value if isinstance(value, str) else ""
    reason = ("missing_value" if "value" not in record else "explicit_null" if value is None
              else "blank_value" if not value.strip() else "")
    version = _digest(json.dumps(dict(present="value" in record, value=value), sort_keys=True, ensure_ascii=False))
    return body, reason, version


def _version_key(page):
    metadata = _metadata(page)
    if metadata is None:
        raise ValueError("wording content version requires persisted private identity")
    return page["id"] + ":wording-value-sha256:" + metadata["value_version_sha256"]


def _metadata(page):
    identity, provenance = page.get("wording_identity"), page.get("wording_provenance")
    if "wording_identity" not in page and "wording_provenance" not in page:
        return None
    if page.get("kind") != "wordings" or not isinstance(identity, dict) or not isinstance(provenance, dict):
        raise ValueError("wording debt: private metadata is partial or belongs to another domain")
    key, region = identity.get("key"), identity.get("region")
    record = provenance.get("original_record")
    if not isinstance(record, dict) or (record.get("value") is not None and not isinstance(record.get("value"), str)):
        raise ValueError("wording debt: persisted original value is not a string/null/debt")
    body, reason, version = _value_state(record)
    if (identity.get("schema") != _SCHEMA or identity.get("body_policy") != _POLICY
            or not isinstance(key, str) or not key.strip() or not isinstance(region, str) or region not in REGIONS
            or identity.get("family") != "wordings:key-sha256:" + _digest(key)
            or identity.get("legacy_id") != page.get("id")
            or identity.get("source") != page.get("source")
            or identity.get("language") != page.get("language")
            or identity.get("language") != REGIONS[region].language
            or page.get("canonical_key") != identity.get("family")
            or record.get("wordingKey") != key
            or _digest(body) != identity.get("value_sha256")
            or version != identity.get("value_version_sha256")
            or identity.get("no_expression") is not bool(reason)
            or identity.get("no_expression_reason") != reason
            or _record_hash(record) != identity.get("record_sha256")
            or provenance.get("original_record_sha256") != identity.get("record_sha256")
            or provenance.get("parsed_value_sha256") != identity.get("value_sha256")
            or page.get("source_hash") != identity.get("record_sha256")
            or ("text_hash" in page and page["text_hash"] != identity.get("value_sha256"))
            or ("text" in page and page["text"] != body)
            or (bool(reason) and not page.get("untranslated"))):
        raise ValueError("wording debt: persisted identity or raw-record provenance changed")
    token, mapping = _token_map(record.get("value"))
    if (provenance.get("adapter_value_token") != (token if "value" in record else None)
            or provenance.get("decoded_unicode_to_token_unicode") != (mapping if "value" in record else [])
            or provenance.get("token_provenance") != "adapter serialization, not original master-file token"
            or provenance.get("offset_unit") != "Unicode code points; end-exclusive"):
        raise ValueError("wording debt: persisted reversible value-token provenance changed")
    if not str(page.get("id", "")).startswith("web:" + str(page.get("source", "")) + ":" + region + ":wordings:"):
        raise ValueError("wording debt: persisted region/legacy ID binding changed")
    return identity


def _allows_native_ja_han_ui(page, language):
    """Allow native JP Han-only UI bodies, not inferred overseas localization."""
    if language != "ja" or page.get("language") != "ja" or page.get("kind") != "wordings":
        return False
    try:
        identity = _metadata(page)
    except ValueError:
        return False
    if identity is None or identity["region"] != "jp" or identity["no_expression"]:
        return False
    body = page.get("text")
    if not isinstance(body, str):
        return False
    # Han is shared with Chinese; validated native JP provenance supplies the
    # domain evidence that the ordinary dialogue script heuristic cannot use.
    return (len(re.findall(r"[\u3400-\u4dbf\u4e00-\u9fff]", body)) > 4
            and not re.search(r"[\u3040-\u309f\u30a1-\u30fa\u30fd-\u30ff]", body)
            and not re.search(r"[\u1100-\u11ff\u3130-\u318f\ua960-\ua97f\uac00-\ud7ff]", body)
            and all(not char.isalpha() or "\u3400" <= char <= "\u4dbf" or "\u4e00" <= char <= "\u9fff"
                    or unicodedata.name(char, "").startswith(("LATIN ", "FULLWIDTH LATIN "))
                    for char in body))


def _token_map(value):
    if value is None:
        return "null", []
    token = json.dumps(value, ensure_ascii=True)
    mapping, cursor = [], 1
    while cursor < len(token) - 1:
        start = cursor
        if token[cursor] != "\\":
            cursor += 1
        elif token[cursor + 1] != "u":
            cursor += 2
        else:
            code = int(token[cursor + 2:cursor + 6], 16)
            cursor += 6
            if 0xD800 <= code <= 0xDBFF:
                cursor += 6
        mapping.append(dict(start=start, end=cursor, exact=json.loads('"' + token[start:cursor] + '"')))
    return token, mapping


def _adapt(page, record, region, legacy_id=None):
    _validate_records([record], region)
    if region not in REGIONS or page.language != REGIONS[region].language or page.kind != "wordings":
        raise ValueError("wording debt: adapter domain/region binding changed")
    value, key = record.get("value"), record["wordingKey"]
    body, reason, version = _value_state(record)
    if page.source_hash != _record_hash(record):
        raise ValueError("wording debt: adapter source record hash changed")
    if legacy_id is not None:
        page.id = legacy_id
    family = "wordings:key-sha256:" + _digest(key)
    page.text, page.canonical_key = body, family
    page.hash, page.text_hash = hashlib.sha1(body.encode("utf-8")).hexdigest()[:16], _digest(body)
    if reason:
        page.untranslated = True
    page.wording_identity = dict(schema=_SCHEMA, key=key, region=region, family=family, body_policy=_POLICY,
        legacy_id=page.id, source=page.source, language=page.language,
        record_sha256=_record_hash(record), value_sha256=_digest(body), value_version_sha256=version,
        no_expression=bool(reason), no_expression_reason=reason)
    token, mapping = _token_map(value)
    page.wording_provenance = dict(original_record=deepcopy(record), original_record_sha256=_record_hash(record),
        parsed_value_sha256=_digest(body), adapter_value_token=token if "value" in record else None,
        decoded_unicode_to_token_unicode=mapping if "value" in record else [],
        token_provenance="adapter serialization, not original master-file token",
        offset_unit="Unicode code points; end-exclusive")
    _metadata(vars(page))
    return page


def _legacy_record(page):
    if page.get("kind") != "wordings" or _metadata(page) is not None:
        return None
    try:
        record = json.loads(page.get("text") or "")
    except (TypeError, json.JSONDecodeError) as error:
        raise ValueError("wording debt: legacy body is not an original JSON record") from error
    _validate_records([record], "legacy")
    if _record_hash(record) != page.get("source_hash"):
        raise ValueError("wording debt: legacy original record source hash changed")
    prefix = "web:" + str(page.get("source", "")) + ":"
    suffix = str(page.get("id", "")).removeprefix(prefix)
    region, separator, tail = suffix.partition(":wordings:")
    text = json.dumps(record, ensure_ascii=False, indent=2)
    record_id = record.get("id", record.get("seq", hashlib.sha1(text.encode("utf-8")).hexdigest()[:12]))
    if (not str(page.get("id", "")).startswith(prefix) or not separator or region not in REGIONS
            or page.get("language") != REGIONS[region].language or tail != str(record_id)):
        raise ValueError("wording debt: legacy original record/ID/region binding changed")
    return record


def _resume_index(existing_pages, source):
    result = {}
    for page in existing_pages.values():
        if page.get("source") != source or page.get("kind") != "wordings":
            continue
        identity = _metadata(page)
        if identity is not None:
            key = (identity["region"], identity["key"])
        else:
            record = _legacy_record(page)
            region = str(page["id"])[len("web:" + source + ":"):].partition(":wordings:")[0]
            key = (region, record["wordingKey"])
        if key in result:
            raise ValueError("wording debt: ambiguous persisted structural key/region")
        result[key] = page["id"]
    return result


def _view_metadata(page):
    identity = _metadata(page)
    if identity is None or identity["no_expression"]:
        return {}
    raw = page.get("text")
    if not isinstance(raw, str):
        raise ValueError("wording body policy requires the complete raw value")
    return dict(wording_body=dict(schema=_VIEW_SCHEMA, policy=_POLICY, family=identity["family"],
        source=page["source"], page_id=page["id"], language=page["language"],
        page_sha256=_digest(raw), page_code_points=len(raw)))


def _view_policy(view):
    marker = view.get("wording_body")
    if "wording_body" not in view:
        return False
    if not isinstance(marker, dict):
        raise ValueError("wording body marker must be an object")
    start, end, text = view.get("start"), view.get("end"), view.get("text")
    length = marker.get("page_code_points")
    if (set(marker) != {"schema", "policy", "family", "source", "page_id", "language", "page_sha256", "page_code_points"}
            or marker.get("schema") != _VIEW_SCHEMA or marker.get("policy") != _POLICY
            or marker.get("source") != view.get("source") or marker.get("page_id") != view.get("page_id")
            or marker.get("language") != view.get("language") or marker.get("page_sha256") != view.get("sha256")
            or not isinstance(marker.get("family"), str) or not marker["family"].startswith("wordings:key-sha256:")
            or type(length) is not int or type(start) is not int or type(end) is not int
            or not isinstance(text, str) or not 0 <= start < end <= length or end - start != len(text)
            or (start == 0 and end == length and _digest(text) != marker["page_sha256"])):
        raise ValueError("wording body marker binding or raw geometry changed")
    return True


def _persisted_page(conn, source, page_id, cache=None):
    from sekaisync import dbstore
    key = (source, page_id)
    if cache is not None and key in cache:
        page = cache[key]
    else:
        cursor = conn.execute("SELECT * FROM web_pages WHERE source=? AND id=?", key)
        row = cursor.fetchone()
        if row is None:
            raise ValueError("wording replay page absent from store")
        columns = [column[0] for column in cursor.description]
        page = dbstore._row_tuple_to_dict(columns, dict(zip(columns, row)))
        if cache is not None:
            cache[key] = page
    _metadata(page)
    return page


def _full_view(page):
    raw = str(page.get("text") or "")
    return dict(source=page["source"], page_id=page["id"], language=page["language"], sha256=_digest(raw),
        start=0, end=len(raw), text=raw, complete=True, **_view_metadata(page))


def _validate_view_metadata(view, page):
    carried = {"wording_body": view["wording_body"]} if "wording_body" in view else {}
    if carried != _view_metadata(page):
        raise ValueError("wording body domain marker differs from current persisted page")
    _view_policy(view)
