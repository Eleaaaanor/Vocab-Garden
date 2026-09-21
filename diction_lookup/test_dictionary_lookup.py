import unittest

from dictionary_lookup import (
    _extract_zh_wiktionary_definitions,
    _looks_like_lexical_term,
    _unique,
    _validate_word,
)


class DictionaryLookupHelpersTest(unittest.TestCase):
    def test_chinese_parser_keeps_definitions_and_skips_references(self) -> None:
        html = """
        <div class="mw-parser-output">
          <div class="mw-heading mw-heading2"><h2>英语</h2></div>
          <div class="mw-heading mw-heading3"><h3>名词</h3></div>
          <ol>
            <li><a>机缘</a><b>巧合</b><dl><dd>Example text</dd></dl></li>
            <li>意外的收获</li>
          </ol>
          <div class="mw-heading mw-heading3"><h3>参考资料</h3></div>
          <ol><li>Google Books citation</li></ol>
          <div class="mw-heading mw-heading2"><h2>法语</h2></div>
          <div class="mw-heading mw-heading3"><h3>名词</h3></div>
          <ol><li>French definition</li></ol>
        </div>
        """

        self.assertEqual(
            _extract_zh_wiktionary_definitions(html, limit=8),
            ["机缘巧合", "意外的收获"],
        )

    def test_word_validation(self) -> None:
        self.assertEqual(_validate_word("  state-of-the-art  "), "state-of-the-art")
        with self.assertRaises(ValueError):
            _validate_word("apple<script>")

    def test_deduplication_and_noise_filter(self) -> None:
        self.assertEqual(_unique(["Happy", " happy ", "glad"]), ["Happy", "glad"])
        self.assertTrue(_looks_like_lexical_term("good-looking"))
        self.assertFalse(_looks_like_lexical_term("CBer"))


if __name__ == "__main__":
    unittest.main()
