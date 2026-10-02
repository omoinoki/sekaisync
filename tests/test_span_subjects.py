"""Typed span subjects remain raw, occurrence-specific and sense-independent."""
from copy import deepcopy
import hashlib
from pathlib import Path
import tempfile
import unittest

from sekaisync import agent_packets as ap, dbstore, occurrence_store as ledger, span_subjects as subjects, termindex as ti


# Transcript fixtures preserve the six non-whitespace gaps from event:70:6.
_REAL_TURNS = (
    ("ja", "穂波：それに、その人に教えてもらったおかげで、\n自分の絵に自信がついたんです。\nだから、前よりも恥ずかしくなくて……",
     ("自信", "ついた"), "が", "自信がついた"),
    ("zh_hans", "穗波：而且，因为有了那个人的指导，\n我对自己的画有了一点信心。\n所以，我不再像以前那样不敢在人前画画了……",
     ("有了", "信心"), "一点", "有了一点信心"),
    ("ko", "호나미：그리고 그분이 가르쳐 준 덕분에\n스스로의 그림에 자신감이 붙었어요.\n그래서 전보다 부끄럽게 느껴지지도 않아서……",
     ("자신감", "붙었어요"), "이 ", "자신감이 붙었어요"),
    ("en", "Futaba: Also, I have to think that you lending her a hand the way you did is going to do wonders for her.",
     ("lending", "a hand"), " her ", "lending her a hand"),
    ("ja", "二葉：そしたら、なんとなくうまくなった気がしたんだけど、\nなんだか段々描く気力が落ちていって……。\nそんな時、雪平先生が言ってくれたの",
     ("描く気力", "落ちていって"), "が", "描く気力が落ちていって"),
    ("zh_hans", "二叶：那之后，虽然我能感觉自己在不断进步，\n但画画的动力却在逐渐消退……\n就在那时，雪平老师对我说了一句话。",
     ("画画的动力", "逐渐消退"), "却在", "画画的动力却在逐渐消退"),
)


class SpanSubjectTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = Path(self.temp.name) / "store"
        dbstore.initialize(self.store)

    def page(self, text, language="en", page_id=None):
        page = dict(id=page_id or f"web:fixture:{language}:event_story:999:1", source="fixture",
                    kind="event_story", language=language, trust="B", text=text,
                    canonical_key=f"event_story:{language}:999:1")
        dbstore.upsert_web_pages(self.store, "fixture", [page])
        return page

    def view(self, page, start=0, end=None):
        text = page["text"]
        end = len(text) if end is None else end
        return dict(source=page["source"], page_id=page["id"], language=page["language"],
                    sha256=hashlib.sha256(text.encode("utf-8")).hexdigest(),
                    start=start, end=end, text=text[start:end], complete=True)

    def segments(self, page, fragments, start=0):
        parts = []
        for exact in fragments:
            start = page["text"].index(exact, start)
            end = start + len(exact)
            parts.append(dict(start=start, end=end, exact=exact))
            start = end
        return parts

    def validate(self, subject):
        with dbstore.connect(self.store) as conn:
            before = conn.total_changes
            result = subjects._validate(conn, subject)
            self.assertEqual(conn.total_changes, before)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM terms").fetchone()[0], 0)
        return result

    def test_six_real_discontinuous_expressions_preserve_all_raw_fragments_and_gaps(self):
        for language, text, fragments, gap, envelope in _REAL_TURNS:
            with self.subTest(language=language, envelope=envelope):
                page = self.page(text, language)
                selected = self.segments(page, fragments, text.index(envelope))
                subject = subjects._segmented(self.view(page), "event:999:1", selected)
                self.assertEqual(subject["canonical_parts"], list(fragments))
                self.assertEqual(subject["canonical"], " ".join(fragments))
                self.assertEqual(subject["gap_text"], [gap])
                self.assertEqual(subject["source"]["segments"], selected)
                self.assertEqual(self.validate(subject), subject)
                with self.assertRaisesRegex(ValueError, "literal subject"):
                    subjects._literal(self.view(page), "event:999:1", envelope, selected)

    def test_two_real_transcript_fixtures_validate_from_actual_packet_windows(self):
        for index in (0, 3):
            language, text, fragments, gap, envelope = _REAL_TURNS[index]
            with self.subTest(language=language):
                page = self.page(text, language)
                target_language = "en" if language != "en" else "zh_hans"
                target = self.page("Speaker: They offered a helping hand." if target_language == "en"
                                   else "甲：伸出了援手。", target_language)
                groups = ti.group_pages_by_story([page, target])
                items, _ = ap._prepare_scrub_review(self.store, groups, sorted(groups), [envelope], {},
                                                   language, [target_language])
                item = next(item for item in items if item._context["task"] == "translation")
                row = item._context["rows"][0]
                selected = self.segments(page, fragments, text.index(envelope))
                subject = subjects._segmented(row["source"], row["story_key"], selected)
                self.assertEqual(subject["gap_text"], [gap])
                self.assertEqual(self.validate(subject), subject)
                self.assertFalse(ap._term_selection(row["source"], envelope, selected, case_sensitive=True))

    def test_literal_and_segmented_identities_cannot_mix_when_display_text_agrees(self):
        page = self.page("A: build confidence")
        parts = self.segments(page, ("build", "confidence"))
        literal = subjects._literal(self.view(page), "event:999:1", "build confidence", parts)
        segmented = subjects._segmented(self.view(page), "event:999:1", parts)
        self.assertEqual(literal["canonical"], segmented["canonical"])
        self.assertEqual(literal["source"], segmented["source"])
        self.assertNotEqual(literal["id"], segmented["id"])
        self.assertTrue(literal["id"].startswith("subject:literal:"))
        self.assertTrue(segmented["id"].startswith("subject:segmented:"))
        self.validate(literal)
        self.validate(segmented)

    def test_same_display_different_gap_occurrence_case_and_page_are_separate_subjects(self):
        page = self.page("A: lending her a hand; lending him a hand; Lending her a hand.")
        view = self.view(page)
        first = subjects._segmented(view, "event:999:1", self.segments(page, ("lending", "a hand")))
        second = subjects._segmented(view, "event:999:1", self.segments(page, ("lending", "a hand"),
                                                                        page["text"].index("lending him")))
        upper = subjects._segmented(view, "event:999:1", self.segments(page, ("Lending", "a hand")))
        another_page = self.page(page["text"], page_id="web:another:en:event_story:999:1")
        another = subjects._segmented(self.view(another_page), "event:999:1",
                                      self.segments(another_page, ("lending", "a hand")))
        self.assertEqual(first["canonical"], second["canonical"])
        self.assertNotEqual(first["gap_text"], second["gap_text"])
        self.assertEqual(len({subject["id"] for subject in (first, second, upper, another)}), 4)

    def test_soft_wrap_parenthetical_gap_and_non_bmp_offsets_are_raw_code_points(self):
        text = "A: 😀 We were lending\r\n(to her) a hand.\r\nB: Enough."
        page = self.page(text)
        parts = self.segments(page, ("lending", "a hand"))
        subject = subjects._segmented(self.view(page), "event:999:1", parts)
        self.assertEqual(subject["gap_text"], ["\r\n(to her) "])
        self.assertEqual(parts[0]["start"], len("A: 😀 We were "))
        self.assertEqual(self.validate(subject), subject)

    def test_soft_wrap_inside_one_raw_fragment_is_preserved(self):
        page = self.page("A: build\nconfidence and keep trying.")
        parts = self.segments(page, ("build\nconfidence", "keep trying"))
        subject = subjects._segmented(self.view(page), "event:999:1", parts)
        self.assertEqual(subject["canonical_parts"], ["build\nconfidence", "keep trying"])
        self.validate(subject)

    def test_body_only_clipped_window_can_span_soft_wrap_within_the_full_raw_turn(self):
        page = self.page("A: Introductory context.\nWe were lending\nher a hand today.\nB: Another turn.")
        view = self.view(page, start=page["text"].index("We were"), end=page["text"].index("\nB:"))
        parts = self.segments(page, ("lending", "a hand"))
        subject = subjects._segmented(view, "event:999:1", parts)
        self.assertEqual(subject["gap_text"], ["\nher "])
        self.validate(subject)

    def test_clipped_soft_wrap_window_cannot_cross_a_new_real_speaker_turn(self):
        for label in ("B:", "A:", "B﹕", "A︓"):
            with self.subTest(label=label):
                page = self.page("A: Introductory context.\nWe were lending\n" + label + " her a hand today.")
                view = self.view(page, start=page["text"].index("We were"))
                with self.assertRaisesRegex(ValueError, "utterance body"):
                    subjects._segmented(view, "event:999:1", self.segments(page, ("lending", "a hand")))

    def test_clipped_context_edges_may_cut_unselected_words_not_selected_fragments(self):
        page = self.page("A: 😀 We were lending\nher a hand today.\nB: Next.")
        parts = self.segments(page, ("lending", "a hand"))
        view = self.view(page, start=page["text"].index("We") + 1, end=page["text"].index("today") + 3)
        subject = subjects._segmented(view, "event:999:1", parts)
        self.validate(subject)
        for clipped in (self.view(page, start=parts[0]["start"] + 1, end=view["end"]),
                        self.view(page, start=view["start"], end=parts[-1]["end"] - 1)):
            with self.subTest(clipped=clipped), self.assertRaises(ValueError):
                subjects._segmented(clipped, "event:999:1", parts)

    def test_rehashed_clipped_speaker_subject_still_fails_full_page_body_validation(self):
        page = self.page("A: Introductory context.\nNarrator﹕ lending her a hand.")
        start = page["text"].index("rator")
        end = page["text"].index("﹕")
        view = self.view(page, start=start, end=end)
        subject = subjects._segmented(view, "event:999:1", self.segments(page, ("ra", "tor"), start))
        with self.assertRaisesRegex(ValueError, "utterance body"):
            self.validate(subject)

    def test_speaker_metadata_and_cross_speaker_or_repeated_speaker_turns_are_rejected(self):
        for text, fragments in (("A: lending her a hand.", ("A", "a hand")),
                                ("A: lending\nB: a hand.", ("lending", "a hand")),
                                ("A: lending\nA: a hand.", ("lending", "a hand"))):
            with self.subTest(text=text):
                page = self.page(text)
                with self.assertRaisesRegex(ValueError, "utterance body"):
                    subjects._segmented(self.view(page), "event:999:1", self.segments(page, fragments))

    def test_small_colon_speaker_label_cannot_be_borrowed_as_a_raw_fragment(self):
        page = self.page("Alice﹕ lending her a hand.")
        parts = self.segments(page, ("Alice", "a hand"))
        with self.assertRaisesRegex(ValueError, "utterance body"):
            subjects._segmented(self.view(page), "event:999:1", parts)

    def test_full_page_validation_catches_metadata_hidden_by_a_clipped_window(self):
        page = self.page("Narrator: lending her a hand.")
        start = page["text"].index("tor:")
        clipped = self.view(page, start=start + len("tor:"))
        # The local window presents body only; a forged window hiding a speaker
        # fragment must still fail when validation replays the full raw page.
        view = self.view(page, start=page["text"].index("rator"), end=page["text"].index(":"))
        parts = self.segments(page, ("ra", "tor"))
        forged = subjects._segmented(view, "event:999:1", parts)
        with self.assertRaisesRegex(ValueError, "utterance body"):
            self.validate(forged)
        valid = subjects._segmented(clipped, "event:999:1", self.segments(page, ("lending", "a hand")))
        self.validate(valid)

    def test_unordered_overlapping_out_of_range_fake_and_whitespace_fragments_are_rejected(self):
        page = self.page("A: lending her a hand.")
        valid = self.segments(page, ("lending", "a hand"))
        bad = [list(reversed(valid)), [valid[0], dict(start=valid[0]["start"] + 1,
                                                    end=valid[0]["end"], exact="ending")],
               [dict(valid[0], start=-1), valid[1]], [valid[0], dict(valid[1], end=len(page["text"]) + 1)],
               [dict(valid[0], exact="lend"), valid[1]],
               [valid[0], dict(start=valid[0]["end"], end=valid[0]["end"] + 1, exact=" ")]]
        for parts in bad:
            with self.subTest(parts=parts), self.assertRaises(ValueError):
                subjects._segmented(self.view(page), "event:999:1", parts)
        with self.assertRaisesRegex(ValueError, "at least two"):
            subjects._segmented(self.view(page), "event:999:1", [valid[0]])

    def test_fabricated_gap_canonical_parts_identity_and_provenance_are_rejected(self):
        page = self.page("A: lending her a hand.")
        subject = subjects._segmented(self.view(page), "event:999:1", self.segments(page, ("lending", "a hand")))
        mutations = []
        for key, value in (("canonical", "lend a hand"), ("canonical_parts", ["lend", "a hand"]),
                           ("gap_text", [" "]), ("id", "subject:segmented:forged"),
                           ("window", dict(start=False, end=len(page["text"])))):
            changed = deepcopy(subject)
            changed[key] = value
            mutations.append(changed)
        changed = deepcopy(subject)
        changed["gaps"][0]["exact"] = " "
        mutations.append(changed)
        changed = deepcopy(subject)
        changed["utterance"]["body_ranges"][0]["start"] = 0
        mutations.append(changed)
        for changed in mutations:
            with self.subTest(changed=changed), self.assertRaises(ValueError):
                self.validate(changed)

    def test_page_version_language_story_and_status_are_revalidated(self):
        page = self.page("A: lending her a hand.")
        subject = subjects._segmented(self.view(page), "event:999:1", self.segments(page, ("lending", "a hand")))
        for updates in (dict(text="A: lending her a hand!"), dict(language="ja"), dict(trust="D"),
                        dict(url="https://fixture.invalid/story/event/999/2/"), dict(aux_flag=1)):
            with self.subTest(updates=updates):
                self.page(page["text"])
                columns = ", ".join(key + "=?" for key in updates)
                with dbstore.connect(self.store) as conn:
                    conn.execute("UPDATE web_pages SET " + columns + " WHERE source=? AND id=?",
                                 [*updates.values(), page["source"], page["id"]])
                    conn.commit()
                with self.assertRaises(ValueError):
                    self.validate(subject)

    def test_contextual_sense_is_explicit_and_never_inferred_from_display_text(self):
        page = self.page("A: lending her a hand.")
        subject = subjects._segmented(self.view(page), "event:999:1", self.segments(page, ("lending", "a hand")))
        self.assertNotIn("sense", subject)
        first = ledger._sense(subject["id"], "en", "helping", "Assisting the beneficiary in this utterance")
        second = ledger._sense(subject["id"], "en", "literal-object", "A deliberately separate contextual interpretation")
        self.assertNotEqual(first["id"], second["id"])
        self.assertEqual(first["term_id"], subject["id"])
        with self.assertRaises(ValueError):
            ledger._sense(subject["id"], "en", "", "")


if __name__ == "__main__":
    unittest.main()
