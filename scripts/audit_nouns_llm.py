#!/usr/bin/env python3
"""Legacy heuristic audit of released nouns; this script makes no LLM calls.

Its historical filename is preserved. Rules and dictionary checks are not a
substitute for an agent reading aligned context or independent bilingual gold.
For host-agent packet evaluation use scripts/evaluate_agent_review.py.
"""

import json, re
from pathlib import Path
from collections import Counter, defaultdict

sample_path = Path("data/nouns/NOUNS_RELEASED_SAMPLE_500.json")
if not sample_path.exists():
    import random
    released = json.loads(Path("data/nouns/NOUNS_RELEASED.json").read_text(encoding='utf-8'))
    random.seed(42)
    sample = random.sample(released, 500)
    sample_path.write_text(json.dumps(sample, ensure_ascii=False, indent=2), encoding='utf-8')
else:
    sample = json.loads(sample_path.read_text(encoding='utf-8'))

# Also load glossary character names for person contamination check
try:
    from sekaisync.glossary import load_glossary
    from sekaisync.layout import glossary_path
    g = load_glossary(glossary_path(Path("store")))
    CHAR_NAMES = set()
    for gt in g:
        for n in gt.names.values():
            CHAR_NAMES.add(n)
            CHAR_NAMES.add(n.strip())
except Exception:
    CHAR_NAMES = set()

# --- Heuristic audit rules that capture obvious mismatches ---

def audit_entry(e):
    issues = []
    canon = e["canonical"]
    names = e.get("names", {})
    tags = e.get("tags", [])
    ja = names.get("ja", canon)

    # Only check languages that are present (non-ja may be missing for curated ja-only)
    for lang in ["zh_hans","zh_tw","en","ko"]:
        val = names.get(lang)
        if not val:
            continue
        val_stripped = val.strip()
        # 1. Truncation: ja is longer than translation suspiciously short (e.g. 莱利梦幻乐园 -> 莱利)
        # But for this sample most are long, so check trailing particle residue
        if val_stripped.endswith("看") and not canon.endswith("看"):
            issues.append(f"{lang} trailing 看: {val_stripped!r}")
        if val_stripped.endswith("的") and len(val_stripped) <= 3:
            issues.append(f"{lang} single 的 truncation: {val_stripped!r}")
        # 2. Cross-field swap: zh_hans contains ×/THE CENTER etc that belongs to different term
        if "×" in val_stripped and "×" not in canon and lang in ("zh_hans","en"):
            issues.append(f"{lang} has × despite canon no ×: {val_stripped!r}")
        if val_stripped in ("THE CENTER","THEATRE") and canon not in ("THE CENTER","THEATRE"):
            issues.append(f"{lang} THE CENTER contamination: {val_stripped!r}")
        # 3. Person name contamination for non-person
        if "person" not in tags and val_stripped in CHAR_NAMES and val_stripped != canon:
            # Check if val is a full character name
            if len(val_stripped) >= 3 and val_stripped in CHAR_NAMES:
                issues.append(f"{lang} person-name contamination for non-person: {val_stripped!r}")
        # 4. Role suffix in en for non-person
        if lang == "en" and re.search(r"(Leader|Member|Proprietress|Sister|Mother|Father|Officer|Host|Records|Management|Voice|Mayor|Prince|Princess)$", val_stripped):
            if "person" not in tags and canon not in (val_stripped,):
                issues.append(f"en role suffix pollution: {val_stripped!r}")
        # 5. Sentence fragment: contains 。 or ！且 length > 15 (whole utterance kept as term name)
        if any(c in val_stripped for c in ["。","！","!","♪"]) and len(val_stripped) >= 12:
            # But check if canon itself has those - then it's ok
            if not any(c in canon for c in ["！","!","♪"]):
                issues.append(f"{lang} sentence fragment kept: {val_stripped!r}")
        # 6. zh_hans that is clearly a different word: e.g. canon is バンド but zh is MEIKO (from earlier pollution)
        # Detect by: zh_hans is 2-4 kana-like romanization of a different character
        if lang == "zh_hans" and val_stripped in ("MEIKO","KAITO","RAD WEEKEND") and canon not in ("MEIKO","KAITO","RAD WEEKEND"):
            issues.append(f"zh_hans categorical mismatch: {val_stripped!r}")

    # 7. Tag vs. content sanity
    if "person" in tags and len(canon) <= 2 and e.get("occurrences",0) < 3:
        issues.append(f"person tag but single short occ={e.get('occurrences')}")

    status = "mismatch" if issues else "ok"
    return status, issues

results = []
for e in sample:
    status, issues = audit_entry(e)
    results.append({"canonical": e["canonical"], "tags": e["tags"], "weight": e["weight"], "occurrences": e["occurrences"], "names": e["names"], "status": status, "issues": issues})

mismatch = [r for r in results if r["status"]=="mismatch"]
ok = [r for r in results if r["status"]=="ok"]
print(f"Audited 500: ok={len(ok)} mismatch={len(mismatch)} ({len(mismatch)/5:.1f}%)")
# By tag
for tag in ["event","location","organization","product","person","other"]:
    tagged = [r for r in results if tag in r["tags"]]
    mm = [r for r in tagged if r["status"]=="mismatch"]
    print(f"  {tag}: {len(tagged)} sampled, {len(mm)} mismatch ({len(mm)/max(1,len(tagged))*100:.1f}%)")
# Top issues
issue_counter = Counter()
for r in mismatch:
    for iss in r["issues"]:
        key = iss.split(":")[0]
        issue_counter[key]+=1
print(f"issue types: {dict(issue_counter.most_common(10))}")
for r in mismatch[:20]:
    print(f"  {r['canonical'][:30]:30s} {r['tags']} w={r['weight']:.2f} -> {r['issues']}")

# Save
Path("data/audits/NOUNS_RELEASED_AUDIT_500.json").write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding='utf-8')
print("wrote data/audits/NOUNS_RELEASED_AUDIT_500.json")
