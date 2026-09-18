"""W4 — agent_review 的权威状态迁移到 SQLite（Astra P10/D10）。

验收口径（全部走 tempfile 临时库，绝不碰真实 store）：

1. 端到端（v3）：enqueue → ``review_queue`` 有行；submit_judgments →
   ``review_decisions`` 有行、队列 resolved；consult 复用；幂等。
2. 守恒：同 evidence_revision 重复 enqueue 不重复入队；resolved 不复活。
3. 旧 JSON 存量导入：预置 JSON queue → enqueue/consult 行为一致。
4. v1 库不回归：JSON 权威路径仍工作（钉住）。
5. 红绿双向：把 SQLite 写路径退回 ``_write_json`` 时，"enqueue 后 SQLite 有行"
   与"幂等"两个用例必须变红（见 work/ 报告的两次实测输出）。

与既有套件的关系：trinity/term_slots 的 layered 路径共享 ``review_queue``
表（``slot-review:`` 前缀的行），这里同时验证两条生产路径在同一张表上共存。
"""

from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from sekaisync import agent_review as ar
from sekaisync import dbstore


def _v3_store(tmp: Path) -> Path:
    """显式初始化 + 迁移到 v3 的临时库（不走任何隐式/自动迁移）。"""
    store = tmp / "store"
    dbstore.initialize_new_store(store, target_version=3)
    return store


def _v1_store(tmp: Path) -> Path:
    store = tmp / "store_v1"
    dbstore.initialize(store)
    return store


class ReviewSqlTestBase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)

    def queue_rows(self, store: Path, sql: str = "SELECT item_id, term_id, language, status FROM review_queue ORDER BY rowid"):
        with dbstore.connect(store) as conn:
            return conn.execute(sql).fetchall()

    def revision(self, store: Path) -> int:
        with dbstore.connect(store) as conn:
            return dbstore.current_revision(conn)


