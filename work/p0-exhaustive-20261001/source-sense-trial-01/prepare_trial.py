"""Work-only closure-gated source-first trial stages; no public command changes."""
from __future__ import annotations

import argparse
from contextlib import closing
from copy import deepcopy
from datetime import datetime
import hashlib
import json
from pathlib import Path
import re
import sqlite3
import subprocess
import sys

ROOT=Path(__file__).resolve().parents[3]
sys.path.insert(0,str(ROOT))
from scripts import census_scraper_corpus as census
from sekaisync import agent_packets as packets,agent_review as review,dbstore,occurrence_store as ledger,span_subjects,termindex

BASE=Path(__file__).resolve().parent
DESIGN=BASE.parent / "source-sense-design-01"
LANGS=("en","ja","ko","zh_hans","zh_tw")
RAW={"en":"en","ja":"ja","ko":"ko","zh_hans":"zh_hans","zh_tw":"zh_hant"}
SEED="p0-source-first-next01|"
HASH=re.compile(r"[0-9a-f]{64}")


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def digest(value):
    return hashlib.sha256(json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(",",":"),allow_nan=False).encode()).hexdigest()


def freeze(path,value):
    path=Path(path)
    path.parent.mkdir(parents=True,exist_ok=True)
    with path.open("x",encoding="utf-8") as stream:
        stream.write(json.dumps(value,ensure_ascii=False,indent=2)+"\n")
    return sha(path)


def work_path(path):
    path=Path(path).resolve()
    if not path.is_relative_to(ROOT / "work"):
        raise ValueError("trial paths must stay in the workspace work tree")
    return path


def new_stage(output,name):
    output=work_path(output)
    path=output/name
    if path.exists():
        raise FileExistsError("preserve original/partial stage; choose a new trial directory: "+name)
    return path


def product_hashes(root=ROOT,registered=None):
    if registered is not None:
        if not isinstance(registered,dict) or not registered:
            raise ValueError("product closure must bind nonempty code inputs")
        result={}
        for name in registered:
            path=(root/Path(name)).resolve()
            if not path.is_relative_to(root) or path.is_relative_to(root/"work"):
                raise ValueError("registered product code path escapes product tree")
            result[name]=sha(path)
        return result
    paths=[]
    for directory in ("sekaisync","scripts","tests"):
        paths.extend(sorted((root/directory).rglob("*.py")))
    paths.extend(sorted((root/"agents").rglob("*.md")))
    if (root/"pyproject.toml").is_file():
        paths.append(root/"pyproject.toml")
    if not paths:
        raise ValueError("product closure must bind nonempty code inputs")
    return {str(path.resolve()):sha(path) for path in paths}


def verify_design():
    seal=json.loads((DESIGN/"completion-seal-01.json").read_bytes())
    for name,expected in seal["files"].items():
        if sha(DESIGN/name)!=expected:
            raise ValueError("original source-first design seal drift: "+name)
    addendum=DESIGN/"PROSPECTIVE-ADDENDUM-01.json"
    if sha(addendum)!=(DESIGN/"PROSPECTIVE-ADDENDUM-01.json.sha256").read_text().strip():
        raise ValueError("prospective addendum changed")
    isolation=BASE/"PROSPECTIVE-ADDENDUM-02.json"
    if sha(isolation)!=(BASE/"PROSPECTIVE-ADDENDUM-02.json.sha256").read_text().strip():
        raise ValueError("prospective source-language isolation addendum changed")
    return {"completion_seal":sha(DESIGN/"completion-seal-01.json"),"protocol":sha(DESIGN/"PROSPECTIVE-PROTOCOL.md"),
            "addendum":sha(addendum),"source_language_isolation_addendum":sha(isolation)}


def close_product(output,closure_path,expected_sha,explicit_passed=False,root=ROOT):
    if not explicit_passed:
        raise ValueError("Root must provide explicit completed passing closure before sealing product")
    output=work_path(output)
    path=new_stage(output,"product-closure.json")
    closure_path=work_path(closure_path)
    if sha(closure_path)!=expected_sha:
        raise ValueError("root-declared closure report hash changed")
    closure=json.loads(closure_path.read_bytes())
    code=product_hashes(root,closure.get("code_sha256"))
    if (closure.get("product_closure_passed") is not True and closure.get("status")!="PASS") or closure.get("code_sha256")!=code:
        raise ValueError("passing closure report must bind the exact current product code")
    value=dict(schema="work/source-first-product-closure@1",root_explicit_passed=True,
               closure_report=str(closure_path),closure_report_sha256=expected_sha,code_sha256=code,
               design_sha256=verify_design(),runner_sha256=sha(Path(__file__)),
               registration_or_next_family_body_read=False)
    return dict(product_closure_sha256=freeze(path,value),code_files=len(code))


def require_closure(output,root=ROOT):
    path=work_path(output)/"product-closure.json"
    if not path.is_file():
        raise ValueError("PRODUCT CLOSURE PENDING: no registration, metadata selection or body read allowed")
    value=json.loads(path.read_bytes())
    if (value.get("schema")!="work/source-first-product-closure@1" or value.get("root_explicit_passed") is not True
            or value.get("runner_sha256")!=sha(Path(__file__)) or value.get("design_sha256")!=verify_design()
            or value.get("code_sha256")!=product_hashes(root,value.get("code_sha256"))
            or sha(work_path(value["closure_report"]))!=value["closure_report_sha256"]):
        raise ValueError("closed product, root report, design or runner drifted")
    return value


