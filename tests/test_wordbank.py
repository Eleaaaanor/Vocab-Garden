from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

from validate import validate_payload  # noqa: E402
from wordbank import build_words_from_records, parse_markdown_notes, split_raw_term  # noqa: E402


class ParsingTest(unittest.TestCase):
    def test_requested_boundary_examples(self) -> None:
        self.assertEqual(split_raw_term("candid, frank")[0], ["candid", "frank"])
        self.assertEqual(
            split_raw_term("speculative / conjectural")[0],
            ["speculative", "conjectural"],
        )
        self.assertEqual(
            split_raw_term("founder / foundering")[:2],
            (["founder", "foundering"], "related_forms"),
        )
        self.assertEqual(split_raw_term("belied (belie)")[0], ["belie"])
        self.assertEqual(split_raw_term("indebted (to)")[0], ["indebted to"])

    def test_duplicates_merge_without_losing_notes(self) -> None:
        markdown = """| English | Chinese |
| --- | --- |
| vacillation | 优柔寡断 |
| vacillation | 摇摆不定 |
"""
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "notes.md"
            path.write_text(markdown, encoding="utf-8")
            words = build_words_from_records(parse_markdown_notes(path))
        self.assertEqual(len(words), 1)
        self.assertEqual(len(words[0]["personal_notes"]), 2)
        self.assertEqual(words[0]["chinese"], ["优柔寡断", "摇摆不定"])


class ValidationTest(unittest.TestCase):
    def test_self_reference_and_missing_fields_become_flags(self) -> None:
        schema = json.loads((ROOT / "data" / "words.schema.json").read_text(encoding="utf-8"))
        word = {
            "id": "test",
            "term": "test",
            "lemma": "test",
            "pos": [],
            "chinese": [],
            "english_definitions": [],
            "synonyms": ["test"],
            "examples": [],
            "derivatives": [],
            "confusables": [],
            "personal_notes": ["test note"],
            "sources": {"dictionary": [], "synonyms": [], "translation": [], "examples": []},
            "review_flags": [],
        }
        hard_errors, review = validate_payload({"schema_version": 1, "words": [word]}, schema)
        self.assertFalse(hard_errors)
        self.assertEqual(len(review), 1)
        self.assertTrue(
            {"invalid_pos", "missing_chinese", "missing_definition", "missing_example", "self_reference"}
            .issubset(set(word["review_flags"]))
        )


if __name__ == "__main__":
    unittest.main()
