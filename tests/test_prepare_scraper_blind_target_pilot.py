"""Synthetic raw grounding and publication checks, without pilot labels."""
from contextlib import redirect_stdout
from copy import deepcopy
import hashlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from scripts import prepare_scraper_blind_target_pilot as pilot


class BlindTargetPreparationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.languages = ["ja", "zh_hans", "zh_tw", "ko"]

    def view(self, text, language, start=100):
        return dict(source="fixture", page_id="web:fixture:" + language + ":event_story:999:1",
                    language=language, sha256=hashlib.sha256(text.encode()).hexdigest(),
                    start=start, end=start + len(text), text=text, complete=True)

    def seal(self, packet):
        canonical = json.dumps({key: value for key, value in packet.items() if key != "packet_sha256"},
                               ensure_ascii=False, sort_keys=True).encode()
        packet["packet_sha256"] = hashlib.sha256(canonical).hexdigest()
        return packet

    def fixture(self, units=12):
        items, observations = [], []
        for index in range(units):
            source = self.view("Alice: lending her a hand for case" + str(index) + ".", "en")
            surface = "lending her a hand"
            start = source["start"] + source["text"].index(surface)
            contexts = {language: self.view("Alice: \U0001f9edlend her a hand.\r\nContinue this utterance.", language)
                        for language in self.languages}
            identity = "synthetic-source:" + str(index)
            items.append(dict(source_annotation_id=identity, source_surface=surface, source_meaning="Synthetic fixture only",
                              source_categories=["predicate"], source_segments=[dict(start=start, end=start + len(surface), exact=surface)],
                              source_evidence_id="synthetic-row:" + str(index), source_context=source, target_contexts=contexts))
            observations.append(dict(source_annotation_id=identity, targets={language: dict(
                kind="lexical", parts=["lend", "a hand"], reason="Synthetic structure, not a semantic assessment.")
                for language in self.languages}))
        packet = self.seal(dict(schema="sekaisync/p0-fixed-source-blind-target-pilot@1", story_key="event:999:1",
            source_language="en", target_languages=list(self.languages), source_units=units,
            directed_requirements=units * len(self.languages), target_spans_or_labels_in_packet=False,
            judgment_contract=dict(schema="sekaisync/p0-blind-target-pilot-judgments@1"), items=items))
        proposal = dict(agent="synthetic-review", target_reference_read=False, items=observations)
        return packet, proposal

    def files(self):
        packet, proposal = self.fixture()
        packet_path, proposal_path, out = self.base / "packet.json", self.base / "proposal.json", self.base / "judgments.json"
        packet_path.write_text(json.dumps(packet, ensure_ascii=False, indent=2), encoding="utf-8")
        proposal_path.write_text(json.dumps(proposal, ensure_ascii=False, indent=2), encoding="utf-8")
        return packet_path, proposal_path, out

    def invoke(self, paths, expected=None):
        packet_path, proposal_path, out = paths
        digest = expected or hashlib.sha256(packet_path.read_bytes()).hexdigest()
        argv = ["prepare", "--packet", str(packet_path), "--expected-packet-sha256", digest,
                "--proposal", str(proposal_path), "--out", str(out)]
        with patch.object(pilot.sys, "argv", argv), redirect_stdout(io.StringIO()):
            pilot.main()

    def test_twelve_fixed_sources_keep_forty_eight_directions_and_raw_unicode_offsets(self):
        packet, proposal = self.fixture()
        result = pilot._prepare(packet, proposal)
        self.assertEqual(len(result["records"]), 48)
        self.assertEqual({record["target_language"] for record in result["records"]}, set(self.languages))
        segments = result["records"][0]["target_segments"]
        self.assertEqual(segments, [dict(start=108, end=112, exact="lend"), dict(start=117, end=123, exact="a hand")])
        self.assertFalse(result["target_reference_read"])

    def test_overlapping_or_repeated_matches_never_choose_first_hit(self):
        for text, parts in (("Alice: aa aaa.", ["aa"]), ("Alice: lend her a hand, lend him a hand.", ["lend", "a hand"])):
            with self.subTest(text=text):
                packet, proposal = self.fixture(units=1)
                packet["items"][0]["target_contexts"]["ja"] = self.view(text, "ja")
                proposal["items"][0]["targets"]["ja"]["parts"] = parts
                with self.assertRaisesRegex(ValueError, "unambiguous"):
                    pilot._prepare(self.seal(packet), proposal)

    def test_fragment_order_overlap_case_and_exact_original_quotes_are_mandatory(self):
        for parts in (["a hand", "lend"], ["lend her", "her a hand"], ["Lend", "a hand"],
                      ["lend", "imaginary"], ["lend", " "], ["lend", 1]):
            with self.subTest(parts=parts):
                packet, proposal = self.fixture(units=1)
                proposal["items"][0]["targets"]["ja"]["parts"] = parts
                with self.assertRaises(ValueError):
                    pilot._prepare(packet, proposal)

    def test_lexical_speaker_metadata_and_relabelled_turns_are_rejected(self):
        for text, parts in (("Alice: lend a hand.", ["Alice"]),
                            ("Alice: lend\nBob: a hand.", ["lend", "a hand"]),
                            ("Alice: lend\r\nAlice: a hand.", ["lend", "a hand"]),
                            ("Alice﹕lend\nBob︓a hand.", ["lend", "a hand"])):
            with self.subTest(text=text):
                packet, proposal = self.fixture(units=1)
                packet["items"][0]["target_contexts"]["ja"] = self.view(text, "ja")
                proposal["items"][0]["targets"]["ja"]["parts"] = parts
                with self.assertRaises(ValueError):
                    pilot._prepare(self.seal(packet), proposal)

    def test_omitted_unresolved_require_empty_parts_and_contextual_types_keep_selected_parts(self):
        for kind in pilot.KINDS:
            with self.subTest(kind=kind):
                packet, proposal = self.fixture(units=1)
                observed = proposal["items"][0]["targets"]["ja"]
                observed["kind"] = kind
                observed["parts"] = [] if kind in {"omitted", "unresolved"} else ["lend", "a hand"]
                records = pilot._prepare(packet, proposal)["records"]
                self.assertEqual(records[0]["relation_type"], kind)
                self.assertEqual(bool(records[0]["target_segments"]), kind not in {"omitted", "unresolved"})
                observed["parts"] = ["lend"] if not observed["parts"] else []
                with self.assertRaises(ValueError):
                    pilot._prepare(packet, proposal)

    def test_fixed_source_and_direction_denominators_cannot_shrink_duplicate_or_expand(self):
        mutations = (
            lambda packet, proposal: proposal["items"].pop(),
            lambda packet, proposal: proposal["items"].append(deepcopy(proposal["items"][0])),
            lambda packet, proposal: proposal["items"][0]["targets"].pop("ja"),
            lambda packet, proposal: proposal["items"][0]["targets"].update(en=dict(kind="unresolved", parts=[], reason="extra")),
            lambda packet, proposal: packet["items"].append(deepcopy(packet["items"][0])),
            lambda packet, proposal: packet["target_languages"].append("ja"),
            lambda packet, proposal: packet.update(source_units=1),
            lambda packet, proposal: packet.update(directed_requirements=1))
        for mutate in mutations:
            with self.subTest(mutate=mutate):
                packet, proposal = self.fixture()
                mutate(packet, proposal)
                with self.assertRaises(ValueError):
                    pilot._prepare(self.seal(packet), proposal)

    def test_target_windows_require_strict_complete_boolean_and_integer_raw_bounds(self):
        changes = (dict(complete=False), dict(complete=1), dict(start=100.0), dict(end=144.0),
                   dict(start=True), dict(start=-1), dict(end=101), dict(language="en"), dict(sha256="invalid"))
        for fields in changes:
            with self.subTest(fields=fields):
                packet, proposal = self.fixture(units=1)
                view = packet["items"][0]["target_contexts"]["ja"]
                view.update(fields)
                if fields.keys() == {"start"}:
                    view["end"] = view["start"] + len(view["text"])
                with self.assertRaises(ValueError):
                    pilot._prepare(self.seal(packet), proposal)

    def test_source_window_and_segments_are_replayed_not_only_counted(self):
        mutations = (
            lambda original: original["source_context"].update(complete=False),
            lambda original: original["source_context"].update(complete=1),
            lambda original: original["source_context"].update(language="ko"),
            lambda original: original["source_segments"][0].update(exact="invented"),
            lambda original: original["source_segments"][0].update(start=0),
            lambda original: original["source_segments"].clear(),
            lambda original: original["source_segments"][0].update(start=107.0))
        for mutate in mutations:
            with self.subTest(mutate=mutate):
                packet, proposal = self.fixture(units=1)
                mutate(packet["items"][0])
                with self.assertRaises(ValueError):
                    pilot._prepare(self.seal(packet), proposal)
        packet, proposal = self.fixture(units=1)
        view = packet["items"][0]["source_context"]
        packet["items"][0]["source_segments"] = [dict(start=view["start"], end=view["start"] + 5, exact="Alice")]
        with self.assertRaises(ValueError):
            pilot._prepare(self.seal(packet), proposal)

    def test_source_fragments_cannot_cross_different_or_relabelled_speaker_turns(self):
        for text in ("Alice: lending\nBob: a hand.", "Alice: lending\r\nAlice: a hand."):
            with self.subTest(text=text):
                packet, proposal = self.fixture(units=1)
                source = self.view(text, "en")
                original = packet["items"][0]
                original["source_context"] = source
                original["source_segments"] = []
                for exact in ("lending", "a hand"):
                    start = source["start"] + text.index(exact)
                    original["source_segments"].append(dict(start=start, end=start + len(exact), exact=exact))
                with self.assertRaises(ValueError):
                    pilot._prepare(self.seal(packet), proposal)

    def test_genuine_source_and_target_labelled_soft_wraps_preserve_raw_fragments(self):
        packet, proposal = self.fixture(units=1)
        source = self.view("Alice: lending her\r\na hand.", "en")
        original = packet["items"][0]
        original["source_context"] = source
        original["source_segments"] = []
        for exact in ("lending", "a hand"):
            start = source["start"] + source["text"].index(exact)
            original["source_segments"].append(dict(start=start, end=start + len(exact), exact=exact))
        target = self.view("Alice: \U0001f9edlend her\r\na hand.", "ja")
        original["target_contexts"]["ja"] = target
        result = pilot._prepare(self.seal(packet), proposal)
        selected = result["records"][0]["target_segments"]
        self.assertEqual([part["exact"] for part in selected], ["lend", "a hand"])
        self.assertEqual(selected[0]["start"], target["start"] + target["text"].index("lend"))
        self.assertEqual(selected[1]["start"], target["start"] + target["text"].index("a hand"))

    def test_canonical_hash_and_label_isolation_are_independent_checks(self):
        packet, proposal = self.fixture()
        packet["story_key"] = "event:999:2"
        with self.assertRaisesRegex(ValueError, "canonical"):
            pilot._prepare(packet, proposal)
        for target_flag, proposal_flag in ((True, False), (False, True), (0, False), (False, 0)):
            packet, proposal = self.fixture()
            packet["target_spans_or_labels_in_packet"] = target_flag
            proposal["target_reference_read"] = proposal_flag
            with self.assertRaises(ValueError):
                pilot._prepare(self.seal(packet), proposal)

    def test_main_records_actual_file_bytes_and_full_imported_code_closure(self):
        paths = self.files()
        self.invoke(paths)
        result = json.loads(paths[2].read_text(encoding="utf-8"))
        self.assertEqual(len(result["records"]), 48)
        provenance = result["provenance"]
        self.assertEqual(provenance["packet_file_sha256"], hashlib.sha256(paths[0].read_bytes()).hexdigest())
        self.assertEqual(provenance["proposal_file_sha256"], hashlib.sha256(paths[1].read_bytes()).hexdigest())
        self.assertNotEqual(provenance["packet_file_sha256"], result["packet_sha256"])
        self.assertEqual(provenance["code_sha256"], pilot._code_hashes())
        self.assertIn(str((pilot.ROOT / "sekaisync" / "span_subjects.py").resolve()), provenance["code_sha256"])

    def test_wrong_file_hash_and_import_time_code_drift_block_publication(self):
        paths = self.files()
        with self.assertRaisesRegex(ValueError, "file hash"):
            self.invoke(paths, expected="0" * 64)
        self.assertFalse(paths[2].exists())
        with patch.object(pilot, "_code_hashes", return_value={"fake": "changed"}), self.assertRaisesRegex(ValueError, "after import"):
            self.invoke(paths)
        self.assertFalse(paths[2].exists())

    def test_mid_run_input_byte_mutation_blocks_publication(self):
        for changed in (0, 1):
            with self.subTest(changed=changed):
                paths = self.files()
                original = pilot._prepare

                def mutate(packet, proposal):
                    result = original(packet, proposal)
                    paths[changed].write_bytes(paths[changed].read_bytes() + b" ")
                    return result

                with patch.object(pilot, "_prepare", side_effect=mutate), self.assertRaisesRegex(ValueError, "changed during"):
                    self.invoke(paths)
                self.assertFalse(paths[2].exists())

    def test_mid_run_script_dependency_or_new_module_drift_blocks_publication(self):
        root, package = self.base / "code", self.base / "code" / "sekaisync"
        package.mkdir(parents=True)
        script, dependency = root / "prepare.py", package / "span_subjects.py"
        script.write_text("# synthetic preparation\n", encoding="utf-8")
        dependency.write_text("# synthetic dependency\n", encoding="utf-8")
        for changed in (script, dependency, package / "new_dependency.py"):
            with self.subTest(changed=changed), patch.object(pilot, "ROOT", root), patch.object(pilot, "__file__", str(script)):
                baseline = pilot._code_hashes()
                paths = self.files()
                original = pilot._prepare

                def mutate(packet, proposal):
                    result = original(packet, proposal)
                    changed.write_text("# changed synthetic code\n", encoding="utf-8")
                    return result

                with patch.object(pilot, "_IMPORT_HASHES", baseline), patch.object(pilot, "_prepare", side_effect=mutate):
                    with self.assertRaisesRegex(ValueError, "changed during"):
                        self.invoke(paths)
                self.assertFalse(paths[2].exists())

    def test_existing_and_racing_output_are_preserved_exclusively(self):
        paths = self.files()
        paths[2].write_bytes(b"existing frozen result")
        with self.assertRaises(FileExistsError):
            self.invoke(paths)
        self.assertEqual(paths[2].read_bytes(), b"existing frozen result")
        paths[2].unlink()
        original_open = Path.open

        def race(path, mode="r", *args, **kwargs):
            if path == paths[2] and mode == "x":
                with original_open(path, "wb") as output:
                    output.write(b"racing frozen result")
            return original_open(path, mode, *args, **kwargs)

        with patch.object(Path, "open", race), self.assertRaises(FileExistsError):
            self.invoke(paths)
        self.assertEqual(paths[2].read_bytes(), b"racing frozen result")


if __name__ == "__main__":
    unittest.main()