def metadata_connection(path):
    """Database authorizer refuses dialogue-body reads during registration."""
    conn=census.open_read_only(work_path(path))
    def guard(action,table,column,database,trigger):
        if action==sqlite3.SQLITE_READ and table=="web_pages" and column=="text":
            return sqlite3.SQLITE_DENY
        return sqlite3.SQLITE_OK
    conn.set_authorizer(guard)
    return conn


def event_family(story):
    if not isinstance(story,str) or not re.fullmatch(r"event:\d+:\d+",story):
        raise ValueError("only stable event story identities are admitted")
    return ":".join(story.split(":")[:2])


def prior_exclusions(paths):
    if not paths:
        raise ValueError("prior registered metadata is required; no inferred empty history")
    families={"event:105","event:159"}
    evidence={}
    for path in paths:
        path=work_path(path)
        if any(word in path.name.lower() for word in ("reference","judgment","source-selection","target")):
            raise ValueError("exclusions must not load target/reference/selection bodies")
        value=json.loads(path.read_bytes())
        if value.get("schema") not in {"sekaisync/unseen-family-metadata-registration@1","sekaisync/p0-blind-holdout@1","work/source-first-metadata-registration@1"}:
            raise ValueError("unsupported prior registration metadata schema")
        keys=list(value.get("excluded_stories",[]))
        keys.extend(story["story_key"] for story in value.get("stories",[]))
        if isinstance(value.get("family"),dict):
            keys.append(value["family"]["story_key"])
        families.update(event_family(key) for key in keys if isinstance(key,str) and key.startswith("event:"))
        families.update(family for family in value.get("excluded_content_families",[]) if re.fullmatch(r"event:\d+",family))
        evidence[str(path)]=sha(path)
    return sorted(families),evidence


def audit_usage(story,scopes):
    if not scopes:
        raise ValueError("declared historical usage audit scopes are required")
    number=event_family(story).split(":")[1]
    pattern=rf"event:{number}(:|\b)|event_story:{number}:|event[_-]{number}([_:-]|\b)"
    globs=["*.py","*manifest*.json","*protocol*.json","*selection*.json","*report*.json","*seal*.json","*handoff*.json",
           "*registration*.json","*CHECKPOINT*.md","*REPORT*.md","*PROTOCOL*.md"]
    excludes=["!**/tmp/**","!**/store/**","!**/stores/**","!**/test-artifacts/**","!**/synthetic-artifacts/**","!**/test-tmp/**",
              "!**/test-temp/**","!**/*test-tmp*/**","!**/temp/**","!**/tmp*/**",
              "!**/raw/**","!**/*reference*/**","!**/*judgments*/**","!**/internal-tasks/**","!**/holdout-pages/**","!**/packets/**",
              "!**/census/logical-units.jsonl","!**/census/census-index.sqlite","!**/census/holdout-manifest.json","!**/census/*store*/**"]
    resolved=[Path(scope).resolve() for scope in scopes]
    if any(not scope.is_relative_to(ROOT) or not scope.exists() for scope in resolved):
        raise ValueError("usage audit scope missing or outside workspace")
    args=["rg","-uu","-l",pattern,*map(str,resolved)]
    for glob in globs+excludes:
        args.extend(["-g",glob])
    result=subprocess.run(args,cwd=ROOT,capture_output=True,text=True,encoding="utf-8",errors="replace")
    if result.returncode not in (0,1) or result.stderr.strip() or result.stdout.strip():
        raise ValueError("selected family prior use or unreadable usage scope; do not rerank: "+result.stdout+result.stderr)
    return dict(scopes=list(map(str,resolved)),pattern=pattern,included_globs=globs,excluded_globs=excludes,
                match_paths=[],mode="rg path matches only; no matched line bodies returned")


def metadata_universe(census_dir,excluded):
    census_dir=work_path(census_dir)
    units=[]
    with (census_dir/"logical-units.jsonl").open(encoding="utf-8") as stream:
        for line in stream:
            raw=json.loads(line)
            if raw.get("kind")=="event_story" and census.EVENT_STORY.fullmatch(raw.get("logical_key","")):
                story=raw["logical_key"].replace("event_story:","event:",1)
                units.append(dict(story_key=story,logical_key=raw["logical_key"],
                                  complete=raw.get("local_five_language_complete") is True,
                                  release_status=raw.get("release_status")))
    if len({u["story_key"] for u in units})!=len(units):
        raise ValueError("duplicate stable story identity in metadata universe")
    candidates=[]
    with closing(metadata_connection(census_dir/"census-index.sqlite")) as index:
        for unit in sorted(units,key=lambda u:u["story_key"]):
            if not unit["complete"] or unit["release_status"]!="all_five_released_in_verified_masterdata" or event_family(unit["story_key"]) in excluded:
                continue
            pages={}
            for lang in LANGS:
                variants=[json.loads(row[0]) for row in index.execute("SELECT metadata_json FROM pages WHERE logical_key=? AND language=? ORDER BY source,id",(unit["logical_key"],RAW[lang]))]
                usable=[p for p in variants if not p.get("bad_reasons") and not p.get("nonprimary_flags")]
                usable.sort(key=lambda p:(p["source"]!="altsource_ms",p["source"],p["id"]))
                if not usable:
                    break
                page=usable[0]
                if any(field in page for field in ("text","sentence","source_text","target_text")):
                    raise ValueError("dialogue body leaked into census metadata JSON")
                if not HASH.fullmatch(page.get("text_hash","")):
                    raise ValueError("usable metadata page lacks exact registered body hash")
                if termindex.page_story_key(page)!=unit["story_key"] or termindex._term_language(page["language"])!=lang:
                    raise ValueError("metadata page does not reproduce stable content/language identity")
                pages[lang]=dict(source=page["source"],page_id=page["id"],language=lang,
                                 text_sha256=page["text_hash"],metadata=page)
            if len(pages)==5:
                candidates.append(dict(unit,pages=pages,rank=hashlib.sha256((SEED+unit["story_key"]).encode()).hexdigest()))
    candidates.sort(key=lambda u:(u["rank"],u["story_key"]))
    if not candidates:
        raise ValueError("no complete eligible unseen event family; stop without body reads")
    return candidates


