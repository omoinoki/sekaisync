import unittest

from sekaisync.normalize import normalize_name, similarity_score


class NormalizeTest(unittest.TestCase):
    def test_normalizes_full_width_and_punctuation(self):
        self.assertEqual(normalize_name("25時，Nightcord見。"), normalize_name("25時Nightcord見"))

    def test_similarity_handles_exact_and_subset(self):
        self.assertEqual(similarity_score("Hoshino Ichika", "Hoshino Ichika"), 100)
        self.assertGreater(similarity_score("Hoshino Ichika", "Hoshino Ichika and Tenma Saki"), 0)
        self.assertEqual(similarity_score("Hoshino Ichika", "Tenma Saki"), 0)


if __name__ == "__main__":
    unittest.main()
