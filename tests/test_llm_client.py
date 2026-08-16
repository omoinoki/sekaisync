import unittest

from sekaisync.llm_client import _parse_json_content


class LLMClientTest(unittest.TestCase):
    def test_parse_plain_json(self):
        self.assertEqual(_parse_json_content('{"a": 1}'), {"a": 1})

    def test_parse_fenced_json(self):
        content = '```json\n{"terms": [{"term": "网络天堂"}]}\n```'
        self.assertEqual(_parse_json_content(content)["terms"][0]["term"], "网络天堂")

    def test_invalid_json_raises(self):
        with self.assertRaises(ValueError):
            _parse_json_content("not json")


if __name__ == "__main__":
    unittest.main()
