"""Default public occurrence positions are stable across process hash seeds."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import unittest

from sekaisync import termindex


def make_projection(languages=None):
    story = "event:905:1"
    pages = [dict(source="fixture", id="web:fixture:" + language + ":event_story:905:1",
                  language=language, kind="event_story", trust="B", text="A: " + text + ".")
             for language, text in (("en", "source"), ("zh_hans", "target"))]
    anchors = [dict(id="anchor:" + page["language"], language=page["language"], story_key=story,
                    page_source=page["source"], page_id=page["id"],
                    page_sha256=hashlib.sha256(page["text"].encode()).hexdigest(),
                    segments=[dict(start=3, end=9, exact=page["text"][3:9])]) for page in pages]
    relation = dict(source=anchors[0], target=anchors[1], target_language="zh_hans", kind="lexical",
                    story_key=story, structural_grounding=True, grounding=dict(term="source", context={}),
                    sense=dict(id="sense:fixture"))
    return termindex._occurrence_lookup([relation], "source", languages=languages, pages=pages)


class OccurrenceDefaultOrderTests(unittest.TestCase):
    def test_default_order_is_sorted_without_changing_payloads(self):
        actual = make_projection()
        explicit = make_projection(sorted(termindex.TERM_LANGUAGES))
        self.assertEqual(actual, explicit)
        self.assertEqual([row["language"] for row in actual[0]["positions"]],
                         ["en", "ja", "ko", "zh_hans", "zh_tw"])

    def test_explicit_caller_order_is_preserved(self):
        actual = make_projection(["zh_hans", "en"])
        self.assertEqual([row["language"] for row in actual[0]["positions"]], ["zh_hans", "en"])
        self.assertEqual([row["term"] for row in actual[0]["positions"]], ["target", "source"])

    def test_fresh_processes_return_identical_default_and_explicit_shapes(self):
        root = Path(__file__).resolve().parents[1]
        program = ("import json; from test_occurrence_default_order import make_projection; "
                   "print(json.dumps([make_projection(),make_projection(['zh_hans','en'])],"
                   "ensure_ascii=False,sort_keys=True))")
        outputs = []
        for seed in ("0", "1", "2", "3"):
            env = dict(os.environ, PYTHONHASHSEED=seed,
                       PYTHONPATH=os.pathsep.join((str(root / "tests"), str(root))))
            process = subprocess.run([sys.executable, "-B", "-X", "utf8", "-c", program],
                                     cwd=root, env=env, capture_output=True, text=True, encoding="utf-8")
            self.assertEqual(process.returncode, 0, process.stderr)
            outputs.append(process.stdout)
        self.assertTrue(all(value == outputs[0] for value in outputs))


if __name__ == "__main__":
    unittest.main()
