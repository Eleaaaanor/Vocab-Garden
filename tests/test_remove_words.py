from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

import build  # noqa: E402
import remove_words  # noqa: E402


def write_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def make_word(word_id: str, term: str, **extra: object) -> dict:
    word = {
        "id": word_id,
        "term": term,
        "lemma": term,
        "pos": ["noun"],
        "chinese": ["测试"],
        "english_definitions": [{"pos": "noun", "definition": "A test word."}],
        "synonyms": [],
        "examples": [],
        "derivatives": [],
        "confusables": [],
        "personal_notes": [],
        "sources": {"dictionary": [], "synonyms": [], "translation": [], "examples": []},
        "review_flags": [],
    }
    word.update(extra)
    return word


class RemovalFixture(unittest.TestCase):
    """Everything runs inside a temp dir; the real data files are never touched."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.words_path = self.tmp / "words.json"
        self.lists_path = self.tmp / "lists.json"
        self.curation_dir = self.tmp / "curation"
        self.cache_dir = self.tmp / "cache"
        self.raw_records_path = self.tmp / "raw_records.jsonl"
        self.review_path = self.tmp / "review_queue.json"
        self.backup_dir = self.tmp / "backups"
        self.source_path = self.tmp / "notes.md"

        write_json(
            self.words_path,
            {
                "schema_version": 1,
                "words": [
                    make_word("alpha", "alpha", synonyms=["beta"]),
                    make_word(
                        "beta",
                        "beta",
                        synonyms=["alpha", "gamma"],
                        derivatives=[{"term": "alpha", "relation": "related form"}],
                    ),
                    make_word("gamma", "gamma", confusables=[{"term": "beta", "distinction": "spelling"}]),
                    make_word("delta", "delta"),
                ],
            },
        )
        write_json(
            self.lists_path,
            {
                "schema_version": 1,
                "chunk_size": 30,
                "lists": [
                    {"id": "custom", "name": "Custom", "word_ids": ["alpha", "beta", "gamma", "delta"]}
                ],
            },
        )
        write_json(
            self.curation_dir / "01_ab.json",
            {"words": {"beta": {"chinese": ["贝塔"]}, "alpha": {"chinese": ["阿尔法"]}}},
        )
        write_json(self.curation_dir / "02_rest.json", {"words": {"gamma": {"chinese": ["伽马"]}}})
        write_json(self.cache_dir / "dictionary" / "beta.json", {"word": "beta"})
        self.raw_records_path.write_text(
            "\n".join(
                [
                    json.dumps({"raw_term": "beta", "raw_note": "贝塔"}, ensure_ascii=False),
                    json.dumps({"raw_term": "alpha", "raw_note": "阿尔法"}, ensure_ascii=False),
                ]
            )
            + "\n",
            encoding="utf-8",
        )
        self.source_path.write_text(
            "| English | Chinese |\n| --- | --- |\n| beta | 贝塔 |\n| alpha | 阿尔法 |\n| delta | 德尔塔 |\n",
            encoding="utf-8",
        )

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def run_tool(self, *queries: str, **overrides: object):
        kwargs = {
            "words_path": self.words_path,
            "lists_path": self.lists_path,
            "curation_dir": self.curation_dir,
            "review_path": self.review_path,
            "schema_path": ROOT / "data" / "words.schema.json",
            "cache_dir": self.cache_dir,
            "raw_records_path": self.raw_records_path,
            "source_files": (self.source_path,),
            "backup_dir": self.backup_dir,
        }
        kwargs.update(overrides)
        return remove_words.run(list(queries), **kwargs)

    def terms(self) -> list[str]:
        payload = json.loads(self.words_path.read_text(encoding="utf-8"))
        return [word["term"] for word in payload["words"]]

    def word(self, term: str) -> dict:
        payload = json.loads(self.words_path.read_text(encoding="utf-8"))
        for word in payload["words"]:
            if word["term"] == term:
                return word
        raise AssertionError(f"{term} not in wordbank")

    def snapshot(self) -> dict[str, str]:
        return {
            str(path.relative_to(self.tmp)): path.read_text(encoding="utf-8")
            for path in sorted(self.tmp.rglob("*"))
            if path.is_file()
        }


class DryRunTest(RemovalFixture):
    def test_dry_run_reports_plan_without_writing(self) -> None:
        before = self.snapshot()
        code, summary = self.run_tool("beta")
        self.assertEqual(code, 0)
        self.assertFalse(summary["applied"])
        plan = summary["plan"]
        self.assertEqual([item["id"] for item in plan["targets"]], ["beta"])
        self.assertEqual(self.snapshot(), before, "dry run 不能写任何文件")

        # only references coming from surviving words count
        self.assertEqual(
            sorted((ref["word"], ref["field"], ref["value"]) for ref in plan["references"]),
            [("alpha", "synonyms", "beta"), ("gamma", "confusables", "beta")],
        )
        self.assertEqual([entry["term"] for entry in plan["curation"]], ["beta"])
        self.assertEqual([entry["list"] for entry in plan["lists"]], ["custom"])
        self.assertEqual(len(plan["cache"]), 1)
        self.assertTrue(plan["cache"][0].endswith("beta.json"))
        self.assertEqual(plan["raw_records"], 1)
        self.assertEqual([hit["line"] for hit in plan["source_notes"]], [3])
        self.assertEqual([hit["file"] for hit in plan["source_notes"]], ["notes.md"])

    def test_unknown_word_exits_one_and_writes_nothing(self) -> None:
        before = self.snapshot()
        code, summary = self.run_tool("nope")
        self.assertEqual(code, 1)
        self.assertEqual(summary["error"], "unknown words")
        self.assertEqual(self.snapshot(), before)


class ApplyRemovalTest(RemovalFixture):
    def test_apply_removes_word_and_dangling_references(self) -> None:
        code, summary = self.run_tool("beta", apply=True)
        self.assertEqual(code, 0)
        self.assertTrue(summary["applied"])
        self.assertEqual(self.terms(), ["alpha", "delta", "gamma"])

        self.assertEqual(self.word("alpha")["synonyms"], [], "synonym 指向被删词时应当被清理")
        self.assertEqual(self.word("gamma")["confusables"], [])
        self.assertEqual(self.word("delta")["synonyms"], [])
        self.assertEqual(summary["counters"]["references_pruned"], 2)

        # curation entries for the removed term are gone, the others survive
        ab = json.loads((self.curation_dir / "01_ab.json").read_text(encoding="utf-8"))
        self.assertEqual(sorted(ab["words"]), ["alpha"])
        rest = json.loads((self.curation_dir / "02_rest.json").read_text(encoding="utf-8"))
        self.assertEqual(sorted(rest["words"]), ["gamma"])

        # custom lists lose the removed id
        lists = json.loads(self.lists_path.read_text(encoding="utf-8"))
        self.assertEqual(lists["lists"][0]["word_ids"], ["alpha", "gamma", "delta"])

        # review queue is regenerated
        self.assertTrue(summary["review_queue"]["refreshed"])
        self.assertTrue(self.review_path.exists())
        review = json.loads(self.review_path.read_text(encoding="utf-8"))
        self.assertIn("summary", review)

        # backup exists
        backups = list(self.backup_dir.glob("words-*.json"))
        self.assertEqual(len(backups), 1)
        self.assertIn("beta", backups[0].read_text(encoding="utf-8"))

    def test_keep_refs_and_keep_curation(self) -> None:
        code, summary = self.run_tool("beta", apply=True, prune_refs=False, prune_curation=False)
        self.assertEqual(code, 0)
        self.assertEqual(self.word("alpha")["synonyms"], ["beta"])
        ab = json.loads((self.curation_dir / "01_ab.json").read_text(encoding="utf-8"))
        self.assertEqual(sorted(ab["words"]), ["alpha", "beta"])
        self.assertEqual(summary["counters"]["references_pruned"], 0)
        self.assertEqual(summary["counters"]["curation_entries"], 0)

    def test_purges_are_off_by_default(self) -> None:
        code, _ = self.run_tool("beta", apply=True)
        self.assertEqual(code, 0)
        self.assertTrue((self.cache_dir / "dictionary" / "beta.json").exists())
        self.assertEqual(len(self.raw_records_path.read_text(encoding="utf-8").strip().splitlines()), 2)
        self.assertIn("| beta |", self.source_path.read_text(encoding="utf-8"))

    def test_optional_purges(self) -> None:
        code, summary = self.run_tool(
            "beta", apply=True, purge_cache=True, purge_raw=True, purge_source_notes=True
        )
        self.assertEqual(code, 0)
        self.assertFalse((self.cache_dir / "dictionary" / "beta.json").exists())
        self.assertEqual(summary["counters"]["cache_files"], 1)
        self.assertEqual(summary["counters"]["raw_records"], 1)
        self.assertEqual(summary["counters"]["source_rows"], 1)
        raw_lines = self.raw_records_path.read_text(encoding="utf-8").strip().splitlines()
        self.assertEqual(len(raw_lines), 1)
        self.assertIn("alpha", raw_lines[0])
        notes = self.source_path.read_text(encoding="utf-8")
        self.assertNotIn("| beta |", notes)
        self.assertIn("| alpha |", notes)
        self.assertTrue(list(self.backup_dir.glob("notes-*.md")), "源笔记清理前应当先备份")

    def test_bulk_guard_blocks_accidental_mass_deletion(self) -> None:
        code, summary = self.run_tool("alpha", "beta", "gamma", apply=True)
        self.assertEqual(code, 1)
        self.assertIn("refusing to delete", summary["error"])
        self.assertEqual(self.terms(), ["alpha", "beta", "gamma", "delta"])

        code, summary = self.run_tool("alpha", "beta", "gamma", apply=True, force=True)
        self.assertEqual(code, 0)
        self.assertEqual(self.terms(), ["delta"])

    def test_missing_word_can_be_ignored(self) -> None:
        code, summary = self.run_tool("beta", "nope", apply=True, ignore_missing=True)
        self.assertEqual(code, 0)
        self.assertEqual(summary["missing"], ["nope"])
        self.assertEqual(self.terms(), ["alpha", "delta", "gamma"])

    def test_backup_can_be_skipped(self) -> None:
        code, _ = self.run_tool("beta", apply=True, backup=False)
        self.assertEqual(code, 0)
        self.assertFalse(self.backup_dir.exists())

    def test_removal_then_rebuild_has_no_stale_lists(self) -> None:
        code, _ = self.run_tool("beta", apply=True)
        self.assertEqual(code, 0)
        out_path = self.tmp / "out.html"
        result = build.build(
            out_path=out_path,
            words_path=self.words_path,
            lists_path=self.lists_path,
            log=lambda *args: None,
        )
        html = out_path.read_text(encoding="utf-8")
        self.assertEqual(result["words"], 3)
        self.assertNotIn('"term":"beta"', html)
        self.assertIn('"term":"alpha"', html)
        self.assertIn('"word_ids":["alpha","delta","gamma"]', html)  # generated all list
        self.assertEqual(result["custom_lists"], 1)


class TargetResolutionTest(RemovalFixture):
    def test_matches_id_term_and_lemma_case_insensitively(self) -> None:
        payload = json.loads(self.words_path.read_text(encoding="utf-8"))
        payload["words"].append(make_word("epsilon-id", "Epsilon", lemma="epsilon"))
        write_json(self.words_path, payload)
        words = json.loads(self.words_path.read_text(encoding="utf-8"))["words"]
        for query in ("epsilon-id", "epsilon", "EPSILON", " Epsilon "):
            matched, missing = remove_words.resolve_targets(words, [query])
            self.assertEqual([word["id"] for word in matched], ["epsilon-id"], query)
            self.assertEqual(missing, [])

    def test_plan_lists_source_note_occurrences(self) -> None:
        payload = json.loads(self.words_path.read_text(encoding="utf-8"))
        plan = remove_words.build_plan(
            payload,
            [word for word in payload["words"] if word["term"] == "alpha"],
            lists_path=self.lists_path,
            curation_dir=self.curation_dir,
            cache_dir=self.cache_dir,
            raw_records_path=self.raw_records_path,
            source_files=(self.source_path,),
        )
        self.assertEqual(
            sorted((ref["word"], ref["field"]) for ref in plan["references"]),
            [("beta", "derivatives"), ("beta", "synonyms")],
        )
        self.assertEqual(plan["source_notes"][0]["file"], "notes.md")
        self.assertIn("alpha", plan["source_notes"][0]["text"])


if __name__ == "__main__":
    unittest.main()
