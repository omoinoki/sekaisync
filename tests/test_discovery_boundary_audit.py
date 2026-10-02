"""The host receives a source-only layered audit, not synthesized word labels."""
from types import SimpleNamespace
import unittest

from sekaisync import agent_packets as packets


class DiscoveryBoundaryAuditTests(unittest.TestCase):
    def render(self, task="discovery"):
        source = dict(page_id="source-page", source="fixture", language="en",
                      start=0, end=33, text="Minori: I can tell you're excited!",
                      complete=True, sha256="source-sha")
        target = dict(page_id="target-page", source="fixture", language="ja",
                      start=0, end=12, text="TARGET_SECRET", complete=True,
                      sha256="target-sha")
        item = SimpleNamespace(_context=dict(task=task, source_language="en",
                                             rows=[dict(id="row-1", story_key="event:1:1",
                                                        source=source, target=target)]))
        return "\n".join(packets._render_context(item))

    def test_audit_is_source_only_and_keeps_original_context(self):
        rendered = self.render()
        self.assertIn("discovery_boundary_audit:", rendered)
        self.assertIn("I can tell you're excited!", rendered)
        self.assertNotIn("TARGET_SECRET", rendered)
        self.assertNotIn("target-page", rendered)
        self.assertNotIn("source-sha", rendered)

    def test_audit_requires_composite_layers_without_substring_enumeration(self):
        rendered = self.render()
        for instruction in ("meaningful minimal and extended layers separately",
                            "auxiliary/negation/modality", "selected complement",
                            "overlapping fragments do not register their composite",
                            "contractions and tense/politeness inflection intact",
                            "never invent a lemma", "do not enumerate all substrings"):
            with self.subTest(instruction=instruction):
                self.assertIn(instruction, rendered)

    def test_translation_does_not_acquire_source_discovery_instructions(self):
        rendered = self.render("translation")
        self.assertIn("TARGET_SECRET", rendered)
        self.assertNotIn("discovery_boundary_audit:", rendered)


if __name__ == "__main__":
    unittest.main()