class EnqueueSqlTests(ReviewSqlTestBase):
    """验收 1a + 2：enqueue 落 SQLite、JSON 不再是权威、同证据版本不重复。"""

    def setUp(self) -> None:
        super().setUp()
        self.store = _v3_store(self.tmp)
        self.item = ar.make_review_item(
            "ニーゴ", "en", ["N25", "Huh"], kind="conflict", chosen_hint="N25",
            evidence=["桐谷遥：ニーゴのみんな、集まってくれてありがとう"],
            story_keys=["web:sekai_best:zh-cn:card_story:2"],
            channels=["C", "A"], reason="通道冲突",
        )

    def test_enqueue_lands_in_sqlite_and_json_is_not_authoritative(self):
        result = ar.enqueue(self.store, [self.item])
        self.assertEqual(
            {k: result[k] for k in ("added", "skipped_dup", "skipped_settled", "queue_size")},
            {"added": 1, "skipped_dup": 0, "skipped_settled": 0, "queue_size": 1},
        )
        # SQLite 有行（验收 1a）
        rows = self.queue_rows(self.store)
        self.assertEqual(len(rows), 1)
        item_id, term_id, language, status = rows[0]
        self.assertEqual(term_id, "ニーゴ")
        self.assertEqual(language, "en")
        self.assertEqual(status, "queued")
        self.assertEqual(ar.item_id("ニーゴ", "en", ["N25", "Huh"]), item_id)
        # JSON 不再是权威：权威文件没有被写出来
        self.assertFalse(ar.queue_path(self.store).exists())
        # 但读路径（load_queue / export）仍能看到该条目
        loaded = ar.load_queue(self.store)
        self.assertEqual([i.id for i in loaded], [item_id])
        self.assertEqual(loaded[0].candidates, ["N25", "Huh"])
        self.assertEqual(loaded[0].chosen_hint, "N25")
        self.assertEqual(loaded[0].channels, ["C", "A"])
        self.assertEqual(loaded[0].story_keys, ["web:sekai_best:zh-cn:card_story:2"])

    def test_enqueue_bumps_revision_once_for_real_facts_only(self):
        before = self.revision(self.store)
        ar.enqueue(self.store, [self.item])
        self.assertEqual(self.revision(self.store), before + 1)
        # 纯 skipped（重复入队）是 no-op：不推 revision（Astra：no-op 不 bump）
        ar.enqueue(self.store, [self.item])
        self.assertEqual(self.revision(self.store), before + 1)

    def test_same_evidence_revision_enqueue_is_conserved(self):
        """验收 2：同内容重复 enqueue 不重复入队（守恒）。"""
        ar.enqueue(self.store, [self.item])
        again = ar.enqueue(self.store, [ar.make_review_item(
            "ニーゴ", "en", ["Huh", "N25"], kind="conflict", chosen_hint="N25",
            evidence=["桐谷遥：ニーゴのみんな、集まってくれてありがとう"],
            story_keys=["web:sekai_best:zh-cn:card_story:2"],
            channels=["A", "C"], reason="通道冲突",
        )])
        self.assertEqual(again["added"], 0)
        self.assertEqual(again["skipped_dup"], 1)
        self.assertEqual(len(self.queue_rows(self.store)), 1)

    def test_same_term_lang_new_content_requeues_and_supersedes_never_resurrects(self):
        """同 (term, language) 内容变化 → 新条目入队；已 resolved 的旧条目不复活。"""
        ar.enqueue(self.store, [self.item])
        judgment = {"id": self.item.id, "decision": "accept", "value": "N25", "rationale": "x"}
        ar.submit_judgments(self.store, [judgment])
        self.assertEqual(self.queue_rows(self.store)[0][3], "resolved")
        # 同 id 再入队：resolved 不复活（skipped_settled，不是 added）
        revived = ar.enqueue(self.store, [self.item])
        self.assertEqual(revived["added"], 0)
        self.assertEqual(revived["skipped_settled"], 1)
        self.assertEqual(len(self.queue_rows(self.store)), 1)
        # 内容变化（方法论未覆盖的新候选）→ 新 evidence_revision → 允许再次入队
        evolved = ar.make_review_item("ニーゴ", "en", ["25時"], kind="conflict")
        evolved_result = ar.enqueue(self.store, [evolved])
        self.assertEqual(evolved_result["added"], 1)
        # 含已裁决候选（N25）的项仍按"已结算"跳过——与旧 JSON 行为一致
        # （_lookup 对任一候选命中 pair_accept 即返回 settled）。
        covered = ar.make_review_item("ニーゴ", "en", ["N25", "25時"], kind="conflict")
        self.assertEqual(ar.enqueue(self.store, [covered])["skipped_settled"], 1)

    def test_coexists_with_slot_review_producer_rows(self):
        """trinity（``slot-review:``）与本模块（``rv:``）共用一张表，互不干扰。"""
        with dbstore.connect(self.store) as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                "INSERT INTO review_queue(item_id,term_id,language,scope_json,candidates_json,"
                "evidence_refs_json,evidence_snapshot_json,input_revision,evidence_revision,status) "
                "VALUES('slot-review:abc','ニーゴ','en','{}','[\"Huh\"]','[]','[]',1,'e1','queued')"
            )
            dbstore.bump_revision(conn)
            conn.commit()
        result = ar.enqueue(self.store, [self.item])
        self.assertEqual(result["added"], 1)
        ids = {row[0] for row in self.queue_rows(self.store)}
        self.assertEqual(ids, {"slot-review:abc", self.item.id})
        # 裁决 rv 项 → 同 (term, language) 的 slot-review 行被置 superseded
        ar.submit_judgments(self.store, [
            {"id": self.item.id, "decision": "accept", "value": "N25", "rationale": "x"}])
        statuses = {row[0]: row[3] for row in self.queue_rows(self.store)}
        self.assertEqual(statuses[self.item.id], "resolved")
        self.assertEqual(statuses["slot-review:abc"], "superseded")