def strip_labels(value):
    if isinstance(value,dict):
        return {key:strip_labels(item) for key,item in value.items() if key not in {"title","sentence","name"}}
    if isinstance(value,list):
        return [strip_labels(item) for item in value]
    return value


def census_release(census_dir,family,as_of_utc):
    """Revalidate frozen local release source files without opening production DB."""
    raw=None
    with (work_path(census_dir)/"logical-units.jsonl").open(encoding="utf-8") as stream:
        for line in stream:
            candidate=json.loads(line)
            if candidate.get("logical_key")==family["logical_key"]:
                if raw is not None:
                    raise ValueError("duplicate release metadata identity")
                raw=candidate
    if raw is None:
        raise ValueError("selected local release metadata missing")
    event,episode=map(int,family["story_key"].split(":")[1:])
    as_of_ms=int(datetime.fromisoformat(as_of_utc).timestamp()*1000)
    result={}
    tables=census.RawTables()
    for lang in LANGS:
        proof=raw.get("release",{}).get(RAW[lang])
        if not isinstance(proof,dict) or proof.get("status")!="released_in_verified_masterdata" or proof.get("live_server_verified") is not False or proof.get("reasons"):
            raise ValueError("five verified local release metadata proofs required")
        if proof.get("event_id")!=event or proof.get("episode_no")!=episode or proof.get("region")!=census.REGIONS[RAW[lang]]:
            raise ValueError("release proof does not bind selected event/chapter/region")
        if not isinstance(proof.get("start_at_ms"),(int,float)) or isinstance(proof["start_at_ms"],bool) or not 0<proof["start_at_ms"]<=as_of_ms:
            raise ValueError("selected event publication is after fixed as-of")
        loaded={}
        for field in ("event_file","story_file","condition_file"):
            evidence=proof.get(field)
            if not isinstance(evidence,dict) or not isinstance(evidence.get("path"),str) or not HASH.fullmatch(evidence.get("sha256","")):
                raise ValueError("frozen local release source hash changed: "+field)
            loaded[field]=tables.load(ROOT/Path(evidence["path"]),evidence["sha256"])
            if loaded[field].get("error"):
                raise ValueError("frozen local release source hash changed or malformed: "+field)
        if "episode_file" in proof:
            evidence=proof["episode_file"]
            if not isinstance(evidence,dict) or not isinstance(evidence.get("path"),str) or not HASH.fullmatch(evidence.get("sha256","")):
                raise ValueError("frozen separate episode source hash changed")
            loaded["episode_file"]=tables.load(ROOT/Path(evidence["path"]),evidence["sha256"])
            if loaded["episode_file"].get("error"):
                raise ValueError("frozen separate episode source hash changed or malformed")
        chapter=proof.get("episode_record")
        condition=proof.get("unlock_condition")
        if not isinstance(chapter,dict) or chapter.get("episodeNo")!=episode or not chapter.get("scenarioId") or not isinstance(condition,dict):
            raise ValueError("release episode/unlock identities missing")
        events=[row for row in loaded["event_file"]["records"] if row.get("id")==event]
        stories=[row for row in loaded["story_file"]["records"] if row.get("eventId")==event]
        if len(events)!=1 or events[0].get("startAt")!=proof["start_at_ms"] or len(stories)!=1:
            raise ValueError("release proof event/story identity does not match frozen raw files")
        identity={field:events[0].get(field) for field in ("id","startAt","closedAt","endAt")}
        if proof.get("event_record_identity")!=identity:
            raise ValueError("release proof event record identity changed")
        episodes=list(stories[0].get("eventStoryEpisodes",[]))
        if "episode_file" in loaded:
            episodes.extend(row for row in loaded["episode_file"]["records"] if row.get("eventStoryId")==stories[0].get("id"))
        selected=[row for row in episodes if row.get("episodeNo")==episode]
        if not selected or any(row!=chapter for row in selected):
            raise ValueError("release proof episode identity does not match frozen raw files")
        conditions=[row for row in loaded["condition_file"]["records"] if row.get("id")==chapter.get("releaseConditionId")]
        if len(conditions)!=1 or conditions[0]!=condition:
            raise ValueError("release proof unlock identity does not match frozen raw files")
        if not ((condition.get("id")==1 and condition.get("releaseConditionType")=="none") or
                (condition.get("releaseConditionType")=="event_point" and condition.get("releaseConditionTypeId")==event and
                 isinstance(condition.get("releaseConditionTypeQuantity"),(int,float)) and condition["releaseConditionTypeQuantity"]>=0)):
            raise ValueError("unsupported frozen local unlock condition")
        for container in (stories[0],chapter):
            for key in ("releaseAt","startAt","availableAt","openAt"):
                if key in container and container[key] is not None and (not isinstance(container[key],(int,float)) or isinstance(container[key],bool) or not 0<container[key]<=as_of_ms):
                    raise ValueError("frozen chapter not released at fixed as-of")
        result[lang]=dict(strip_labels(proof),reverification="frozen census identity and raw local release files hash-identical; no live server or user unlock proof")
    return result


