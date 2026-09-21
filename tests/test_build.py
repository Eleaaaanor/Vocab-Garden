from __future__ import annotations

import json
import re
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

import build  # noqa: E402

CJK_RE = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]")


def write_json(path: Path, payload: object) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


REGEX_PREFIX_CHARS = set("(,=:[!&|?{};+-*%~^<>")
KEYWORDS_BEFORE_REGEX = ("return", "typeof", "case", "delete", "void", "in", "of", "new", "instanceof")


def js_string_literals(source: str) -> list[str]:
    """Extract JS string literals, skipping comments and regex literals.

    Good enough for a UI-language check; not a full JavaScript parser.
    """
    literals: list[str] = []
    index = 0
    length = len(source)
    while index < length:
        char = source[index]
        following = source[index + 1] if index + 1 < length else ""
        if char == "/" and following == "/":
            end = source.find("\n", index)
            if end == -1:
                break
            index = end
            continue
        if char == "/" and following == "*":
            end = source.find("*/", index + 2)
            index = length if end == -1 else end + 2
            continue
        if char == "/" and regex_can_start(source, index):
            index = skip_regex(source, index)
            continue
        if char in "'\"`":
            quote = char
            index += 1
            buffer: list[str] = []
            while index < length:
                current = source[index]
                if current == "\\":
                    buffer.append(source[index:index + 2])
                    index += 2
                    continue
                if current == quote:
                    index += 1
                    break
                buffer.append(current)
                index += 1
            literals.append("".join(buffer))
            continue
        index += 1
    return literals


def regex_can_start(source: str, index: int) -> bool:
    """Heuristic: a slash starts a regex when the previous token cannot end an expression."""
    cursor = index - 1
    while cursor >= 0 and source[cursor] in " \t\r\n":
        cursor -= 1
    if cursor < 0:
        return True
    if source[cursor] in REGEX_PREFIX_CHARS:
        return True
    word = ""
    while cursor >= 0 and (source[cursor].isalnum() or source[cursor] in "_$"):
        word = source[cursor] + word
        cursor -= 1
    return word in KEYWORDS_BEFORE_REGEX


def skip_regex(source: str, index: int) -> int:
    index += 1
    length = len(source)
    in_class = False
    while index < length:
        char = source[index]
        if char == "\\":
            index += 2
            continue
        if char == "[":
            in_class = True
        elif char == "]":
            in_class = False
        elif char == "/" and not in_class:
            index += 1
            while index < length and source[index].isalpha():
                index += 1
            return index
        elif char == "\n":
            return index
        index += 1
    return length


def shipped_page_path() -> Path:
    """默认产物路径：[语言简称]-[词数].html（如 dist/En-735.html）。"""
    return build.shipped_out_path()


def minimal_word(word_id: str, term: str, **extra: object) -> dict:
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


