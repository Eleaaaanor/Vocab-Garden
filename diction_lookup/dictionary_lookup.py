from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime, timezone
from typing import Any, Iterable, Iterator
from urllib.parse import quote

import requests
from bs4 import BeautifulSoup, NavigableString, Tag
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry


FREE_DICTIONARY_URL = "https://freedictionaryapi.com/api/v1/entries/en/{word}"
DATAMUSE_URL = "https://api.datamuse.com/words"
TATOEBA_URL = "https://api.tatoeba.org/v1/sentences"
ZH_WIKTIONARY_URL = "https://zh.wiktionary.org/w/api.php"

USER_AGENT = (
    "LocalVocabularyLookup/0.1 "
    "(personal vocabulary research; low-rate API client)"
)


class DictionaryLookupError(RuntimeError):
    """Raised when required dictionary data cannot be obtained."""


class WordNotFoundError(DictionaryLookupError):
    """Raised when the primary dictionary does not contain the word."""


def _clean_text(value: Any) -> str:
    return " ".join(str(value or "").split())


def _unique(values: Iterable[str], limit: int | None = None) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        cleaned = _clean_text(value)
        key = cleaned.casefold()
        if not cleaned or key in seen:
            continue
        seen.add(key)
        result.append(cleaned)
        if limit is not None and len(result) >= limit:
            break
    return result


def _looks_like_lexical_term(value: str) -> bool:
    letters = [character for character in value if character.isalpha()]
    if not letters:
        return False
    # Mixed internal capitalization in a lower-case lookup is commonly noisy
    # Wiktionary shorthand rather than a useful learner-facing synonym.
    return not (any(character.islower() for character in letters) and any(
        character.isupper() for character in letters[1:]
    ))


def _iter_senses(senses: Iterable[dict[str, Any]]) -> Iterator[dict[str, Any]]:
    for sense in senses:
        yield sense
        yield from _iter_senses(sense.get("subsenses") or [])