def register(output,census_dir,corpus_db,prior_paths,usage_scopes,as_of_utc,root=ROOT):
    require_closure(output,root)
    path=new_stage(output,"metadata-registration.json")
    output=work_path(output)
    corpus_db=work_path(corpus_db) if corpus_db else None
    census_dir=work_path(census_dir)
    excluded,prior=prior_exclusions(prior_paths)
    universe=metadata_universe(census_dir,excluded)
    family=deepcopy(universe[0])
    audit=audit_usage(family["story_key"],usage_scopes)
    if corpus_db:
        with closing(metadata_connection(corpus_db)) as conn:
            verifier=census.ReleaseVerifier(conn,datetime.fromisoformat(as_of_utc))
            release={lang:strip_labels(verifier.chapter(family["logical_key"],RAW[lang])) for lang in LANGS}
            for lang,page in family["pages"].items():
                row=conn.execute("SELECT source,id,kind,language,text_hash FROM web_pages WHERE source=? AND id=?",(page["source"],page["page_id"])).fetchone()
                if row is None or row["text_hash"]!=page["text_sha256"] or termindex._term_language(row["language"])!=lang:
                    raise ValueError("selected work corpus metadata drifted; do not rerank")
    else:
        release=census_release(census_dir,family,as_of_utc)
    if any(r.get("status")!="released_in_verified_masterdata" or r.get("live_server_verified") is not False or r.get("reasons") for r in release.values()):
        raise ValueError("selected family's five local release proofs failed; do not rerank")
    family["release"]=release
    universe_sha=freeze(output/"candidate-metadata-universe.json",universe)
    value=dict(schema="work/source-first-metadata-registration@1",family=family,seed=SEED,
               corpus_database=str(corpus_db) if corpus_db else None,source_body_handoff_pending=corpus_db is None,
               original_corpus_location_hint=str(ROOT/"store/kb/sekaisync.db"),as_of_utc=as_of_utc,excluded_content_families=excluded,
               prior_registration_sha256=prior,usage_audit=audit,
               candidate_universe_sha256=universe_sha,product_closure_sha256=sha(output/"product-closure.json"),
               metadata_inputs={str(census_dir/name):sha(census_dir/name) for name in ("logical-units.jsonl","census-index.sqlite")},
               source_or_target_bodies_read=False,registered_before_source_or_target_materialization=True,
               fixed_source_slots=10,fixed_directed_obligations=40,runner_sha256=sha(Path(__file__)))
    registration_sha=freeze(path,value)
    freeze(output/"metadata-registration-seal.json",dict(registration_sha256=registration_sha,
           candidate_universe_sha256=universe_sha,product_closure_sha256=value["product_closure_sha256"]))
    return dict(story_key=family["story_key"],registration_sha256=registration_sha,body_reads=0)


def verify_registration(output,root=ROOT):
    require_closure(output,root)
    output=work_path(output)
    seal=json.loads((output/"metadata-registration-seal.json").read_bytes())
    if sha(output/"metadata-registration.json")!=seal["registration_sha256"]:
        raise ValueError("original metadata registration file changed")
    value=json.loads((output/"metadata-registration.json").read_bytes())
    if (value.get("schema")!="work/source-first-metadata-registration@1" or value.get("runner_sha256")!=sha(Path(__file__))
            or value.get("product_closure_sha256")!=sha(output/"product-closure.json")
            or value.get("candidate_universe_sha256")!=sha(output/"candidate-metadata-universe.json")):
        raise ValueError("metadata registration binding drifted")
    for path,expected in {**value["metadata_inputs"],**value["prior_registration_sha256"]}.items():
        if sha(work_path(path))!=expected:
            raise ValueError("registered metadata/prior exclusion input changed")
    universe=json.loads((output/"candidate-metadata-universe.json").read_bytes())
    expected=deepcopy(universe[0])
    expected["release"]=value["family"]["release"]
    if expected!=value["family"]:
        raise ValueError("selected family differs from frozen metadata ranking")
    return value