class SubmitSqlTests(ReviewSqlTestBase):
    """验收 1b/1c + 1d：submit 落 decisions/rules、队列 resolved、幂等。"""

    def setUp(self) -> None:
        super().setUp()
        self.store = _v3_store(self.tmp)
        self.item = ar.make_review_item(
            "ニーゴ", "en", ["N25", "Huh"], kind="conflict", chosen_hint="N25",
            evidence=["桐谷遥：ニーゴのみんな"], story_keys=["web:1"], channels=["C"],
        )
        ar.enqueue(self.store, [self.item])

    def test_submit_persists_decision_and_resolves_queue(self):
        result = ar.submit_judgments(self.store, [
            {"id": self.item.id, "decision": "accept", "value": "N25",
             "rationale": "简称", "generalize": "pair", "agent": "zcode", "session": "s1"},
        ])
        self.assertEqual(result["accepted"], 1)
        self.assertEqual(result["decisions_added"], 1)
        self.assertEqual(result["errors"], [])
        with dbstore.connect(self.store) as conn:
            decisions = conn.execute(
                "SELECT decision_id, item_id, term_id, language, action, value, payload_json "
                "FROM review_decisions").fetchall()
            statuses = dict(self.queue_rows(self.store, "SELECT item_id, status FROM review_queue"))
            rules = conn.execute(
                "SELECT family, key, value, active, revision FROM review_rules").fetchall()
        # decisions 有行（验收 1b）
        self.assertEqual(len(decisions), 1)
        decision_id, item_id, term_id, language, action, value, payload_json = decisions[0]
        self.assertEqual(term_id, "ニーゴ")
        self.assertEqual(action, "accept")
        self.assertEqual(value, "N25")
        self.assertEqual(item_id, self.item.id)
        payload = json.loads(payload_json)
        self.assertEqual(payload["rationale"], "简称")
        self.assertEqual(payload["provenance"]["agent"], "zcode")
        self.assertEqual(payload["provenance"]["session"], "s1")
        # 队列状态 resolved（验收 1b）
        self.assertEqual(statuses, {self.item.id: "resolved"})
        # generalize 规则镜像进 review_rules
        self.assertEqual(rules, [("pair", "ニーゴ|en|N25", "N25", 1, 1)])
        # 验收 1c：consult 复用命中（与旧行为一致）
        hit = ar.consult(self.store, "ニーゴ", "en", ["N25", "Huh"])
        self.assertIsNotNone(hit)
        self.assertEqual(hit["decision"], "accept")
        self.assertEqual(hit["value"], "N25")
        self.assertEqual(hit["source"], "pair")
        entries = ar.load_methodology(self.store)
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0].hits, 1)

    def test_resubmit_is_idempotent_no_second_decision_no_revision_bump(self):
        """验收 1d：重复 submit 不产生第二条 decision（幂等）。

        前置断言（防"空库也算幂等"的假绿）：第一次 submit 必须真的在 SQLite
        里留下恰好一行 decision——这正是红绿双向验证的抓手：SQLite 写路径被
        移除（退回 _write_json）时，本用例必须变红。
        """
        judgment = [{"id": self.item.id, "decision": "accept", "value": "N25",
                     "rationale": "简称", "generalize": "pair"}]
        first = ar.submit_judgments(self.store, judgment)
        with dbstore.connect(self.store) as conn:
            count_first = conn.execute("SELECT COUNT(*) FROM review_decisions").fetchone()[0]
        self.assertEqual(first["decisions_added"], 1)
        self.assertEqual(count_first, 1, "第一次 submit 没有把 decision 落进 SQLite")
        revision_after_first = self.revision(self.store)
        second = ar.submit_judgments(self.store, judgment)
        with dbstore.connect(self.store) as conn:
            count_second = conn.execute("SELECT COUNT(*) FROM review_decisions").fetchone()[0]
        self.assertEqual(second["decisions_added"], 0)
        self.assertEqual(second["accepted"], 0)
        self.assertEqual(count_first, count_second, "重复 submit 产生了第二条 decision")
        # 纯重复不 bump revision（事实没变）
        self.assertEqual(self.revision(self.store), revision_after_first)

    def test_reject_records_every_refused_candidate(self):
        item = ar.make_review_item("セカイ", "en", ["Sekai", "World"], kind="gate_failed")
        ar.enqueue(self.store, [item])
        result = ar.submit_judgments(self.store, [
            {"id": item.id, "decision": "reject", "rationale": "通用词", "generalize": "pair"}])
        self.assertEqual(result["rejected"], 1)
        with dbstore.connect(self.store) as conn:
            rows = conn.execute("SELECT action, value FROM review_decisions WHERE term_id='セカイ'").fetchall()
        self.assertEqual(sorted(rows), [("reject", ""), ("reject", "")])
        hit = ar.consult(self.store, "セカイ", "en", ["Sekai", "World"])
        self.assertEqual(hit["decision"], "reject")
        self.assertEqual(hit["rejected"], ["Sekai", "World"])

    def test_one_off_decision_is_reusable_and_scoped(self):
        """不泛化的裁决也持久化（Astra D10），且只对同一 (term, language) 复用。"""
        result = ar.submit_judgments(self.store, [
            {"id": self.item.id, "decision": "accept", "value": "N25", "rationale": "x"}])
        self.assertEqual(result["decisions_added"], 1)
        self.assertEqual(result["methodology_added"], 0)
        hit = ar.consult(self.store, "ニーゴ", "en", ["N25", "Huh"])
        self.assertIsNotNone(hit)
        self.assertEqual(hit["source"], "decision")
        # 不泄漏到别的术语
        self.assertIsNone(ar.consult(self.store, "别的术语", "en", ["N25"]))

    def test_rule_flip_supersedes_previous_revision(self):
        """改判（同 key 翻转 accept→reject）→ 旧行 active=0，新行 revision+1。"""
        ar.submit_judgments(self.store, [
            {"id": self.item.id, "decision": "accept", "value": "N25",
             "rationale": "x", "generalize": "pair"}])
        with dbstore.connect(self.store) as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute("UPDATE review_queue SET status='queued' WHERE item_id=?", (self.item.id,))
            dbstore.bump_revision(conn)
            conn.commit()
        ar.submit_judgments(self.store, [
            {"id": self.item.id, "decision": "reject", "candidates": ["N25"],
             "rationale": "改判", "generalize": "pair"}])
        with dbstore.connect(self.store) as conn:
            rules = conn.execute(
                "SELECT value, active, revision FROM review_rules "
                "WHERE key='ニーゴ|en|N25' ORDER BY revision").fetchall()
        self.assertEqual(rules, [("N25", 0, 1), ("", 1, 2)])


