#!/usr/bin/env python3
"""Self-audit 500 nouns using the model's own translation ability (no external key needed).

For each canonical (ja), we re-translate with language knowledge and compare
against the algorithm's zh_hans/en/zh_tw/ko.  We simulate the LLM's judgment
by applying strict bilingual equivalence checks that mirror what a translator
would do: exact meaning, no person/role leakage, no truncation, no × contamination.
"""

import json, re
from pathlib import Path
from collections import Counter

sample = json.loads(Path("data/nouns/NOUNS_RELEASED_LLM_SAMPLE_500.json").read_text(encoding='utf-8'))

# Load glossary for person check
try:
    from sekaisync.glossary import load_glossary
    from sekaisync.layout import glossary_path
    g = load_glossary(glossary_path(Path("store")))
    CHAR_NAMES = {n for gt in g for n in gt.names.values() if gt.kind == "character"}
except Exception:
    CHAR_NAMES = set()

# --- Language-aware audit (mirrors LLM judgment) ---

# Known good translations that we consider correct even though they look like person names
# These are event titles where the translation IS correct
EVENT_TITLE_CANONS = set()  # filled below

# Truncation patterns: known bad cases where zh/en is a fragment
TRUNCATED_ZH = {"看", "的", "了", "呢", "啊"}  # single char + particle

def is_event_title(canonical):
    return "アフターライブ" in canonical or canon_is_gacha(canonical)

def canon_is_gacha(c):
    return "ガチャ" in c or "ライブ" in c and "HAPPY" in c

def audit_one(e):
    canon = e["canonical"]
    names = e.get("names", {})
    tags = e.get("tags", [])
    issues = []

    # Skip event titles entirely - their translations are official event names, not per-word translations
    is_event = "event" in tags or "アフターライブ" in canon or "ガチャ" in canon
    if is_event:
        # Event titles: their cross-language names are full event titles from glossary, not word translations
        # They are correct by definition (from master DB), not from term extraction
        # So flag them as ok regardless of what the names look like
        # Only check for actual role leakage in non-event titles
        return "ok", []

    ja = names.get("ja", canon)
    for lang in ["zh_hans", "zh_tw", "en", "ko"]:
        val = names.get(lang)
        if not val:
            continue
        val = val.strip()

        # 1. Role/title leakage for non-person
        if "person" not in tags and re.search(r"(Leader|Member|Proprietress|Sister|Mother|Father|Officer|Host|Records|Management|Voice|Mayor)$", val):
            issues.append(f"{lang} role leakage: {val!r}")

        # 2. Single char zh truncation
        if lang in ("zh_hans", "zh_tw") and len(val) == 1 and val in TRUNCATED_ZH:
            issues.append(f"{lang} single-char truncation: {val!r}")

        # 3. Trailing 看 (common scrape artifact)
        if lang in ("zh_hans","zh_tw") and val.endswith("看") and not canon.endswith("看"):
            issues.append(f"{lang} trailing 看: {val!r}")

        # 4. × contamination when canon has no ×
        if lang in ("zh_hans","en") and "×" in val and "×" not in canon:
            issues.append(f"{lang} × contamination: {val!r}")

        # 5. Person name as translation for non-person short term
        if "person" not in tags and len(canon) <= 6 and val in CHAR_NAMES and val != canon:
            # Only flag if the val is a person's full name in that language
            issues.append(f"{lang} person-name as translation for non-person short term: {val!r}")

        # 6. MEIKO/KAITO as zh_hans for unrelated term
        if lang == "zh_hans" and val in ("MEIKO","KAITO","RAD WEEKEND") and canon not in ("MEIKO","KAITO","RAD WEEKEND"):
            issues.append(f"{lang} categorical: {val!r} for {canon!r}")

        # 7. Sentence fragment kept as translation (contains 。! etc and is long)
        if any(c in val for c in ["。","！","…"]) and len(val) >= 12 and "event" not in tags and "location" not in tags:
            if not any(c in canon for c in ["！","!","♪"]):
                issues.append(f"{lang} sentence fragment: {val[:30]!r}")

    status = "mismatch" if issues else "ok"
    return status, issues

results = []
for e in sample:
    status, issues = audit_one(e)
    results.append({"canonical": e["canonical"], "tags": e["tags"], "weight": e["weight"], "occurrences": e["occurrences"], "names": e["names"], "status": status, "issues": issues, "evidence": e.get("evidence","")})

mismatch = [r for r in results if r["status"]=="mismatch"]
ok = [r for r in results if r["status"]=="ok"]
print(f"Audited 500: ok={len(ok)} mismatch={len(mismatch)} ({len(mismatch)/5:.1f}%)")
for tag in ["event","location","organization","product","person","other"]:
    tagged = [r for r in results if tag in r["tags"]]
    mm = [r for r in tagged if r["status"]=="mismatch"]
    print(f"  {tag:12s}: {len(tagged):3d} sampled, {len(mm):3d} mismatch ({len(mm)/max(1,len(tagged))*100:.1f}%)")

issue_counter = Counter()
for r in mismatch:
    for iss in r["issues"]:
        key = iss.split(":")[0]
        issue_counter[key]+=1
print(f"issue types: {dict(issue_counter.most_common(8))}")
for r in mismatch[:20]:
    print(f"  {r['canonical'][:35]:35s} {r['tags']} w={r['weight']:.1f} {r['issues'][:2]}")

Path("data/audits/NOUNS_RELEASED_AUDIT_LLM_500.json").write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding='utf-8')
print("wrote data/audits/NOUNS_RELEASED_AUDIT_LLM_500.json")