def materialize_sources(output,root=ROOT,body_handoff=None):
    registration=verify_registration(output,root)
    output=work_path(output)
    stage=new_stage(output,"source-inputs")
    handoff=None
    if registration["corpus_database"] is None:
        if body_handoff is None:
            raise ValueError("REGISTERED BODY HANDOFF PENDING: Root must capture selected5 into work-only files after registration")
        body_handoff=work_path(body_handoff)
        handoff=json.loads(body_handoff.read_bytes())
        if (set(handoff)!={"schema","registration_sha256","pages"} or handoff["schema"]!="work/source-first-registered-body-handoff@1"
                or handoff["registration_sha256"]!=sha(output/"metadata-registration.json") or set(handoff["pages"])!=set(LANGS)):
            raise ValueError("selected-five body handoff is stale/incomplete/another registration")
    stage.mkdir()
    family=registration["family"]
    artifacts={}
    sources=[]
    def registered_rows():
        if handoff is not None:
            for lang in LANGS:
                observed=handoff["pages"][lang]
                page=family["pages"][lang]
                if set(observed)!={"source","page_id","language","body_file","text_sha256"} or any(observed[key]!=page[key] for key in ("source","page_id","language","text_sha256")):
                    raise ValueError("body handoff altered registered source/page/language/hash")
                body=work_path(observed["body_file"]).read_bytes()
                yield lang,dict(kind="event_story",language=lang,text_hash=observed["text_sha256"],text=body.decode("utf-8"))
        else:
            with closing(census.open_read_only(work_path(registration["corpus_database"]))) as conn:
                for lang in LANGS:
                    page=family["pages"][lang]
                    row=conn.execute("SELECT kind,language,text_hash,text FROM web_pages WHERE source=? AND id=?",(page["source"],page["page_id"])).fetchone()
                    yield lang,row
    for lang,row in registered_rows():
            page=family["pages"][lang]
            if (row is None or row["kind"]!="event_story" or termindex._term_language(row["language"])!=lang
                    or not isinstance(row["text"],str) or row["text_hash"]!=page["text_sha256"]
                    or hashlib.sha256(row["text"].encode()).hexdigest()!=page["text_sha256"]):
                raise ValueError("registered source body absent/stale/wrong language; preserve partial source stage")
            raw_page=dict(page["metadata"],language=page["metadata"].get("raw_language",page["metadata"]["language"]),text=row["text"])
            groups=termindex.group_pages_by_story([raw_page])
            windows=packets._scope_windows(groups,[family["story_key"]],lang,[])
            chosen=[window for window in windows if window["source"]["complete"]][:8]
            if not chosen:
                raise ValueError("no complete source-only rows; preserve no-unit debt, do not choose another family")
            if any(window["targets"] for window in chosen):
                raise ValueError("target bodies leaked into source-only projection")
            selector=dict(schema="work/source-only-selector@1",language=lang,story_key=family["story_key"],
                          rows=[dict(id=w["id"],story_key=w["story_key"],source=w["source"]) for w in chosen],
                          fixed_slots=["restricted_nominal","complete_operator_predicate"],target_bodies_read=False)
            text="\n".join(["SOURCE-ONLY SELECTION. Read only this language; do not inspect other files.",
                "Choose exactly two fixed slots using the frozen PROSPECTIVE-ADDENDUM-01 source rules; earliest source row/start/end, never target content. If absent retain no-unit debt. Preserve exact source vector and explicit unresolved referents. No auto punctuation widening.",
                "Return source_annotations=[{slot,status:selected|missing,kind:literal|segmented,evidence_id,segments,canonical,sense_key,sense_gloss,source_components,rationale}]. missing supplies slot,status,rationale only. These are internal source annotations, not normal target submissions.",
                "source_selector: "+json.dumps(selector,ensure_ascii=False)])+"\n"
            for name,value in ((lang+".json",selector),(lang+"-page.json",raw_page)):
                artifacts[name]=freeze(stage/name,value)
            with (stage/(lang+".txt")).open("x",encoding="utf-8") as stream:
                stream.write(text)
            artifacts[lang+".txt"]=sha(stage/(lang+".txt"))
            sources.append(dict(language=lang,shown_rows=len(chosen),selector_sha256=artifacts[lang+".json"]))
    report=dict(schema="work/source-first-source-inputs@1",registration_sha256=sha(output/"metadata-registration.json"),
                source_rows=sources,source_input_artifact_sha256=artifacts,
                target_packet_materialization=False,source_only_role_separate_from_target_alignment=True)
    report_sha=freeze(stage/"report.json",report)
    freeze(output/"source-inputs-seal.json",dict(source_input_report_sha256=report_sha,
           registration_sha256=report["registration_sha256"],source_input_artifact_sha256=artifacts))
    return dict(source_input_report_sha256=report_sha,source_languages=5,target_packets=0)


def load_source_inputs(output,root=ROOT):
    verify_registration(output,root)
    stage=work_path(output)/"source-inputs"
    seal=json.loads((work_path(output)/"source-inputs-seal.json").read_bytes())
    if sha(stage/"report.json")!=seal.get("source_input_report_sha256"):
        raise ValueError("source input report changed after independent stage seal")
    report=json.loads((stage/"report.json").read_bytes())
    expected={lang+suffix for lang in LANGS for suffix in (".json","-page.json",".txt")}
    if (report.get("schema")!="work/source-first-source-inputs@1" or report["registration_sha256"]!=sha(work_path(output)/"metadata-registration.json")
            or report["registration_sha256"]!=seal.get("registration_sha256") or set(report.get("source_input_artifact_sha256",{}))!=expected
            or report["source_input_artifact_sha256"]!=seal.get("source_input_artifact_sha256")):
        raise ValueError("source input registration drifted")
    for name,expected in report["source_input_artifact_sha256"].items():
        path=(stage/name).resolve()
        if not path.is_relative_to(stage) or sha(path)!=expected:
            raise ValueError("source-only input artifact changed or escapes stage")
    return report,{lang:json.loads((stage/(lang+".json")).read_bytes()) for lang in LANGS}


def nonempty(value):
    return isinstance(value,str) and bool(value.strip())


def components(value):
    return isinstance(value,list) and bool(value) and all(nonempty(item) for item in value)