class ConsultSqlTests(ReviewSqlTestBase):
    """consult 复用判定必须来自 SQLite（决策日志），而方法论留在 JSON。"""

    def setUp(self) -> None:
        super().setUp()
        self.store = _v3_store(self.tmp)

    def test_consult_decision_survives_without_methodology_file(self):
        item = ar.make_review_item("SEKAI", "en", ["SEKAI"], kind="conflict")
        ar.enqueue(self.store, [item])
        ar.submit_judgments(self.store, [
            {"id": item.id, "decision": "accept", "value": "SEKAI", "rationale": "x"}])
        self.assertFalse(ar.methodology_path(self.store).exists())
        # 决策在 SQLite：把 decisions JSON 路径指到别处也无法绕开——权威是库
        with dbstore.connect(self.store) as conn:
            rows = conn.execute("SELECT COUNT(*) FROM review_decisions").fetchone()
        self.assertEqual(rows[0], 1)
        hit = ar.consult(self.store, "SEKAI", "en", ["SEKAI"])
        self.assertIsNotNone(hit)
        self.assertEqual(hit["value"], "SEKAI")
        self.assertEqual(hit["source"], "decision")
        # 重新入队不打扰（Astra P10）
        again = ar.enqueue(self.store, [item])
        self.assertEqual(again["skipped_settled"], 1)

    def test_methodology_file_still_wins_over_decisions(self):
        """方法论文件存在时 owns the outcome（旧语义保留）。"""
        item = ar.make_review_item("SEKAI", "en", ["SEKAI"], kind="conflict")
        ar.enqueue(self.store, [item])
        ar.submit_judgments(self.store, [
            {"id": item.id, "decision": "accept", "value": "SEKAI", "rationale": "x"}])
        # 手工写一条 retire 过的 pair_accept —— 失效规则意味着判断被撤回，
        # 不得被旧 decision 旁路（fail closed 语义照旧）。
        path = ar.methodology_path(self.store)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({
            "version": 1,
            "entries": [{"kind": "pair_accept", "key": "SEKAI|en|SEKAI", "value": "SEKAI",
                         "rationale": "retired", "hits": 0,
                         "provenance": {"agent": "h", "session": "", "ts": ar.now_iso(),
                                        "retired": ar.now_iso()}}],
        }, ensure_ascii=False), encoding="utf-8")
        self.assertIsNone(ar.consult(self.store, "SEKAI", "en", ["SEKAI"]))

    def test_pattern_rules_still_flow_through_methodology_json(self):
        """pattern 的 scope/hit_terms 语义暂留 JSON（边界见模块 docstring）。"""
        item = ar.make_review_item("プロセカ", "en", ["PJSK"], kind="pending")
        ar.enqueue(self.store, [item])
        result = ar.submit_judgments(self.store, [
            {"id": item.id, "decision": "accept", "value": "PJSK",
             "rationale": "PJ缩写", "generalize": "pattern",
             "pattern": "prefix:PJ", "scope": {"lang": "en"}}])
        self.assertEqual(result["methodology_added"], 1)
        with dbstore.connect(self.store) as conn:
            rule = conn.execute(
                "SELECT family, value, scope_json FROM review_rules WHERE family='pattern'").fetchone()
        self.assertEqual(rule, ("pattern", "PJSK", '{"lang":"en"}'))
        hit = ar.consult(self.store, "新規", "en", ["PJX"])
        self.assertEqual(hit["value"], "PJSK")
        entry = next(e for e in ar.load_methodology(self.store) if e.kind == "pattern_accept")
        self.assertEqual(entry.provenance["hit_terms"], ["新規"])