def _contains_cjk(value: str) -> bool:
    return bool(re.search(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]", value))


def _validate_word(word: str) -> str:
    normalized = " ".join(word.strip().split())
    if not normalized:
        raise ValueError("word must not be empty")
    if len(normalized) > 80:
        raise ValueError("word must be at most 80 characters")
    if not all(character.isalpha() or character in " -'" for character in normalized):
        raise ValueError("word may only contain letters, spaces, apostrophes, and hyphens")
    return normalized


def _direct_definition_text(item: Tag) -> str:
    skipped = {"dl", "ol", "ul", "table", "style", "script", "sup"}

    def walk(node: Tag) -> Iterator[str]:
        for child in node.children:
            if isinstance(child, NavigableString):
                yield str(child)
            elif isinstance(child, Tag) and child.name not in skipped:
                yield from walk(child)

    return _clean_text("".join(walk(item)))


def _is_part_of_speech_heading(heading: Tag | None) -> bool:
    if heading is None or heading.name not in {"h3", "h4", "h5"}:
        return False
    text = _clean_text(heading.get_text(" ", strip=True))
    markers = {
        "名词",
        "名詞",
        "专有名词",
        "專有名詞",
        "动词",
        "動詞",
        "形容词",
        "形容詞",
        "副词",
        "副詞",
        "代词",
        "代詞",
        "介词",
        "介詞",
        "连词",
        "連詞",
        "限定词",
        "限定詞",
        "数词",
        "數詞",
        "冠词",
        "冠詞",
        "感叹词",
        "感嘆詞",
        "叹词",
        "歎詞",
        "助词",
        "助詞",
        "短语",
        "短語",
        "词组",
        "詞組",
        "缩写",
        "縮寫",
    }
    return any(marker in text for marker in markers)


def _is_level_two_heading(node: Tag) -> bool:
    if node.name == "h2":
        return True
    classes = set(node.get("class") or [])
    return "mw-heading2" in classes or node.find("h2", recursive=False) is not None


def _extract_zh_wiktionary_definitions(html: str, limit: int) -> list[str]:
    soup = BeautifulSoup(html, "html.parser")
    english_heading = next(
        (
            heading
            for heading in soup.find_all("h2")
            if _clean_text(heading.get_text(" ", strip=True)) in {"英语", "英語", "英文"}
        ),
        None,
    )
    if english_heading is None:
        return []

    heading_container = english_heading
    parent_classes = set(english_heading.parent.get("class") or [])
    if "mw-heading" in parent_classes:
        heading_container = english_heading.parent

    definitions: list[str] = []
    for sibling in heading_container.next_siblings:
        if not isinstance(sibling, Tag):
            continue
        if _is_level_two_heading(sibling):
            break

        ordered_lists = [sibling] if sibling.name == "ol" else []
        ordered_lists.extend(sibling.find_all("ol"))
        for ordered_list in ordered_lists:
            if ordered_list.find_parent("ol") is not None:
                continue
            if not _is_part_of_speech_heading(
                ordered_list.find_previous(["h2", "h3", "h4", "h5"])
            ):
                continue
            for item in ordered_list.find_all("li", recursive=False):
                definition = _direct_definition_text(item)
                if definition:
                    definitions.append(definition)

    return _unique(definitions, limit)


class DictionaryClient:
    """Reusable low-rate client for one-word dictionary lookups."""

    def __init__(
        self,
        *,
        timeout: tuple[float, float] = (5.0, 25.0),
        max_definitions_per_pos: int = 3,
        max_chinese_meanings: int = 8,
        max_synonyms: int = 12,
        max_examples: int = 3,
    ) -> None:
        self.timeout = timeout
        self.max_definitions_per_pos = max_definitions_per_pos
        self.max_chinese_meanings = max_chinese_meanings
        self.max_synonyms = max_synonyms
        self.max_examples = max_examples

        retry = Retry(
            total=2,
            connect=2,
            read=2,
            status=2,
            backoff_factor=0.5,
            status_forcelist=(429, 500, 502, 503, 504),
            allowed_methods=frozenset({"GET"}),
            respect_retry_after_header=True,
            raise_on_status=False,
        )
        adapter = HTTPAdapter(max_retries=retry)
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": USER_AGENT, "Accept": "application/json"})
        self.session.mount("https://", adapter)

    def __enter__(self) -> "DictionaryClient":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def close(self) -> None:
        self.session.close()

    def _get_json(
        self,
        url: str,
        *,
        params: dict[str, Any] | None = None,
        not_found_ok: bool = False,
    ) -> dict[str, Any] | list[Any] | None:
        try:
            response = self.session.get(url, params=params, timeout=self.timeout)
        except requests.RequestException as error:
            raise DictionaryLookupError(f"request failed for {url}: {error}") from error

        if not_found_ok and response.status_code == 404:
            return None
        try:
            response.raise_for_status()
            return response.json()
        except requests.RequestException as error:
            raise DictionaryLookupError(
                f"HTTP {response.status_code} returned by {response.url}"
            ) from error
        except ValueError as error:
            raise DictionaryLookupError(f"invalid JSON returned by {response.url}") from error

    def _primary_lookup(self, word: str) -> dict[str, Any]:
        url = FREE_DICTIONARY_URL.format(word=quote(word, safe="-'"))
        payload = self._get_json(
            url,
            params={"translations": "true"},
            not_found_ok=True,
        )
        if payload is None:
            raise WordNotFoundError(f"word not found: {word}")
        if not isinstance(payload, dict):
            raise DictionaryLookupError("FreeDictionaryAPI returned an unexpected response")
        return payload

    def _extract_primary(
        self, payload: dict[str, Any], word: str
    ) -> tuple[list[dict[str, Any]], list[str], list[str], list[str]]:
        entries = payload.get("entries") or []
        definitions_by_pos: dict[str, list[str]] = {}
        translations: dict[str, list[str]] = {"cmn": [], "zh": []}
        synonyms: list[str] = []
        examples: list[str] = []

        for entry in entries:
            part_of_speech = _clean_text(entry.get("partOfSpeech"))
            if part_of_speech:
                definitions_by_pos.setdefault(part_of_speech, [])
            selected_from_entry = False

            for sense in _iter_senses(entry.get("senses") or []):
                definition = _clean_text(sense.get("definition"))
                if part_of_speech and definition:
                    selected_definitions = definitions_by_pos[part_of_speech]
                    already_selected = any(
                        item.casefold() == definition.casefold()
                        for item in selected_definitions
                    )
                    if (
                        not already_selected
                        and len(selected_definitions) < self.max_definitions_per_pos
                    ):
                        selected_definitions.append(definition)
                        selected_from_entry = True
                        synonyms.extend(sense.get("synonyms") or [])
                        examples.extend(sense.get("examples") or [])

                for translation in sense.get("translations") or []:
                    language_code = _clean_text(
                        (translation.get("language") or {}).get("code")
                    )
                    translated_word = _clean_text(translation.get("word"))
                    if language_code in translations and _contains_cjk(translated_word):
                        translations[language_code].append(translated_word)

            if selected_from_entry:
                synonyms.extend(entry.get("synonyms") or [])

        english = [
            {
                "part_of_speech": part_of_speech,
                "definitions": definitions,
            }
            for part_of_speech, definitions in definitions_by_pos.items()
            if definitions
        ]
        chinese = _unique(
            [*translations["cmn"], *translations["zh"]],
            self.max_chinese_meanings,
        )
        filtered_synonyms = [
            synonym
            for synonym in _unique(synonyms)
            if synonym.casefold() != word.casefold()
            and _looks_like_lexical_term(synonym)
        ]
        return english, chinese, filtered_synonyms, _unique(examples)

    def _wiktionary_chinese(self, word: str) -> tuple[list[str], str]:
        payload = self._get_json(
            ZH_WIKTIONARY_URL,
            params={
                "action": "parse",
                "page": word,
                "prop": "text|sections",
                "format": "json",
                "formatversion": "2",
                "redirects": "1",
                "disableeditsection": "1",
                "disablelimitreport": "1",
            },
        )
        if not isinstance(payload, dict) or "parse" not in payload:
            return [], ""
        definitions = _extract_zh_wiktionary_definitions(
            payload["parse"].get("text") or "",
            self.max_chinese_meanings,
        )
        page_url = "https://zh.wiktionary.org/wiki/" + quote(word.replace(" ", "_"))
        return definitions, page_url

    def _datamuse_synonyms(self, word: str, parts_of_speech: set[str]) -> list[str]:
        payload = self._get_json(
            DATAMUSE_URL,
            params={"rel_syn": word, "md": "pf", "max": 30},
        )
        if not isinstance(payload, list):
            return []

        pos_codes = {
            "noun": "n",
            "verb": "v",
            "adjective": "adj",
            "adverb": "adv",
        }
        accepted_codes = {
            pos_codes[part]
            for part in parts_of_speech
            if part in pos_codes
        }
        result: list[str] = []
        for item in payload:
            candidate = _clean_text(item.get("word"))
            tags = set(item.get("tags") or [])
            candidate_pos = tags.intersection(set(pos_codes.values()))
            if accepted_codes and candidate_pos and not accepted_codes.intersection(candidate_pos):
                continue
            if candidate and candidate.casefold() != word.casefold():
                result.append(candidate)
        return _unique(result)

    def _tatoeba_examples(self, word: str) -> list[dict[str, Any]]:
        payload = self._get_json(
            TATOEBA_URL,
            params={
                "lang": "eng",
                "q": word,
                "trans:lang": "cmn",
                "trans:is_direct": "yes",
                "is_unapproved": "no",
                "trans:is_unapproved": "no",
                "sort": "relevance",
                "limit": max(10, self.max_examples * 4),
                "showtrans": "matching",
            },
        )
        if not isinstance(payload, dict):
            return []

        examples: list[dict[str, Any]] = []
        seen: set[str] = set()
        for item in payload.get("data") or []:
            english = _clean_text(item.get("text"))
            if not english or english.casefold() in seen:
                continue
            seen.add(english.casefold())

            translations = [
                translation
                for translation in item.get("translations") or []
                if translation.get("lang") == "cmn"
            ]
            translations.sort(key=lambda value: value.get("script") != "Hans")
            chinese = _clean_text(translations[0].get("text")) if translations else ""
            sentence_id = item.get("id")
            examples.append(
                {
                    "english": english,
                    "chinese": chinese or None,
                    "source": "Tatoeba",
                    "source_url": f"https://tatoeba.org/en/sentences/show/{sentence_id}",
                    "author": item.get("owner"),
                    "license": item.get("license"),
                }
            )
            if len(examples) >= self.max_examples:
                break
        return examples

    def lookup(self, word: str) -> dict[str, Any]:
        word = _validate_word(word)
        warnings: list[str] = []
        sources: list[dict[str, Any]] = []

        primary = self._primary_lookup(word)
        english, chinese, primary_synonyms, primary_examples = self._extract_primary(
            primary, word
        )
        if not english:
            raise DictionaryLookupError(
                f"no English definitions with parts of speech were returned for: {word}"
            )

        source = primary.get("source") or {}
        sources.append(
            {
                "name": "FreeDictionaryAPI",
                "url": source.get("url")
                or FREE_DICTIONARY_URL.format(word=quote(word, safe="-'")),
                "license": (source.get("license") or {}).get("name") or "CC BY-SA 4.0",
            }
        )

        if not chinese:
            try:
                chinese, wiktionary_url = self._wiktionary_chinese(word)
                if chinese:
                    sources.append(
                        {
                            "name": "Chinese Wiktionary",
                            "url": wiktionary_url,
                            "license": "CC BY-SA 4.0",
                        }
                    )
            except DictionaryLookupError as error:
                warnings.append(f"Chinese Wiktionary fallback failed: {error}")

        datamuse_synonyms: list[str] = []
        if len(primary_synonyms) < 3:
            try:
                datamuse_synonyms = self._datamuse_synonyms(
                    word,
                    {item["part_of_speech"] for item in english},
                )
                sources.append(
                    {
                        "name": "Datamuse",
                        "url": f"https://api.datamuse.com/words?rel_syn={quote(word)}",
                        "license": None,
                    }
                )
            except DictionaryLookupError as error:
                warnings.append(f"Datamuse fallback failed: {error}")

        synonyms = _unique(
            [*primary_synonyms, *datamuse_synonyms],
            self.max_synonyms,
        )

        examples: list[dict[str, Any]] = []
        try:
            examples = self._tatoeba_examples(word)
            sources.append(
                {
                    "name": "Tatoeba",
                    "url": "https://tatoeba.org/",
                    "license": "per-sentence license in each example",
                }
            )
        except DictionaryLookupError as error:
            warnings.append(f"Tatoeba fallback failed: {error}")

        for example in primary_examples:
            if len(examples) >= self.max_examples:
                break
            if not 10 <= len(example) <= 300:
                continue
            if any(item["english"].casefold() == example.casefold() for item in examples):
                continue
            examples.append(
                {
                    "english": example,
                    "chinese": None,
                    "source": "Wiktionary via FreeDictionaryAPI",
                    "source_url": sources[0]["url"],
                    "author": None,
                    "license": sources[0]["license"],
                }
            )

        if not chinese:
            warnings.append("No Chinese meaning was returned by the available sources.")
        if not synonyms:
            warnings.append("No strict synonym was returned by the available sources.")
        if not examples:
            warnings.append("No example sentence was returned by the available sources.")

        return {
            "word": word,
            "chinese_meanings": chinese,
            "english": english,
            "synonyms": synonyms,
            "examples": examples,
            "sources": sources,
            "warnings": warnings,
            "retrieved_at": datetime.now(timezone.utc).isoformat(),
        }


def lookup_word(word: str, **client_options: Any) -> dict[str, Any]:
    """Look up one word and return a JSON-serializable dictionary."""
    with DictionaryClient(**client_options) as client:
        return client.lookup(word)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Look up an English word using open/free dictionary APIs."
    )
    parser.add_argument("word", help="English word or phrase to look up")
    parser.add_argument("--compact", action="store_true", help="print compact JSON")
    return parser


def main(argv: list[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    args = _build_parser().parse_args(argv)
    try:
        result = lookup_word(args.word)
    except (ValueError, DictionaryLookupError) as error:
        print(
            json.dumps({"error": str(error)}, ensure_ascii=False),
            file=sys.stderr,
        )
        return 2

    print(
        json.dumps(
            result,
            ensure_ascii=False,
            indent=None if args.compact else 2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