def freeze_selection(output,answer_paths,root=ROOT):
    report,selectors=load_source_inputs(output,root)
    path=new_stage(output,"source-selection.json")
    if set(answer_paths)!=set(LANGS):
        raise ValueError("all five source-only selector answers are required")
    slots=[]
    for lang in LANGS:
        answer_path=work_path(answer_paths[lang])
        answer=json.loads(answer_path.read_bytes())
        if set(answer)!={"source_annotations"} or len(answer["source_annotations"])!=2:
            raise ValueError("exactly two fixed source slots are required, including missing strata")
        rows={row["id"]:row for row in selectors[lang]["rows"]}
        seen=set()
        selected_subjects=set()
        for annotation in answer["source_annotations"]:
            slot=annotation.get("slot")
            if slot not in {"restricted_nominal","complete_operator_predicate"} or slot in seen:
                raise ValueError("duplicate/unknown source stratum")
            seen.add(slot)
            value=dict(annotation,source_language=lang,slot_id=lang+":"+slot)
            if annotation.get("status")=="missing":
                if set(annotation)!={"slot","status","rationale"} or not nonempty(annotation["rationale"]):
                    raise ValueError("missing stratum requires explicit no-unit rationale")
            elif annotation.get("status")=="selected":
                if set(annotation)!={"slot","status","kind","evidence_id","segments","canonical","sense_key","sense_gloss","source_components","rationale"}:
                    raise ValueError("selected source annotation has unallowed fields, including target leakage")
                if annotation.get("evidence_id") not in rows or annotation.get("kind") not in {"literal","segmented"}:
                    raise ValueError("selected source must cite its source-only row and exact typed geometry")
                row=rows[annotation["evidence_id"]]
                constructor=span_subjects._literal if annotation["kind"]=="literal" else span_subjects._segmented
                args=[row["source"],row["story_key"]]
                if annotation["kind"]=="literal":
                    args.append(annotation.get("canonical"))
                subject=constructor(*args,annotation.get("segments"))
                sense=ledger._sense(subject["id"],lang,annotation.get("sense_key"),annotation.get("sense_gloss"))
                if subject["id"] in selected_subjects:
                    raise ValueError("two fixed source strata must select distinct exact source occurrences")
                selected_subjects.add(subject["id"])
                if annotation["canonical"]!=subject["canonical"]:
                    raise ValueError("source annotation canonical differs from exact typed source geometry")
                if not components(annotation.get("source_components")) or not nonempty(annotation.get("rationale")):
                    raise ValueError("source-only interpretation requires components and rationale")
                value.update(subject=subject,sense=sense)
            else:
                raise ValueError("source stratum status must be selected or missing")
            slots.append(value)
    grid=[dict(obligation_id="source-first:"+slot["slot_id"]+":"+target,source_slot_id=slot["slot_id"],
               source_language=slot["source_language"],target_language=target,source_status=slot["status"])
          for slot in slots for target in LANGS if target!=slot["source_language"]]
    assert len(slots)==10 and len(grid)==40
    value=dict(schema="work/source-first-fixed-source-selection@1",source_input_report_sha256=sha(work_path(output)/"source-inputs/report.json"),
               source_only=True,target_read=False,slots=slots,obligations=grid,
               answer_sha256={lang:dict(path=str(work_path(answer_paths[lang])),sha256=sha(answer_paths[lang])) for lang in LANGS})
    selection_sha=freeze(path,value)
    local_packets={}
    for lang in LANGS:
        local=dict(source_language=lang,source_selection_sha256=selection_sha,
                   rows=selectors[lang]["rows"],slots=[slot for slot in slots if slot["source_language"]==lang])
        packet=work_path(output)/(lang+"-source-reference.txt")
        with packet.open("x",encoding="utf-8") as stream:
            stream.write("INDEPENDENT SOURCE-ONLY REFERENCE. Read ONLY this language's two slots and raw context. Never inspect other language versions. Return {source_language,source_selection_sha256,source_packet_sha256,target_bodies_read:false,readings:[{source_slot_id,state:supported|source_unsettled|missing_stratum,sense_key,sense_gloss,source_components,rationale}]}. Preserve uncertainty; machine review is not certification.\n")
            stream.write("source_local_reference: "+json.dumps(local,ensure_ascii=False)+"\n")
        local_packets[lang]=sha(packet)
    packet_seal_sha=freeze(work_path(output)/"source-reference-packet-seal.json",dict(source_selection_sha256=selection_sha,local_packet_sha256=local_packets,
            required_readers_per_source_language=2,each_reader_allowed_language_count=1))
    freeze(work_path(output)/"source-selection-seal.json",dict(source_selection_sha256=selection_sha,
           source_input_report_sha256=value["source_input_report_sha256"],source_reference_packet_seal_sha256=packet_seal_sha))
    return dict(source_slots=10,directed_obligations=40,source_selection_sha256=selection_sha,source_reference_packets=5)


def load_source_selection(output,root=ROOT):
    load_source_inputs(output,root)
    output=work_path(output)
    seal=json.loads((output/"source-selection-seal.json").read_bytes())
    if (sha(output/"source-selection.json")!=seal.get("source_selection_sha256")
            or sha(output/"source-inputs/report.json")!=seal.get("source_input_report_sha256")
            or sha(output/"source-reference-packet-seal.json")!=seal.get("source_reference_packet_seal_sha256")):
        raise ValueError("source selection/report/local packet seal changed")
    selection=json.loads((output/"source-selection.json").read_bytes())
    packet_seal=json.loads((output/"source-reference-packet-seal.json").read_bytes())
    if (packet_seal.get("source_selection_sha256")!=sha(output/"source-selection.json")
            or set(packet_seal.get("local_packet_sha256",{}))!=set(LANGS)
            or packet_seal.get("required_readers_per_source_language")!=2 or packet_seal.get("each_reader_allowed_language_count")!=1
            or selection.get("source_input_report_sha256")!=sha(output/"source-inputs/report.json")):
        raise ValueError("source selection changed after source-local reference packet freeze")
    for answer in selection["answer_sha256"].values():
        if sha(work_path(answer["path"]))!=answer["sha256"]:
            raise ValueError("frozen source selector answer changed")
    for lang in LANGS:
        packet=output/(lang+"-source-reference.txt")
        if sha(packet)!=packet_seal["local_packet_sha256"][lang]:
            raise ValueError("source-local reference packet changed")
    return selection,packet_seal