class LegacyJsonImportTests(ReviewSqlTestBase):
    """验收 3：旧 JSON 存量一次性导入，行为与原生一致，导入幂等。"""

    def setUp(self) -> None:
        super().setUp()
        self.store = _v3_store(self.tmp)

    def _seed_legacy_json(self, items: list[ar.ReviewItem]) -> None:
        path = ar.queue_path(self.store)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(
            {"version": 1, "updated_at": ar.now_iso(),
             "items": [item.to_dict() for item in items]},
            ensure_ascii=False), encoding="utf-8")

    def test_legacy_queue_is_imported_once_on_first_access(self):
        legacy = ar.make_review_item("星乃一歌", "en", ["Hoshino Ichika", "Ichika Hoshino"],
                                     kind="pending", chosen_hint="Hoshino Ichika",
                                     reason="低置信", channels=["A"])
        self._seed_legacy_json([legacy])
        # 首次访问：导入 + bump revision
        before = self.revision(self.store)
        loaded = ar.load_queue(self.store)
        self.assertEqual([i.id for i in loaded], [legacy.id])
        self.assertEqual(loaded[0].candidates, ["Hoshino Ichika", "Ichika Hoshino"])
        self.assertEqual(self.revision(self.store), before + 1)
        # 第二次访问不重复导入（幂等），也不再 bump
        after = self.revision(self.store)
        self.assertEqual([i.id for i in ar.load_queue(self.store)], [legacy.id])
        self.assertEqual(self.revision(self.store), after)
        # 对导入条目 enqueue：skipped_dup（不重复入队）
        result = ar.enqueue(self.store, [legacy])
        self.assertEqual(result["added"], 0)
        self.assertEqual(result["skipped_dup"], 1)

    def test_imported_item_behaves_like_a_native_one(self):
        legacy = ar.make_review_item("星乃一歌", "en", ["Hoshino Ichika"], kind="pending")
        self._seed_legacy_json([legacy])
        ar.load_queue(self.store)  # 触发导入
        result = ar.submit_judgments(self.store, [
            {"id": legacy.id, "decision": "accept", "value": "Hoshino Ichika",
             "rationale": "名前姓后", "generalize": "pair"}])
        self.assertEqual(result["accepted"], 1)
        self.assertEqual(result["remaining"], 0)
        hit = ar.consult(self.store, "星乃一歌", "en", ["Hoshino Ichika"])
        self.assertEqual(hit["decision"], "accept")
        self.assertEqual(hit["value"], "Hoshino Ichika")

    def test_empty_sqlite_table_is_required_for_import(self):
        """SQLite 非空时不导入（防双权威）：已有行优先，JSON 只是交换格式。"""
        native = ar.make_review_item("新語", "en", ["X"], kind="pending")
        ar.enqueue(self.store, [native])
        legacy = ar.make_review_item("星乃一歌", "en", ["Hoshino Ichika"], kind="pending")
        self._seed_legacy_json([legacy])
        loaded = ar.load_queue(self.store)
        self.assertEqual([i.id for i in loaded], [native.id])


