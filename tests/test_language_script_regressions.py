import unittest

from sekaisync.webindex import text_matches_language


class LanguageScriptRegressionTests(unittest.TestCase):
    def test_english_does_not_accept_other_scripts_without_latin_body(self):
        for body in ("\uadfc\ucc98\uc5d0 \uc788\ub294 \uce90\ub9ad\ud130\uc5d0\uac8c \ub9d0\uc744 \uac78\uc5b4 \ubcf4\uc138\uc694.",
                     "\u3053\u3093\u306b\u3061\u306f", "\uff7a\uff9d\uff86\uff81\uff8a", "\u3131\u3131\u3131", "\u1100\u1161"):
            for prefix in ("", "Kanade: ", "Kanade\uff1a"):
                with self.subTest(body=body, prefix=prefix):
                    self.assertFalse(text_matches_language("en", prefix + body))

    def test_korean_does_not_accept_other_scripts_without_hangul_body(self):
        for body in ("\u3053\u3093\u306b\u3061\u306f", "\u7e41\u9ad4\u4e2d\u6587\u6b63\u6587", "\uff7a\uff9d\uff86\uff81\uff8a",
                     "Try talking to the characters nearby."):
            for prefix in ("", "\uce74\ub098\ub370: ", "\uce74\ub098\ub370\uff1a"):
                with self.subTest(body=body, prefix=prefix):
                    self.assertFalse(text_matches_language("ko", prefix + body))

    def test_short_borrowed_latin_and_nonscript_forms_remain_usable(self):
        for body in ("", "...", "123", "JUMP!", "Wonderhoy!", "Leo/need", "Vivid BAD SQUAD", "Make everyone smile!"):
            for language in ("en", "ko"):
                with self.subTest(body=body, language=language):
                    self.assertTrue(text_matches_language(language, body))

    def test_real_expected_body_scripts_and_borrowed_names_are_usable(self):
        self.assertTrue(text_matches_language("en", "Kanade\uff1aTry talking to the characters nearby."))
        self.assertTrue(text_matches_language("ko", "Kanade\uff1a\uadfc\ucc98\uc5d0 \uc788\ub294 \uce90\ub9ad\ud130\uc5d0\uac8c \ub9d0\uc744 \uac78\uc5b4 \ubcf4\uc138\uc694."))
        self.assertTrue(text_matches_language("ko", "\u3131\u3131\u3131"))
        self.assertTrue(text_matches_language("ko", "\u1100\u1161"))
        self.assertTrue(text_matches_language("ko", "\u7a7a\u6e2f\uc5d0 \uac00\uc694"))
        self.assertTrue(text_matches_language("en", "I will sing \u30df\u30af's song today."))

    def test_speaker_scripts_do_not_establish_missing_body_language(self):
        self.assertFalse(text_matches_language("en", "English speaker: \u3042\u308a\u304c\u3068\u3046"))
        self.assertFalse(text_matches_language("ko", "\uce74\ub098\ub370: The nearby characters are waiting for you."))
        self.assertTrue(text_matches_language("en", "\u594f\uff1aThank you!"))
        self.assertTrue(text_matches_language("ko", "\u594f\uff1a\uace0\ub9c8\uc6cc!"))


if __name__ == "__main__":
    unittest.main()