def validate_readers(output,reader_paths,selection):
    output=work_path(output)
    if set(reader_paths)!=set(LANGS) or any(len(paths)!=2 for paths in reader_paths.values()) or len({work_path(path) for paths in reader_paths.values() for path in paths})!=10:
        raise ValueError("two distinct independent source-only answers PER source language required: ten readers, two slots each")
    refs=[]
    for lang in LANGS:
        packet=output/(lang+"-source-reference.txt")
        slots={slot["slot_id"]:slot for slot in selection["slots"] if slot["source_language"]==lang}
        for input_path in reader_paths[lang]:
            input_path=work_path(input_path)
            answer=json.loads(input_path.read_bytes())
            if (set(answer)!={"source_language","source_selection_sha256","source_packet_sha256","target_bodies_read","readings"}
                    or answer["source_language"]!=lang or answer["source_packet_sha256"]!=sha(packet)
                    or answer["source_selection_sha256"]!=sha(output/"source-selection.json")
                    or answer["target_bodies_read"] is not False or not isinstance(answer["readings"],list) or len(answer["readings"])!=2
                    or any(not isinstance(r,dict) for r in answer["readings"])
                    or {r.get("source_slot_id") for r in answer["readings"]}!=set(slots)):
                raise ValueError("each source reference reader must bind ONLY one language's two fixed slots")
            if any(set(r)!={"source_slot_id","state","sense_key","sense_gloss","source_components","rationale"}
                   or r.get("state") not in {"supported","source_unsettled","missing_stratum"} or not nonempty(r.get("rationale"))
                   or not nonempty(r.get("sense_key")) or not nonempty(r.get("sense_gloss")) or not components(r.get("source_components"))
                   or ((r["state"]=="missing_stratum")!=(slots[r["source_slot_id"]]["status"]=="missing")) for r in answer["readings"]):
                raise ValueError("source reference fields/state/rationale missing or target leakage")
            refs.append(dict(source_language=lang,path=str(input_path),sha256=sha(input_path)))
    return refs


def freeze_source_references(output,reader_paths,root=ROOT):
    selection,packet_seal=load_source_selection(output,root)
    output=work_path(output)
    path=new_stage(output,"source-reference-freeze.json")
    refs=validate_readers(output,reader_paths,selection)
    value=dict(schema="work/source-first-source-reference-freeze@1",source_selection_sha256=sha(output/"source-selection.json"),
               source_reference_packet_seal_sha256=sha(output/"source-reference-packet-seal.json"),
               reader_answers=refs,required_readers_per_source_language=2,all_ten_source_slots_retained=True,target_packets_materialized=False,
               source_only_declaration=True,semantic_certificate=False)
    gate_sha=freeze(path,value)
    freeze(output/"source-reference-freeze-seal.json",dict(source_reference_freeze_sha256=gate_sha,
           source_selection_sha256=value["source_selection_sha256"],source_reference_packet_seal_sha256=value["source_reference_packet_seal_sha256"]))
    return dict(source_reference_freeze_sha256=gate_sha,source_slots=10,target_packets=0)