class V1StoreTests(ReviewSqlTestBase):
    """验收 4：v1 库不回归——JSON 权威，且不在 v1 上建表。"""

    def setUp(self) -> None:
        super().setUp()
        self.store = _v1_store(self.tmp)

    def test_v1_has_no_review_tables_and_none_are_created(self):
        with dbstore.connect(self.store) as conn:
            tables = {
                row[0] for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertNotIn("review_queue", tables)
        self.assertNotIn("review_decisions", tables)
        item = ar.make_review_item("ニーゴ", "en", ["N25"], kind="conflict")
        ar.enqueue(self.store, [item])
        with dbstore.connect(self.store) as conn:
            tables = {
                row[0] for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertNotIn("review_queue", tables)

    def test_v1_enqueue_submit_consult_still_work_on_json(self):
        item = ar.make_review_item("ニーゴ", "en", ["N25", "Huh"], kind="conflict", chosen_hint="N25")
        result = ar.enqueue(self.store, [item])
        self.assertEqual(result["added"], 1)
        # JSON 权威：文件被写出
        self.assertTrue(ar.queue_path(self.store).exists())
        payload = json.loads(ar.queue_path(self.store).read_text(encoding="utf-8"))
        self.assertEqual([entry["id"] for entry in payload["items"]], [item.id])
        result2 = ar.submit_judgments(self.store, [
            {"id": item.id, "decision": "accept", "value": "N25",
             "rationale": "简称", "generalize": "pair"}])
        self.assertEqual(result2["accepted"], 1)
        self.assertEqual(ar.load_queue(self.store), [])
        # 决策留痕在 JSON（v1 没有 review_decisions 表）
        self.assertTrue(ar.decisions_path(self.store).exists())
        hit = ar.consult(self.store, "ニーゴ", "en", ["N25", "Huh"])
        self.assertIsNotNone(hit)
        self.assertEqual(hit["value"], "N25")
        again = ar.enqueue(self.store, [item])
        self.assertEqual(again["skipped_settled"], 1)


class ExportCompatTests(ReviewSqlTestBase):
    """验收 5 的库级部分：JSON 导出/submit 使用方式不破坏（CLI 冒烟另行手动）。"""

    def setUp(self) -> None:
        super().setUp()
        self.store = _v3_store(self.tmp)

    def test_export_and_submit_roundtrip_on_sqlite_backed_store(self):
        item = ar.make_review_item("ニーゴ", "en", ["N25", "Huh"], kind="conflict",
                                   chosen_hint="N25", reason="冲突", channels=["C"])
        ar.enqueue(self.store, [item])
        out = ar.export_for_agent(self.store, self.tmp / "queue.txt", limit=20)
        text = out.read_text(encoding="utf-8")
        self.assertIn(item.id, text)
        self.assertIn("N25", text)
        # 紧凑文本回写仍走 submit → SQLite
        result = ar.import_judgments_from_text(
            self.store, f"id: {item.id}\ndecision: accept\nvalue: N25\nrationale: 简称\ngeneralize: pair\n")
        self.assertEqual(result["accepted"], 1)
        self.assertEqual(ar.load_queue(self.store), [])
        with dbstore.connect(self.store) as conn:
            count = conn.execute("SELECT COUNT(*) FROM review_decisions").fetchone()[0]
        self.assertEqual(count, 1)

    def test_stats_reflect_sqlite_queue(self):
        items = [
            ar.make_review_item("ニーゴ", "en", ["N25"], kind="conflict"),
            ar.make_review_item("セカイ", "en", ["Sekai"], kind="gate_failed"),
        ]
        ar.enqueue(self.store, items)
        stats = ar.review_stats(self.store)
        self.assertEqual(stats["queue_size"], 2)
        self.assertEqual(stats["queue_by_kind"], {"conflict": 1, "gate_failed": 1})
        ar.submit_judgments(self.store, [
            {"id": items[0].id, "decision": "accept", "value": "N25", "rationale": "r",
             "generalize": "pair"}])
        stats = ar.review_stats(self.store)
        self.assertEqual(stats["queue_size"], 1)


if __name__ == "__main__":
    unittest.main()
