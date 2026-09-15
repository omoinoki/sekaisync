"""``sekaisync.agent_review`` 的单元测试（零依赖、只用临时目录、不碰真实 store）。

覆盖需求里的 9 项验收：
1. 入队去重；2. 读取与导出；3. 提交判断；4. 方法论复用与 hits；5. 幂等；
6. 紧凑格式解析（含报错行号）；7. mtime 失效；外加 pattern 泛化闸门与退役接口。
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from sekaisync import agent_review as ar


class AgentReviewTestBase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.store = self.tmp / "store"

    def tearDown(self) -> None:
        self._tmp.cleanup()

    # ── 工具 ──────────────────────────────────────────────────────────

    def _sample_items(self) -> list[ar.ReviewItem]:
        """三条混合语言队列项：日文→英、中文→英、片假名→英。"""
        return [
            ar.make_review_item(
                "ニーゴ",
                "en",
                ["N25", "Huh"],
                kind="conflict",
                chosen_hint="N25",
                evidence=[
                    "桐谷遥：ニーゴのみんな、集まってくれてありがとう",
                    "え？ニーゴって25時、ナイトコードで。の略称なの？",
                ],
                story_keys=["web:sekai_best:zh-cn:card_story:2"],
                channels=["C", "A"],
                reason="通道 C 与通道 A 对同一术语给出不同候选，需要裁决",
            ),
            ar.make_review_item(
                "星乃一歌",
                "en",
                ["Hoshino Ichika", "Ichika Hoshino"],
                kind="pending",
                chosen_hint="Hoshino Ichika",
                evidence=["星乃一歌はバンド『Leo/need』のギターボーカル。"],
                story_keys=["web:sekai_best:zh-cn:event_story:12"],
                channels=["A"],
                reason="低置信：两种姓名序都出现过，无法靠分布区分",
            ),
            ar.make_review_item(
                "セカイ",
                "en",
                ["Sekai"],
                kind="gate_failed",
                evidence=["みんなでセカイを守ろう！"],
                story_keys=["web:sekai_best:zh-cn:main_story:1"],
                channels=["B"],
                reason="罗马音门控未过：セカイ是通用词，无专属译名",
            ),
        ]


class QueueTests(AgentReviewTestBase):
    def test_queue_paths_under_kb_terms(self):
        self.assertEqual(
            ar.queue_path(self.store),
            self.store / "kb" / "terms" / "review_queue.json",
        )
        self.assertEqual(
            ar.methodology_path(self.store),
            self.store / "kb" / "terms" / "methodology.json",
        )
        # kb 是唯一数据层：不得写进可再生成的 cache/
        self.assertNotIn("cache", str(ar.queue_path(self.store)))

    def test_enqueue_dedupe_same_item_twice(self):
        item = self._sample_items()[0]
        first = ar.enqueue(self.store, [item])
        self.assertEqual(
            {k: first[k] for k in ("added", "skipped_dup", "skipped_settled", "queue_size")},
            {"added": 1, "skipped_dup": 0, "skipped_settled": 0, "queue_size": 1},
        )
        second = ar.enqueue(self.store, [item])
        self.assertEqual(second["added"], 0)
        self.assertEqual(second["skipped_dup"], 1)
        self.assertEqual(second["queue_size"], 1)

    def test_item_id_is_candidate_order_insensitive(self):
        a = ar.item_id("ニーゴ", "en", ["N25", "Huh"])
        b = ar.item_id("ニーゴ", "en", ["Huh", "N25"])
        self.assertEqual(a, b)
        self.assertNotEqual(a, ar.item_id("ニーゴ", "zh_hans", ["N25", "Huh"]))

    def test_enqueue_dedupes_within_one_batch(self):
        item = self._sample_items()[0]
        result = ar.enqueue(self.store, [item, item])
        self.assertEqual(result["added"], 1)
        self.assertEqual(result["skipped_dup"], 1)

    def test_load_queue_order_stable_and_filters(self):
        items = self._sample_items()
        ar.enqueue(self.store, items)
        loaded = ar.load_queue(self.store)
        self.assertEqual([x.id for x in loaded], [x.id for x in items])

        limited = ar.load_queue(self.store, limit=2)
        self.assertEqual([x.id for x in limited], [x.id for x in items[:2]])

        conflicts = ar.load_queue(self.store, kind="conflict")
        self.assertEqual(len(conflicts), 1)
        self.assertEqual(conflicts[0].kind, "conflict")

    def test_dedupes_candidates_and_caps_evidence(self):
        item = ar.make_review_item(
            "テスト語",
            "en",
            ["A", "A", "B", " "],
            evidence=["x" * 500, "b", "c", "d", "e"],
            story_keys=["s1", "s2", "s3", "s4"],
        )
        self.assertEqual(item.candidates, ["A", "B"])
        self.assertEqual(len(item.evidence), ar.EVIDENCE_MAX_ITEMS)
        self.assertEqual(len(item.evidence[0]), ar.EVIDENCE_MAX_CHARS)
        self.assertTrue(item.evidence[0].endswith("…"))
        self.assertEqual(len(item.story_keys), ar.STORY_KEYS_MAX)


class SubmitTests(AgentReviewTestBase):
    def _seed(self) -> list[ar.ReviewItem]:
        items = self._sample_items()
        ar.enqueue(self.store, items)
        return items

    def test_submit_accept_reject_replace(self):
        items = self._seed()
        result = ar.submit_judgments(
            self.store,
            [
                {
                    "id": items[0].id,
                    "decision": "accept",
                    "value": "N25",
                    "rationale": "ニーゴ 是 25時、ナイトコードで。的简称，官方英文名 N25",
                    "confidence": 0.9,
                    "generalize": "pair",
                    "agent": "claude-code",
                    "session": "sess-1",
                },
                {
                    "id": items[1].id,
                    "decision": "replace",
                    "value": "Ichika Hoshino",
                    "rationale": "官方英文名采用名前姓后，与 master_db 一致",
                    "confidence": 0.85,
                    "generalize": "pair",
                },
                {
                    "id": items[2].id,
                    "decision": "reject",
                    "rationale": "セカイ 在该语境是通用词「世界」，没有专属译名",
                    "confidence": 0.7,
                    "generalize": "pair",
                },
            ],
        )
        self.assertEqual(result["accepted"], 1)
        self.assertEqual(result["rejected"], 1)
        self.assertEqual(result["replaced"], 1)
        self.assertEqual(result["errors"], [])
        # 三条裁决 → 三条方法论（accept 1 + replace 1 + reject 1，各自 pair 级）
        self.assertEqual(result["methodology_added"], 3)
        self.assertEqual(result["remaining"], 0)
        self.assertEqual(ar.load_queue(self.store), [])

        entries = ar.load_methodology(self.store)
        kinds = sorted(e.kind for e in entries)
        self.assertEqual(kinds, ["pair_accept", "pair_accept", "pair_reject"])
        by_key = {e.key: e for e in entries}
        self.assertEqual(by_key["ニーゴ|en|N25"].value, "N25")
        self.assertEqual(by_key["ニーゴ|en|N25"].provenance["agent"], "claude-code")
        self.assertEqual(by_key["ニーゴ|en|N25"].provenance["session"], "sess-1")
        self.assertIn("25時", by_key["ニーゴ|en|N25"].rationale)
        self.assertIn("星乃一歌|en|Ichika Hoshino", by_key)
        self.assertIn("セカイ|en|Sekai", by_key)

    def test_reject_single_candidate_writes_one_entry(self):
        """单候选 reject → 恰好 1 条方法论（"三次裁决三条方法论"的口径）。"""
        item = ar.make_review_item("セカイ", "en", ["Sekai"], kind="gate_failed")
        ar.enqueue(self.store, [item])
        result = ar.submit_judgments(
            self.store,
            [
                {
                    "id": item.id,
                    "decision": "reject",
                    "rationale": "通用词，无专属译名",
                    "generalize": "pair",
                }
            ],
        )
        self.assertEqual(result["methodology_added"], 1)
        self.assertEqual(ar.load_methodology(self.store)[0].kind, "pair_reject")

    def test_accept_without_value_falls_back_to_hint(self):
        items = self._seed()
        result = ar.submit_judgments(
            self.store,
            [
                {
                    "id": items[0].id,
                    "decision": "accept",
                    "rationale": "用 hint",
                    "generalize": "pair",
                }
            ],
        )
        self.assertEqual(result["accepted"], 1)
        by_key = {e.key: e for e in ar.load_methodology(self.store)}
        self.assertEqual(by_key["ニーゴ|en|N25"].value, "N25")

    def test_accept_without_value_or_hint_falls_back_to_first_candidate(self):
        item = ar.make_review_item("テスト語", "en", ["First", "Second"], kind="pending")
        ar.enqueue(self.store, [item])
        ar.submit_judgments(
            self.store,
            [
                {
                    "id": item.id,
                    "decision": "accept",
                    "rationale": "无 hint 时取 candidates[0]",
                    "generalize": "pair",
                }
            ],
        )
        self.assertEqual(ar.load_methodology(self.store)[0].key, "テスト語|en|First")

    def test_replace_requires_value(self):
        items = self._seed()
        result = ar.submit_judgments(
            self.store, [{"id": items[0].id, "decision": "replace", "rationale": "忘了给值"}]
        )
        self.assertEqual(result["replaced"], 0)
        self.assertTrue(result["errors"])
        self.assertEqual(len(ar.load_queue(self.store)), 3)  # 未被消费

    def test_reject_specific_candidates_only(self):
        items = self._seed()
        ar.submit_judgments(
            self.store,
            [
                {
                    "id": items[0].id,
                    "decision": "reject",
                    "candidates": ["Huh"],
                    "rationale": "Huh 是感叹词误配",
                    "generalize": "pair",
                }
            ],
        )
        keys = {e.key for e in ar.load_methodology(self.store)}
        self.assertEqual(keys, {"ニーゴ|en|Huh"})

    def test_no_generalize_writes_no_methodology(self):
        items = self._seed()
        ar.submit_judgments(
            self.store,
            [{"id": items[0].id, "decision": "accept", "value": "N25", "rationale": "一次性"}],
        )
        self.assertEqual(ar.load_methodology(self.store), [])
        # 队列只消费被裁决的那条，其余两条仍在
        remaining = [item.id for item in ar.load_queue(self.store)]
        self.assertEqual(remaining, [items[1].id, items[2].id])

    def test_idempotent_resubmit_produces_no_duplicates(self):
        items = self._seed()
        judgment = {
            "id": items[0].id,
            "decision": "accept",
            "value": "N25",
            "rationale": "ニーゴ→N25",
            "generalize": "pair",
        }
        first = ar.submit_judgments(self.store, [judgment])
        count_after_first = len(ar.load_methodology(self.store))
        second = ar.submit_judgments(self.store, [judgment])
        self.assertEqual(first["methodology_added"], 1)
        self.assertEqual(second["methodology_added"], 0)
        self.assertEqual(second["accepted"], 0)
        self.assertEqual(len(ar.load_methodology(self.store)), count_after_first)

    def test_generalize_none_string_is_treated_as_empty(self):
        items = self._seed()
        ar.submit_judgments(
            self.store,
            [
                {
                    "id": items[0].id,
                    "decision": "accept",
                    "value": "N25",
                    "generalize": "null",
                }
            ],
        )
        self.assertEqual(ar.load_methodology(self.store), [])


class ReuseTests(AgentReviewTestBase):
    def _settle_n25(self) -> ar.ReviewItem:
        item = self._sample_items()[0]
        ar.enqueue(self.store, [item])
        ar.submit_judgments(
            self.store,
            [
                {
                    "id": item.id,
                    "decision": "accept",
                    "value": "N25",
                    "rationale": "ニーゴ 官方英文名 N25",
                    "generalize": "pair",
                    "agent": "zcode",
                }
            ],
        )
        return item

    def test_enqueue_skips_already_settled(self):
        item = self._settle_n25()
        result = ar.enqueue(self.store, [item])
        self.assertEqual(result["added"], 0)
        self.assertEqual(result["skipped_settled"], 1)
        self.assertEqual(result["queue_size"], 0)

    def test_consult_returns_decision_and_counts_hits(self):
        self._settle_n25()
        for expected in (1, 2, 3):
            hit = ar.consult(self.store, "ニーゴ", "en", ["N25", "Huh"])
            self.assertIsNotNone(hit)
            self.assertEqual(hit["decision"], "accept")
            self.assertEqual(hit["value"], "N25")
            self.assertEqual(hit["entry"]["kind"], "pair_accept")
            hits = [e.hits for e in ar.load_methodology(self.store)][0]
            self.assertEqual(hits, expected)

    def test_consult_miss_returns_none_and_does_not_write(self):
        self._settle_n25()
        self.assertIsNone(ar.consult(self.store, "未知术语", "en", ["X"]))
        path = ar.methodology_path(self.store)
        before = path.read_text(encoding="utf-8")
        ar.consult(self.store, "未知术语", "en", ["X"])
        self.assertEqual(path.read_text(encoding="utf-8"), before)

    def test_consult_reject_only_when_all_candidates_rejected(self):
        item = ar.make_review_item("セカイ", "en", ["Sekai", "World"], kind="gate_failed")
        ar.enqueue(self.store, [item])
        ar.submit_judgments(
            self.store,
            [{"id": item.id, "decision": "reject", "rationale": "通用词", "generalize": "pair"}],
        )
        hit = ar.consult(self.store, "セカイ", "en", ["Sekai", "World"])
        self.assertEqual(hit["decision"], "reject")
        self.assertEqual(hit["rejected"], ["Sekai", "World"])
        self.assertEqual(hit["value"], "")
        # 只否掉一个候选时，剩下的没结论 → 不猜，交回智能体
        partial = ar.make_review_item("セカイ", "en", ["Sekai"], kind="gate_failed")
        ar2 = self.tmp / "store2"
        ar.enqueue(ar2, [partial])
        ar.submit_judgments(
            ar2,
            [
                {
                    "id": partial.id,
                    "decision": "reject",
                    "candidates": ["Sekai"],
                    "rationale": "通用词",
                    "generalize": "pair",
                }
            ],
        )
        self.assertIsNotNone(ar.consult(ar2, "セカイ", "en", ["Sekai"]))
        self.assertIsNone(ar.consult(ar2, "セカイ", "en", ["Sekai", "World"]))

    def test_apply_methodology_batch(self):
        self._settle_n25()
        result = ar.apply_methodology_batch(
            self.store,
            {"ニーゴ": {"en": ["N25", "Huh"]}, "新規": {"en": ["A", "B"]}},
        )
        self.assertEqual(result["settled"], {"ニーゴ": {"en": "N25"}})
        self.assertEqual(result["rejected"], [])
        self.assertEqual(len(result["unresolved"]), 1)
        self.assertEqual(result["unresolved"][0]["term"], "新規")
        self.assertEqual(result["consulted"], 2)

    def test_consult_survives_corrupt_methodology_file(self):
        self._settle_n25()
        ar.methodology_path(self.store).write_text("{ not json", encoding="utf-8")
        self.assertIsNone(ar.consult(self.store, "ニーゴ", "en", ["N25"]))


class PatternTests(AgentReviewTestBase):
    def test_pattern_generalize_and_scope(self):
        item = ar.make_review_item("プロセカ", "en", ["PJSK"], kind="pending")
        ar.enqueue(self.store, [item])
        result = ar.submit_judgments(
            self.store,
            [
                {
                    "id": item.id,
                    "decision": "accept",
                    "value": "PJSK",
                    "rationale": "PJ 开头的缩写形如 PJSK，可直接采纳",
                    "generalize": "pattern",
                    "pattern": "prefix:PJ",
                    "scope": {"lang": "en"},
                }
            ],
        )
        self.assertEqual(result["methodology_added"], 1)
        entry = ar.load_methodology(self.store)[0]
        self.assertEqual(entry.kind, "pattern_accept")
        self.assertEqual(entry.provenance["scope"], {"lang": "en"})

        # 作用域内的其它术语命中
        self.assertEqual(ar.consult(self.store, "新規", "en", ["PJX"])["value"], "PJSK")
        # 记录命中范围（审计用）
        self.assertEqual(ar.load_methodology(self.store)[0].provenance["hit_terms"], ["新規"])
        # 语言作用域外不命中
        self.assertIsNone(ar.consult(self.store, "新規", "zh_hans", ["PJX"]))

    def test_pattern_requires_explicit_pattern(self):
        item = ar.make_review_item("プロセカ", "en", ["PJSK"], kind="pending")
        ar.enqueue(self.store, [item])
        result = ar.submit_judgments(
            self.store,
            [
                {
                    "id": item.id,
                    "decision": "accept",
                    "value": "PJSK",
                    "generalize": "pattern",
                }
            ],
        )
        self.assertEqual(result["methodology_added"], 0)
        self.assertTrue(any("pattern" in err for err in result["errors"]))

    def test_overbroad_patterns_rejected(self):
        item = ar.make_review_item("プロセカ", "en", ["PJSK"], kind="pending")
        ar.enqueue(self.store, [item])
        result = ar.submit_judgments(
            self.store,
            [
                {
                    "id": item.id,
                    "decision": "accept",
                    "value": "PJSK",
                    "rationale": "太宽",
                    "generalize": "pattern",
                    "pattern": "re:.*",
                }
            ],
        )
        self.assertEqual(result["methodology_added"], 0)
        self.assertTrue(any("泛化" in err for err in result["errors"]))

    def test_validate_pattern(self):
        self.assertFalse(ar.validate_pattern("")[0])
        self.assertFalse(ar.validate_pattern("re:.*")[0])
        self.assertFalse(ar.validate_pattern("N")[0])
        self.assertFalse(ar.validate_pattern("^N[0-9]+$")[0])  # 疑似正则缺前缀
        self.assertTrue(ar.validate_pattern("prefix:PJ")[0])
        self.assertTrue(ar.validate_pattern(r"re:^N\d{1,3}$")[0])
        # 探测集命中比例过高 → 判为泛化过头
        ok, reason = ar.validate_pattern("prefix:Sh", probes=["Shinonome", "Shirasagi", "N25"])
        self.assertFalse(ok)
        self.assertIn("探测集", reason)

    def test_pattern_mapping_rule_maps_every_match_to_value(self):
        """pattern_accept 带 value = 映射规则：命中形态的候选一律映射到 value。"""
        item = ar.make_review_item("プロセカ", "en", ["PJSK"], kind="pending")
        ar.enqueue(self.store, [item])
        ar.submit_judgments(
            self.store,
            [
                {
                    "id": item.id,
                    "decision": "accept",
                    "value": "PJSK",
                    "rationale": "PJ 开头的缩写统一写 PJSK",
                    "generalize": "pattern",
                    "pattern": r"re:^PJ[A-Z]*$",
                    "scope": {"lang": "en"},
                }
            ],
        )
        self.assertEqual(ar.load_methodology(self.store)[0].value, "PJSK")
        self.assertEqual(ar.consult(self.store, "新規", "en", ["PJX"])["value"], "PJSK")

    def test_pattern_form_rule_accepts_candidate_itself(self):
        """pattern_accept 的 value 为空 = 形态规则：候选本身可采纳，不套用别的值。"""
        path = ar.methodology_path(self.store)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(
                {
                    "version": 1,
                    "entries": [
                        {
                            "kind": "pattern_accept",
                            "key": r"re:^[A-Z]{2,6}$",
                            "value": "",
                            "rationale": "全大写 2-6 字母的缩写形可直接采纳",
                            "provenance": {
                                "agent": "human",
                                "session": "",
                                "ts": ar.now_iso(),
                                "scope": {"lang": "en"},
                            },
                            "hits": 0,
                        }
                    ],
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        hit = ar.consult(self.store, "プロセカ", "en", ["PJSK"])
        self.assertEqual(hit["decision"], "accept")
        self.assertEqual(hit["value"], "PJSK")  # 取候选本身，而不是别的术语的值
        self.assertIsNone(ar.consult(self.store, "プロセカ", "en", ["Pjsk"]))  # 形态不符

    def test_retire_methodology_entry(self):
        item = ar.make_review_item("プロセカ", "en", ["PJSK"], kind="pending")
        ar.enqueue(self.store, [item])
        ar.submit_judgments(
            self.store,
            [
                {
                    "id": item.id,
                    "decision": "accept",
                    "value": "PJSK",
                    "rationale": "缩写",
                    "generalize": "pair",
                }
            ],
        )
        self.assertIsNotNone(ar.consult(self.store, "プロセカ", "en", ["PJSK"]))
        self.assertTrue(ar.retire_methodology_entry(self.store, "プロセカ|en|PJSK", reason="误判"))
        self.assertIsNone(ar.consult(self.store, "プロセカ", "en", ["PJSK"]))
        # 软删除：审计痕迹仍在
        entry = ar.load_methodology(self.store)[0]
        self.assertTrue(entry.retired)
        self.assertEqual(entry.provenance["retired_reason"], "误判")
        self.assertFalse(ar.retire_methodology_entry(self.store, "不存在的key"))


class ExportImportTests(AgentReviewTestBase):
    def test_export_is_compact_and_self_contained(self):
        ar.enqueue(self.store, self._sample_items())
        out = ar.export_for_agent(self.store, self.tmp / "queue.txt", limit=20)
        self.assertTrue(out.exists())
        text = out.read_text(encoding="utf-8")
        for item in ar.load_queue(self.store):
            self.assertIn(item.id, text)
            self.assertIn(item.term, text)
            self.assertIn(item.candidates[0], text)
            self.assertIn(item.reason[:20], text)
        # 每条 5-8 行（`#` 注释头不算；`## ` 是队列项标题，要算）
        body = [
            line
            for line in text.splitlines()
            if line.strip() and not (line.startswith("#") and not line.startswith("## "))
        ]
        raw = ar.load_queue(self.store)
        blocks: list[list[str]] = []
        for line in body:
            if line.startswith("## "):
                blocks.append([line])
            elif blocks:
                blocks[-1].append(line)
        self.assertEqual(len(blocks), len(raw))
        for block in blocks:
            self.assertGreaterEqual(len(block), 5)
            self.assertLessEqual(len(block), 8)

    def test_export_limit(self):
        ar.enqueue(self.store, self._sample_items())
        out = ar.export_for_agent(self.store, self.tmp / "queue.txt", limit=2)
        text = out.read_text(encoding="utf-8")
        self.assertEqual(sum(1 for line in text.splitlines() if line.startswith("## ")), 2)

    def test_import_yamlish_text(self):
        items = self._sample_items()
        ar.enqueue(self.store, items[:2])
        text = "\n".join(
            [
                "# 智能体的判断",
                f"id: {items[0].id}",
                "decision: accept",
                "value: N25",
                "rationale: ニーゴ 是 25時、ナイトコードで。的简称，官方英文名 N25",
                "confidence: 0.9",
                "generalize: pair",
                "",
                f"id: {items[1].id}",
                "decision: reject",
                "rationale: 两种姓名序都不确定，先否掉",
                "confidence: 0.5",
            ]
        )
        result = ar.import_judgments_from_text(self.store, text)
        self.assertEqual(result["parsed"], 2)
        self.assertEqual(result["accepted"], 1)
        self.assertEqual(result["rejected"], 1)
        self.assertEqual(ar.load_queue(self.store), [])
        # 第二条没写 generalize → 只消费队列，不沉淀方法论
        keys = {e.key for e in ar.load_methodology(self.store)}
        self.assertEqual(keys, {"ニーゴ|en|N25"})

    def test_import_jsonl_and_fenced_code(self):
        items = self._sample_items()
        ar.enqueue(self.store, items)
        text = "\n".join(
            [
                "```json",
                json.dumps(
                    {
                        "id": items[0].id,
                        "decision": "accept",
                        "value": "N25",
                        "rationale": "简称",
                        "generalize": "pair",
                    },
                    ensure_ascii=False,
                ),
                json.dumps(
                    {
                        "id": items[1].id,
                        "decision": "replace",
                        "value": "Ichika Hoshino",
                        "rationale": "名前姓后",
                        "generalize": "pair",
                    },
                    ensure_ascii=False,
                ),
                "```",
            ]
        )
        result = ar.import_judgments_from_text(self.store, text)
        self.assertEqual(result["parsed"], 2)
        self.assertEqual(result["accepted"], 1)
        self.assertEqual(result["replaced"], 1)

    def test_import_reports_line_number_on_error(self):
        items = self._sample_items()
        ar.enqueue(self.store, items[:1])
        with self.assertRaises(ar.ReviewFormatError) as ctx:
            ar.import_judgments_from_text(
                self.store,
                "\n".join(
                    [
                        "# 注释",
                        f"id: {items[0].id}",
                        "decision: accept",
                        "value: N25",
                        "これは説明のつもりだけど記号が無い行",
                    ]
                ),
            )
        message = str(ctx.exception)
        self.assertIn("line 5", message)

        with self.assertRaises(ar.ReviewFormatError) as ctx2:
            ar.import_judgments_from_text(self.store, f"id: {items[0].id}\ndecision: maybe\n")
        self.assertIn("line 1", str(ctx2.exception))
        self.assertIn("maybe", str(ctx2.exception))

        with self.assertRaises(ar.ReviewFormatError) as ctx3:
            ar.import_judgments_from_text(self.store, "decision: accept\n")
        self.assertIn("id", str(ctx3.exception))

    def test_import_does_not_partially_apply_on_error(self):
        items = self._sample_items()
        ar.enqueue(self.store, items[:2])
        with self.assertRaises(ar.ReviewFormatError):
            ar.import_judgments_from_text(
                self.store,
                "\n".join(
                    [
                        f"id: {items[0].id}",
                        "decision: accept",
                        "value: N25",
                        "不合法的一行",
                    ]
                ),
            )
        self.assertEqual(len(ar.load_queue(self.store)), 2)
        self.assertEqual(ar.load_methodology(self.store), [])


class MtimeInvalidationTests(AgentReviewTestBase):
    def _write_handwritten_entry(self, key: str, value: str) -> None:
        """模拟"另一个进程/人工"直接改 methodology.json（不经过本模块的写接口）。"""
        path = ar.methodology_path(self.store)
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {"entries": []}
        payload.setdefault("entries", []).append(
            {
                "kind": "pair_accept",
                "key": key,
                "value": value,
                "rationale": "人工补录",
                "provenance": {"agent": "human", "session": "", "ts": ar.now_iso()},
                "hits": 0,
            }
        )
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    def test_consult_picks_up_handwritten_entry_without_prior_write(self):
        """纯 mtime 失效：只读路径也要发现外部改动（回归：曾因不校验 stamp 而漏）。"""
        # 先用一次未命中把空缓存烤热（这一步不写任何文件）
        self.assertIsNone(ar.consult(self.store, "ブシドー", "en", ["Bushido"]))
        self._write_handwritten_entry("ブシドー|en|Bushido", "Bushido")
        hit = ar.consult(self.store, "ブシドー", "en", ["Bushido"])
        self.assertIsNotNone(hit)
        self.assertEqual(hit["value"], "Bushido")

    def test_consult_sees_handwritten_new_entry(self):
        first = ar.make_review_item("ニーゴ", "en", ["N25"], kind="pending")
        ar.enqueue(self.store, [first])
        ar.submit_judgments(
            self.store,
            [
                {
                    "id": first.id,
                    "decision": "accept",
                    "value": "N25",
                    "rationale": "简称",
                    "generalize": "pair",
                }
            ],
        )
        self.assertIsNotNone(ar.consult(self.store, "ニーゴ", "en", ["N25"]))

        # 手工追加一条 pair_accept（模拟别的进程/人工编辑）
        path = ar.methodology_path(self.store)
        payload = json.loads(path.read_text(encoding="utf-8"))
        payload["entries"].append(
            {
                "kind": "pair_accept",
                "key": "ブシドー|en|Bushido",
                "value": "Bushido",
                "rationale": "人工补录",
                "provenance": {"agent": "human", "session": "", "ts": ar.now_iso()},
                "hits": 0,
            }
        )
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

        hit = ar.consult(self.store, "ブシドー", "en", ["Bushido"])
        self.assertIsNotNone(hit)
        self.assertEqual(hit["value"], "Bushido")

    def test_cached_entry_not_stale_after_submit(self):
        item = ar.make_review_item("ニーゴ", "en", ["N25"], kind="pending")
        ar.enqueue(self.store, [item])
        self.assertIsNone(ar.consult(self.store, "ニーゴ", "en", ["N25"]))  # 预热空缓存
        ar.submit_judgments(
            self.store,
            [
                {
                    "id": item.id,
                    "decision": "accept",
                    "value": "N25",
                    "rationale": "简称",
                    "generalize": "pair",
                }
            ],
        )
        self.assertIsNotNone(ar.consult(self.store, "ニーゴ", "en", ["N25"]))


class StatsTests(AgentReviewTestBase):
    def test_review_stats_shape(self):
        items = self._sample_items()
        ar.enqueue(self.store, items)
        stats = ar.review_stats(self.store)
        self.assertEqual(stats["queue_size"], 3)
        self.assertEqual(stats["queue_by_kind"], {"conflict": 1, "pending": 1, "gate_failed": 1})
        self.assertEqual(stats["methodology_entries"], 0)
        self.assertEqual(stats["interventions"]["reuse_rate"], 0.0)

        ar.submit_judgments(
            self.store,
            [
                {
                    "id": item.id,
                    "decision": "accept",
                    "value": item.candidates[0],
                    "rationale": "测试",
                    "generalize": "pair",
                }
                for item in items
            ],
        )
        ar.consult(self.store, "ニーゴ", "en", ["N25"])
        ar.consult(self.store, "ニーゴ", "en", ["N25"])
        stats = ar.review_stats(self.store)
        self.assertEqual(stats["queue_size"], 0)
        self.assertEqual(stats["methodology_entries"], 3)
        self.assertEqual(stats["interventions"]["reuse_total"], 2)
        self.assertEqual(stats["interventions"]["reuse_rate"], round(2 / 3, 3))
        self.assertEqual(stats["top_hits"][0]["key"], "ニーゴ|en|N25")
        self.assertEqual(stats["top_hits"][0]["hits"], 2)
        self.assertEqual(len(stats["interventions"]["by_day"]), 1)


class CliTests(AgentReviewTestBase):
    def _run(self, argv: list[str]) -> int:
        return ar.main(argv)

    def test_cli_roundtrip(self):
        import io
        from contextlib import redirect_stdout

        items = self._sample_items()
        ar.enqueue(self.store, items)
        store = str(self.store)

        buf = io.StringIO()
        with redirect_stdout(buf):
            code = self._run(["list", "--store", store, "--limit", "1"])
        self.assertEqual(code, 0)
        self.assertIn(items[0].id, buf.getvalue())

        out = self.tmp / "queue.txt"
        with redirect_stdout(io.StringIO()):
            code = self._run(["export", "--store", store, "--out", str(out), "--limit", "2"])
        self.assertEqual(code, 0)
        self.assertTrue(out.exists())

        judgments = self.tmp / "judgments.json"
        judgments.write_text(
            json.dumps(
                [
                    {
                        "id": items[0].id,
                        "decision": "accept",
                        "value": "N25",
                        "rationale": "简称",
                        "generalize": "pair",
                    }
                ],
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        buf = io.StringIO()
        with redirect_stdout(buf):
            code = self._run(["submit", "--store", store, "--file", str(judgments)])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(buf.getvalue())["accepted"], 1)

        with redirect_stdout(io.StringIO()) as buf2:
            code = self._run(["stats", "--store", store])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(buf2.getvalue())["queue_size"], 2)

        buf3 = io.StringIO()
        with redirect_stdout(buf3):
            code = self._run(["methodology", "--store", store])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(buf3.getvalue())["count"], 1)

    def test_cli_submit_text_and_format_error_exit_code(self):
        import io
        from contextlib import redirect_stderr, redirect_stdout

        items = self._sample_items()
        ar.enqueue(self.store, items[:1])
        text = f"id: {items[0].id}\ndecision: accept\nvalue: N25\nrationale: 简称\ngeneralize: pair\n"
        buf = io.StringIO()
        with redirect_stdout(buf):
            code = self._run(["submit", "--store", str(self.store), "--text", text])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(buf.getvalue())["accepted"], 1)

        err = io.StringIO()
        with redirect_stderr(err):
            code = self._run(["submit", "--store", str(self.store), "--text", "这不是判断"])
        self.assertEqual(code, 2)
        self.assertIn("line 1", err.getvalue())


if __name__ == "__main__":
    unittest.main()