def target_setup(output,root=ROOT):
    report,selectors=load_source_inputs(output,root)
    output=work_path(output)
    gate=output/"source-reference-freeze.json"
    if not gate.is_file():
        raise ValueError("SOURCE REFERENCES PENDING: no target alignment/import/export permitted")
    selection,packet_seal=load_source_selection(output,root)
    gate_seal=json.loads((output/"source-reference-freeze-seal.json").read_bytes())
    if sha(gate)!=gate_seal.get("source_reference_freeze_sha256"):
        raise ValueError("source-only reference gate changed after independent seal")
    ref=json.loads(gate.read_bytes())
    if (ref.get("schema")!="work/source-first-source-reference-freeze@1" or ref.get("source_only_declaration") is not True
            or ref.get("source_selection_sha256")!=sha(output/"source-selection.json")
            or ref.get("source_selection_sha256")!=gate_seal.get("source_selection_sha256")
            or ref.get("source_reference_packet_seal_sha256")!=sha(output/"source-reference-packet-seal.json")
            or ref.get("source_reference_packet_seal_sha256")!=gate_seal.get("source_reference_packet_seal_sha256")
            or ref.get("all_ten_source_slots_retained") is not True or ref.get("target_packets_materialized") is not False
            or ref.get("semantic_certificate") is not False
            or ref.get("required_readers_per_source_language")!=2 or len(ref.get("reader_answers",[]))!=10
            or any(sum(reader.get("source_language")==lang for reader in ref["reader_answers"])!=2 for lang in LANGS)
            or len({reader["path"] for reader in ref["reader_answers"]})!=10):
        raise ValueError("source-only reference gate is stale/invalid")
    for reader in ref["reader_answers"]:
        if sha(work_path(reader["path"]))!=reader["sha256"]:
            raise ValueError("source-only reference answer changed")
    reader_paths={lang:[reader["path"] for reader in ref["reader_answers"] if reader["source_language"]==lang] for lang in LANGS}
    if validate_readers(output,reader_paths,selection)!=ref["reader_answers"]:
        raise ValueError("source-only reference gate answer order or language binding changed")
    stage=new_stage(output,"common-source-setup")
    stage.mkdir()
    pages=[json.loads((output/"source-inputs"/(lang+"-page.json")).read_bytes()) for lang in LANGS]
    registration=verify_registration(output,root)
    story=registration["family"]["story_key"]
    groups=termindex.group_pages_by_story(pages)
    if set(groups)!={story} or len(groups[story])!=5:
        raise ValueError("current product policy rejected five registered pages")
    store=stage/"store"
    dbstore.initialize(store)
    for provider in sorted({page["source"] for page in pages}):
        dbstore.upsert_web_pages(store,provider,[page for page in pages if page["source"]==provider])
    actual=[]
    evidence=[]
    (stage/"packets").mkdir()
    for lang in LANGS:
        items,meta=packets._prepare_scrub_review(store,groups,[story],[],{},lang,[l for l in LANGS if l!=lang])
        discoveries=[item for item in items if item._context.get("task")=="discovery"]
        if not discoveries:
            raise ValueError("normal product did not create source discovery work")
        source_views=[w["source"] for w in packets._read_scope(store,meta["scope_id"])["windows"] if w["source"]["complete"]][:8]
        if source_views!=[row["source"] for row in selectors[lang]["rows"]]:
            raise ValueError("target setup changed original source windows or order")
        actual.extend(discoveries)
        first=discoveries[0]
        packet_path=stage/"packets"/(lang+".txt")
        with packet_path.open("x",encoding="utf-8") as stream:
            stream.write(review.render_item(first)+"\n")
        if '"target"' in packet_path.read_text(encoding="utf-8") or '"targets"' in packet_path.read_text(encoding="utf-8"):
            raise ValueError("ordinary source TXT leaked target bodies")
        evidence.append(dict(language=lang,scope_id=meta["scope_id"],first_discovery_item_id=first.id,
                             discovery_packets=len(discoveries),source_packet_sha256=sha(packet_path)))
    queued=review.enqueue(store,actual)
    with dbstore.connect(store) as conn:
        if conn.execute("SELECT COUNT(*) FROM terms").fetchone()[0]:
            raise ValueError("new common trial unexpectedly published global terms")
    value=dict(schema="work/source-first-common-source-setup@1",story_key=story,store=str(store),
               source_reference_freeze_sha256=sha(gate),product_closure_sha256=sha(output/"product-closure.json"),
               registration_sha256=sha(output/"metadata-registration.json"),normal_enqueue=queued,languages=evidence,
               source_views_equal_before_after_target_alignment=True,target_answers_or_references_read=False)
    return dict(common_source_setup_sha256=freeze(stage/"report.json",value),source_languages=5,queued=queued)


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("stage",choices=["close-product","register","verify-registration","materialize-sources","freeze-selection","freeze-source-references","target-setup"])
    parser.add_argument("--output",type=Path,required=True)
    parser.add_argument("--closure-report",type=Path)
    parser.add_argument("--closure-sha256")
    parser.add_argument("--product-closure-passed",action="store_true")
    parser.add_argument("--census-dir",type=Path)
    parser.add_argument("--corpus-db",type=Path)
    parser.add_argument("--body-handoff",type=Path)
    parser.add_argument("--prior-registration",action="append",type=Path,default=[])
    parser.add_argument("--usage-scope",action="append",type=Path,default=[])
    parser.add_argument("--as-of-utc")
    parser.add_argument("--answer",action="append",default=[],help="LANG=path for five source selector answers")
    parser.add_argument("--source-reader-answer",action="append",default=[],help="LANG=path, two independent reader files for each source language")
    args=parser.parse_args()
    if args.stage=="close-product":
        if not args.closure_report or not args.closure_sha256:
            parser.error("root closure report and SHA are required")
        result=close_product(args.output,args.closure_report,args.closure_sha256,args.product_closure_passed)
    elif args.stage=="register":
        if not args.census_dir or not args.as_of_utc:
            parser.error("register needs work-only full census metadata path and as-of UTC; corpus DB is optional")
        result=register(args.output,args.census_dir,args.corpus_db,args.prior_registration,args.usage_scope,args.as_of_utc)
    elif args.stage=="verify-registration":
        value=verify_registration(args.output)
        result=dict(story_key=value["family"]["story_key"],registration_verified=True,body_reads=0)
    elif args.stage=="materialize-sources":
        result=materialize_sources(args.output,body_handoff=args.body_handoff)
    elif args.stage=="freeze-selection":
        answers={lang:Path(path) for lang,path in (value.split("=",1) for value in args.answer)}
        result=freeze_selection(args.output,answers)
    elif args.stage=="freeze-source-references":
        readers={lang:[] for lang in LANGS}
        for value in args.source_reader_answer:
            lang,path=value.split("=",1)
            readers[lang].append(Path(path))
        result=freeze_source_references(args.output,readers)
    else:
        result=target_setup(args.output)
    print(json.dumps(result,ensure_ascii=False,indent=2))


if __name__=="__main__":
    main()