class BuildFixtureTest(unittest.TestCase):
    """The real data files must stay untouched: everything runs in a temp dir."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.words_path = self.tmp / "words.json"
        self.lists_path = self.tmp / "lists.json"

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def build_fixture(self, words: list, lists: dict | None = None) -> str:
        write_json(self.words_path, {"schema_version": 1, "words": words})
        if lists is not None:
            write_json(self.lists_path, lists)
        out_path = self.tmp / "out.html"
        build.build(
            out_path=out_path,
            words_path=self.words_path,
            lists_path=self.lists_path,
            log=lambda *args: None,
        )
        return out_path.read_text(encoding="utf-8")


class DataEmbeddingTest(BuildFixtureTest):
    def test_embeds_data_and_constants(self) -> None:
        html = self.build_fixture(
            [minimal_word("alpha", "alpha"), minimal_word("beta", "beta")],
            {
                "schema_version": 1,
                "chunk_size": 30,
                "lists": [
                    {
                        "id": "favourites",
                        "name": "Favourites",
                        "word_ids": ["beta"],
                    }
                ],
            },
        )
        self.assertIn("const VOCABULARY_DATA = ", html)
        self.assertIn("const LIST_DATA = ", html)
        self.assertIn('"term":"alpha"', html)
        self.assertIn('"word_ids":["alpha","beta"]', html)
        self.assertIn('"id":"favourites"', html)
        self.assertNotIn(build.WORDS_PLACEHOLDER, html)
        self.assertNotIn(build.LISTS_PLACEHOLDER, html)
        self.assertNotIn(build.CSS_PLACEHOLDER, html)
        self.assertNotIn(build.JS_PLACEHOLDER, html)

    def test_stays_offline(self) -> None:
        html = self.build_fixture([minimal_word("alpha", "alpha")])
        for pattern in ("<script src", "<link rel=\"stylesheet\"", "//unpkg.com", "cdn.jsdelivr.net"):
            self.assertNotIn(pattern, html)

    def test_script_breakout_is_escaped(self) -> None:
        html = self.build_fixture(
            [minimal_word("tricky", "tricky", personal_notes=["</script><script>alert(1)</script>"])]
        )
        self.assertNotIn("</script><script>alert(1)", html)
        self.assertIn("\\u003c/script\\u003e\\u003cscript\\u003ealert(1)", html)

    def test_real_wordbank_round_trips(self) -> None:
        payload = json.loads(build.WORDS_PATH.read_text(encoding="utf-8"))
        vocabulary, warnings = build.load_words(build.WORDS_PATH)
        self.assertEqual(len(vocabulary["words"]), len(payload["words"]))
        self.assertEqual(warnings, [])
        html = build.render_html(
            template=build.TEMPLATE_PATH.read_text(encoding="utf-8"),
            vocabulary_data=vocabulary,
            list_data={"schema_version": 1, "lists": []},
            css="",
            js="",
        )
        self.assertIn('"words":[{', html)
        self.assertGreater(len(html), 100_000)


class ToleranceTest(BuildFixtureTest):
    def test_skips_unusable_words(self) -> None:
        vocabulary, warnings = build.load_words(self.path_with_words([None, {"id": "x"}, minimal_word("ok", "ok")]))
        self.assertEqual([word["id"] for word in vocabulary["words"]], ["ok"])
        self.assertEqual(len(warnings), 1)

    def test_drops_unknown_list_ids(self) -> None:
        write_json(self.words_path, {"schema_version": 1, "words": [minimal_word("ok", "ok")]})
        write_json(
            self.lists_path,
            {
                "schema_version": 1,
                "lists": [{"id": "custom", "name": "Custom", "word_ids": ["ok", "ghost", "ok"]}],
            },
        )
        list_data, warnings, created = build.load_lists(
            build.load_words(self.words_path)[0]["words"], self.lists_path
        )
        self.assertFalse(created)
        custom = [item for item in list_data["lists"] if item["id"] == "custom"]
        self.assertEqual(custom[0]["word_ids"], ["ok"])
        self.assertTrue(any("不在 words.json" in warning for warning in warnings))

    def test_reserved_list_ids_are_ignored(self) -> None:
        write_json(self.words_path, {"schema_version": 1, "words": [minimal_word("ok", "ok")]})
        write_json(
            self.lists_path,
            {
                "schema_version": 1,
                "lists": [
                    {"id": "all", "name": "Stale All", "word_ids": []},
                    {"id": "part-01", "name": "Stale Part", "word_ids": []},
                ],
            },
        )
        list_data, warnings, _ = build.load_lists(
            build.load_words(self.words_path)[0]["words"], self.lists_path
        )
        self.assertEqual([item["name"] for item in list_data["lists"]], ["All Words", "Part 01"])
        self.assertEqual(len([item for item in warnings if "自动生成" in item]), 2)

    def test_creates_lists_file_when_missing(self) -> None:
        write_json(self.words_path, {"schema_version": 1, "words": [minimal_word("ok", "ok")]})
        list_data, _, created = build.load_lists(
            build.load_words(self.words_path)[0]["words"], self.lists_path
        )
        self.assertTrue(created)
        self.assertTrue(self.lists_path.exists())
        self.assertEqual(list_data["lists"][0]["id"], "all")
        self.assertEqual(list_data["lists"][0]["word_ids"], ["ok"])
        written = json.loads(self.lists_path.read_text(encoding="utf-8"))
        self.assertEqual(written["chunk_size"], build.DEFAULT_CHUNK_SIZE)
        self.assertEqual(written["lists"], [])

    def test_missing_placeholder_raises(self) -> None:
        with self.assertRaises(build.BuildError):
            build.render_html("<p>no placeholders</p>", {"words": []}, {"lists": []}, "", "")

    def path_with_words(self, words: list) -> Path:
        path = self.tmp / "words.json"
        write_json(path, {"schema_version": 1, "words": words})
        return path


class ChunkListTest(BuildFixtureTest):
    def make_words(self, count: int) -> list:
        return [minimal_word(f"w{index:03d}", f"word{index:03d}") for index in range(count)]

    def test_chunks_of_thirty_plus_remainder(self) -> None:
        words = self.make_words(735)
        lists = build.chunk_lists(words, 30)
        self.assertEqual(len(lists), 25)
        self.assertEqual([len(item["word_ids"]) for item in lists[:24]], [30] * 24)
        self.assertEqual(len(lists[-1]["word_ids"]), 15)
        self.assertEqual(lists[0]["id"], "part-01")
        self.assertEqual(lists[0]["name"], "Part 01")
        self.assertEqual(lists[0]["description"], "Words 1-30")
        self.assertEqual(lists[-1]["id"], "part-25")
        self.assertEqual(lists[-1]["description"], "Words 721-735")

    def test_chunk_ids_are_padded_and_ordered(self) -> None:
        lists = build.chunk_lists(self.make_words(400), 30)
        self.assertEqual([item["id"] for item in lists[:3]], ["part-01", "part-02", "part-03"])
        flattened = [word_id for item in lists for word_id in item["word_ids"]]
        source = [word["id"] for word in self.make_words(400)]
        self.assertEqual(flattened, source)
        self.assertEqual(len(set(flattened)), len(source))

    def test_remainder_only_kept_when_non_empty(self) -> None:
        self.assertEqual(len(build.chunk_lists(self.make_words(60), 30)), 2)
        self.assertEqual(len(build.chunk_lists(self.make_words(1), 30)), 1)
        self.assertEqual(build.chunk_lists([], 30), [])
        self.assertEqual(build.chunk_lists(self.make_words(10), 0), [])

    def test_chunk_size_from_lists_file(self) -> None:
        write_json(self.words_path, {"schema_version": 1, "words": self.make_words(50)})
        write_json(self.lists_path, {"schema_version": 1, "chunk_size": 20, "lists": []})
        words = build.load_words(self.words_path)[0]["words"]
        list_data, _, _ = build.load_lists(words, self.lists_path)
        parts = [item for item in list_data["lists"] if item["id"].startswith("part-")]
        self.assertEqual([len(item["word_ids"]) for item in parts], [20, 20, 10])

    def test_cli_chunk_size_overrides_file(self) -> None:
        write_json(self.words_path, {"schema_version": 1, "words": self.make_words(50)})
        write_json(self.lists_path, {"schema_version": 1, "chunk_size": 20, "lists": []})
        words = build.load_words(self.words_path)[0]["words"]
        list_data, _, _ = build.load_lists(words, self.lists_path, 25)
        parts = [item for item in list_data["lists"] if item["id"].startswith("part-")]
        self.assertEqual([len(item["word_ids"]) for item in parts], [25, 25])

    def test_build_summary_counts_generated_and_custom_lists(self) -> None:
        write_json(self.words_path, {"schema_version": 1, "words": self.make_words(70)})
        write_json(
            self.lists_path,
            {
                "schema_version": 1,
                "chunk_size": 30,
                "lists": [{"id": "favourites", "name": "Favourites", "word_ids": ["w000"]}],
            },
        )
        result = build.build(
            out_path=self.tmp / "out.html",
            words_path=self.words_path,
            lists_path=self.lists_path,
            log=lambda *args: None,
        )
        self.assertEqual(result["words"], 70)
        self.assertEqual(result["generated_lists"], 4)  # all + part-01..03
        self.assertEqual(result["custom_lists"], 1)
        self.assertEqual(result["lists"], 5)

    def test_check_only_writes_nothing(self) -> None:
        write_json(self.words_path, {"schema_version": 1, "words": self.make_words(5)})
        out_path = self.tmp / "out.html"
        result = build.build(
            out_path=out_path,
            words_path=self.words_path,
            lists_path=self.lists_path,
            check_only=True,
            log=lambda *args: None,
        )
        self.assertIsNone(result["written"])
        self.assertFalse(out_path.exists())


class RealDataTest(unittest.TestCase):
    def test_shipped_lists_reference_real_words(self) -> None:
        vocabulary = build.load_words(build.WORDS_PATH)[0]["words"]
        list_data, warnings, _ = build.load_lists(vocabulary, build.LISTS_PATH)
        ids = [word["id"] for word in vocabulary]
        known = set(ids)
        self.assertTrue(list_data["lists"], "build 至少要生成一个 list")

        all_list = list_data["lists"][0]
        self.assertEqual(all_list["id"], "all")
        self.assertEqual(all_list["word_ids"], ids)

        parts = [item for item in list_data["lists"] if item["id"].startswith("part-")]
        flattened = [word_id for item in parts for word_id in item["word_ids"]]
        self.assertEqual(flattened, ids, "自动 list 必须覆盖整个词库且顺序一致")
        self.assertEqual(len(set(flattened)), len(ids))
        for item in parts[:-1]:
            self.assertEqual(len(item["word_ids"]), build.DEFAULT_CHUNK_SIZE)
        self.assertTrue(parts[-1]["word_ids"])
        self.assertLessEqual(len(parts[-1]["word_ids"]), build.DEFAULT_CHUNK_SIZE)

        for item in list_data["lists"]:
            self.assertTrue(set(item["word_ids"]) <= known, f"{item['id']} 含未知 word_id")
            self.assertEqual(len(item["word_ids"]), len(set(item["word_ids"])))
        self.assertEqual([warning for warning in warnings if "不在 words.json" in warning], [])

    def test_shipped_lists_file_is_config_only(self) -> None:
        payload = json.loads(build.LISTS_PATH.read_text(encoding="utf-8"))
        self.assertEqual(payload.get("chunk_size"), build.DEFAULT_CHUNK_SIZE)
        for item in payload.get("lists", []):
            self.assertNotRegex(item["id"], r"^(?:all|part-\d+)$")


class DiscardFeatureTest(unittest.TestCase):
    """丢弃标记是唯一持久化的状态；session 进度仍然只在内存里。"""

    def test_local_storage_is_used_only_for_discard_marks(self) -> None:
        source = build.JS_PATH.read_text(encoding="utf-8")
        self.assertIn("gre-vocab:discarded:v1", source)
        self.assertLessEqual(source.count("window.localStorage"), 5)
        for forbidden in ("indexedDB", "document.cookie", "sessionStorage"):
            self.assertNotIn(forbidden, source)

    def test_discard_ui_is_wired(self) -> None:
        source = build.JS_PATH.read_text(encoding="utf-8")
        for token in (
            "toggle-discard",
            "copy-discarded",
            "clear-discarded",
            "discardToggleMarkup",
            "Discarded words",
            "Copy discarded words",
        ):
            self.assertIn(token, source)
        self.assertIn("data-act=\"toggle-discard\"", source)

    def test_discarded_manager_lives_on_export_only(self) -> None:
        source = build.JS_PATH.read_text(encoding="utf-8")
        self.assertEqual(source.count("discardedSectionMarkup()"), 2)  # definition + one call
        render_lists = source.split("function renderLists()")[1].split("\n  function ")[0]
        self.assertNotIn("discardedSectionMarkup", render_lists)
        render_export = source.split("function renderExport()")[1].split("\n  /* ---")[0]
        self.assertIn("discardedSectionMarkup()", render_export)

    def test_built_page_ships_the_feature(self) -> None:
        out_path = shipped_page_path()
        if not out_path.exists():  # pragma: no cover - build not run yet
            self.skipTest(f"{out_path.name} 尚未生成")
        html = build.read_text(out_path)
        self.assertIn("gre-vocab:discarded:v1", html)
        self.assertIn("Copy discarded words", html)
        self.assertIn("D = discard", html)


class SessionInteractionTest(unittest.TestCase):
    """Flashcard 交互：无 Show answer、评分不跳词、翻面看中文、score 生命周期。"""

    def setUp(self) -> None:
        self.source = build.JS_PATH.read_text(encoding="utf-8")

    def test_show_answer_is_gone(self) -> None:
        self.assertNotIn("Show answer", self.source)
        self.assertNotIn("revealAnswer", self.source)
        self.assertNotIn("data-act=\"reveal\"", self.source)
        out_path = shipped_page_path()
        if out_path.exists():
            self.assertNotIn("Show answer", build.read_text(out_path))

    def test_rating_buttons_are_always_rendered(self) -> None:
        render_session = self.source.split("function renderSession()")[1].split("\n  /* ---")[0]
        for kind in ("unknown", "vague", "known"):
            self.assertIn(f"rateButtonMarkup('{kind}'", render_session)
        self.assertNotIn("Show answer", render_session)
        self.assertIn("data-act=\"flip-card\"", render_session)
        self.assertIn("data-act=\"previous\"", render_session)
        self.assertIn("data-act=\"next\"", render_session)

    def test_rate_does_not_advance_and_is_idempotent_per_round(self) -> None:
        rate_body = self.source.split("function rate(kind)")[1].split("function commitRound")[0]
        self.assertNotIn("goNext", rate_body)
        self.assertNotIn("session.pos += 1", rate_body)
        self.assertIn("session.roundWorst", rate_body)

    def test_navigation_is_separate_and_round_loop_unchanged(self) -> None:
        self.assertIn("function goNext()", self.source)
        self.assertIn("function goPrevious()", self.source)
        end_round = self.source.split("function endRound()")[1].split("function buildResult()")[0]
        self.assertIn("commitRound()", end_round)
        self.assertIn("session.status[id] !== 'known'", end_round)
        self.assertIn("finishSession()", end_round)

    def test_card_flip_and_chinese_reveal(self) -> None:
        self.assertIn("function toggleCardFace()", self.source)
        self.assertIn("function toggleChinese()", self.source)
        self.assertIn("chineseRevealMarkup", self.source)
        self.assertIn("chinese-blank", self.source)
        flip = self.source.split("function toggleCardFace()")[1].split("function toggleChinese")[0]
        self.assertIn("session.chineseShown = false", flip)

    def test_score_lifecycle_is_session_scoped(self) -> None:
        self.assertIn("gre-vocab:scores:v1", self.source)
        self.assertIn("function persistSessionScores", self.source)
        finish = self.source.split("function finishSession()")[1].split("\n  }")[0]
        self.assertIn("persistSessionScores(session.result)", finish)
        exit_body = self.source.split("function exitSession(")[1].split("\n  }")[0]
        self.assertNotIn("persistSessionScores", exit_body)
        self.assertIn("UNPRACTICED = -1", self.source)

    def test_export_tab_and_buckets(self) -> None:
        self.assertIn("{ name: 'export', label: 'Export', hash: '#/export' }", self.source)
        self.assertIn("function renderExport()", self.source)
        self.assertIn("function donutMarkup(stats)", self.source)
        for label in ("'0'", "'1-3'", "'4-6'", "'7-9'", "'10'"):
            self.assertIn(label, self.source)
        self.assertIn("data-act=\"copy-bucket\"", self.source)

    def test_built_page_ships_the_interaction(self) -> None:
        out_path = shipped_page_path()
        if not out_path.exists():  # pragma: no cover
            self.skipTest(f"{out_path.name} 尚未生成")
        html = build.read_text(out_path)
        self.assertIn("gre-vocab:scores:v1", html)
        self.assertIn("Score statistics", html)
        self.assertIn("tap to flip", html)
        self.assertIn("Chinese meaning", html)


class OutputNamingTest(BuildFixtureTest):
    """产物命名：默认 [语言简称]-[词数].html，也可用 --name / --out 指定，同名直接覆盖。"""

    def test_default_name_uses_language_and_word_count(self) -> None:
        self.assertEqual(build.default_out_name(735), "En-735.html")
        self.assertEqual(build.default_out_name(3, "Fr"), "Fr-3.html")
        self.assertEqual(build.default_out_path(735), build.DIST_DIR / "En-735.html")
        self.assertEqual(build.default_out_path(1, "Zh").name, "Zh-1.html")

    def test_shipped_path_matches_the_current_wordbank(self) -> None:
        count = len(build.load_words()[0]["words"])
        self.assertEqual(shipped_page_path().name, f"En-{count}.html")

    def test_name_option_resolves_into_dist(self) -> None:
        self.assertEqual(build.resolve_out_path(name="my-tool"), build.DIST_DIR / "my-tool.html")
        self.assertEqual(build.resolve_out_path(name="my-tool.html"), build.DIST_DIR / "my-tool.html")
        self.assertEqual(build.resolve_out_path(name="Mixed.HTML"), build.DIST_DIR / "Mixed.HTML")
        self.assertIsNone(build.resolve_out_path())

    def test_out_option_wins_over_name(self) -> None:
        explicit = Path("some/where/custom.html")
        self.assertEqual(build.resolve_out_path(out=explicit, name="ignored"), explicit)

    def test_default_path_is_used_when_nothing_is_given(self) -> None:
        write_json(
            self.words_path,
            {"schema_version": 1, "words": [minimal_word("alpha", "alpha"), minimal_word("beta", "beta")]},
        )
        expected = build.DIST_DIR / "TestLang-2.html"
        self.addCleanup(lambda: expected.unlink(missing_ok=True))
        result = build.build(
            words_path=self.words_path,
            lists_path=self.lists_path,
            language_code="TestLang",
            log=lambda *args: None,
        )
        self.assertEqual(result["written"], str(expected))
        self.assertTrue(expected.exists())
        self.assertIn("const VOCABULARY_DATA = ", expected.read_text(encoding="utf-8"))

    def test_existing_file_is_silently_overwritten(self) -> None:
        write_json(self.words_path, {"schema_version": 1, "words": [minimal_word("alpha", "alpha")]})
        out_path = self.tmp / "same-name.html"
        out_path.write_text("stale content", encoding="utf-8")
        for _ in range(2):
            result = build.build(
                out_path=out_path,
                words_path=self.words_path,
                lists_path=self.lists_path,
                log=lambda *args: None,
            )
            self.assertEqual(result["written"], str(out_path))
        html = out_path.read_text(encoding="utf-8")
        self.assertNotIn("stale content", html)
        self.assertTrue(html.startswith("<!DOCTYPE html>"))

    def test_check_only_reports_the_target_path_without_writing(self) -> None:
        write_json(self.words_path, {"schema_version": 1, "words": [minimal_word("alpha", "alpha")]})
        expected = build.DIST_DIR / "CheckLang-1.html"
        result = build.build(
            words_path=self.words_path,
            lists_path=self.lists_path,
            language_code="CheckLang",
            check_only=True,
            log=lambda *args: None,
        )
        self.assertEqual(result["out_path"], str(expected))
        self.assertIsNone(result["written"])
        self.assertFalse(expected.exists())


class UiLanguageTest(unittest.TestCase):

    def test_app_js_has_no_cjk_in_string_literals(self) -> None:
        source = build.JS_PATH.read_text(encoding="utf-8")
        literals = js_string_literals(source)
        self.assertGreater(len(literals), 100, "字符串扫描器没有正常工作")
        offenders = [item for item in literals if CJK_RE.search(item)]
        self.assertEqual(offenders, [], "UI 字符串里仍有中文")

    def test_app_css_has_no_cjk_outside_comments(self) -> None:
        css = re.sub(r"/\*.*?\*/", "", build.CSS_PATH.read_text(encoding="utf-8"), flags=re.S)
        self.assertIsNone(CJK_RE.search(css))

    def test_template_has_no_cjk_outside_comments(self) -> None:
        template = build.TEMPLATE_PATH.read_text(encoding="utf-8")
        stripped = re.sub(r"/\*.*?\*/", "", template, flags=re.S)
        stripped = re.sub(r"<!--.*?-->", "", stripped, flags=re.S)
        self.assertIsNone(CJK_RE.search(stripped))

    def test_built_page_keeps_vocabulary_chinese(self) -> None:
        out_path = shipped_page_path()
        if not out_path.exists():  # pragma: no cover - build not run yet
            self.skipTest(f"{out_path.name} 尚未生成")
        self.assertIn("减少", build.read_text(out_path), "词库中文释义应当保留在页面数据中")


if __name__ == "__main__":
    unittest.main()
