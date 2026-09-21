from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator

from wordbank import (
    ALLOWED_POS,
    OBJECTIVE_FLAGS,
    REVIEW_PATH,
    ROOT,
    WORDS_PATH,
    atomic_write_json,
    clean_text,
    load_wordbank,
    save_wordbank,
    unique_strings,
)


VALIDATOR_FLAGS = {
    "missing_chinese",
    "missing_definition",
    "missing_example",
    "invalid_pos",
    "self_reference",
    "source_incomplete",
    "schema_error",
}


def _source_incomplete(source: Any) -> bool:
    return not isinstance(source, dict) or not clean_text(source.get("name")) or not source.get("url")


def flags_for_word(word: dict[str, Any]) -> list[str]:
    flags: list[str] = []
    term = clean_text(word.get("term"))
    term_key = term.casefold()
    if not term:
        flags.append("ambiguous_term")
    pos = word.get("pos")
    if not isinstance(pos, list) or not pos or any(item not in ALLOWED_POS for item in pos):
        flags.append("invalid_pos")
    if not word.get("chinese"):
        flags.append("missing_chinese")
    if not word.get("english_definitions"):
        flags.append("missing_definition")
    if not word.get("examples"):
        flags.append("missing_example")

    synonyms = word.get("synonyms") or []
    related = [
        *(item.get("term", "") for item in word.get("derivatives") or [] if isinstance(item, dict)),
        *(item.get("term", "") for item in word.get("confusables") or [] if isinstance(item, dict)),
    ]
    if any(clean_text(item).casefold() == term_key for item in [*synonyms, *related]):
        flags.append("self_reference")

    for example in word.get("examples") or []:
        required = {"sentence", "translation", "source_type", "author", "url", "license"}
        if not isinstance(example, dict) or required.difference(example):
            flags.append("source_incomplete")
            continue
        if not clean_text(example.get("sentence")) or not clean_text(example.get("source_type")):
            flags.append("source_incomplete")
        if example.get("source_type") == "tatoeba" and (
            not example.get("url") or not example.get("license")
        ):
            flags.append("source_incomplete")

    sources = word.get("sources") or {}
    for bucket in ("dictionary", "synonyms", "translation", "examples"):
        if not isinstance(sources.get(bucket), list):
            flags.append("source_incomplete")
            continue
        if any(_source_incomplete(source) for source in sources[bucket]):
            flags.append("source_incomplete")
    return unique_strings(flags)


def validate_payload(
    payload: dict[str, Any], schema: dict[str, Any]
) -> tuple[list[str], list[dict[str, Any]]]:
    hard_errors: list[str] = []
    words = payload.get("words")
    if not isinstance(words, list):
        return ["top-level 'words' must be an array"], []

    ids: Counter[str] = Counter()
    terms: Counter[str] = Counter()
    for word in words:
        if isinstance(word, dict):
            ids[clean_text(word.get("id"))] += 1
            terms[clean_text(word.get("term")).casefold()] += 1
    for value, count in ids.items():
        if value and count > 1:
            hard_errors.append(f"duplicate id: {value} ({count})")
    for value, count in terms.items():
        if value and count > 1:
            hard_errors.append(f"duplicate term: {value} ({count})")

    schema_errors_by_index: dict[int, list[str]] = {}
    for error in Draft202012Validator(schema).iter_errors(payload):
        path = list(error.absolute_path)
        if len(path) >= 2 and path[0] == "words" and isinstance(path[1], int):
            schema_errors_by_index.setdefault(path[1], []).append(error.message)
        else:
            hard_errors.append(f"schema {list(error.absolute_path)}: {error.message}")

    review: list[dict[str, Any]] = []
    for index, word in enumerate(words):
        if not isinstance(word, dict):
            hard_errors.append(f"words[{index}] is not an object")
            continue
        existing = [flag for flag in word.get("review_flags", []) if flag not in VALIDATOR_FLAGS]
        computed = flags_for_word(word)
        if index in schema_errors_by_index:
            computed.append("schema_error")
            hard_errors.extend(
                f"words[{index}] {message}" for message in schema_errors_by_index[index]
            )
        word["review_flags"] = unique_strings([*existing, *computed])
        if word["review_flags"]:
            review.append(
                {
                    "id": word.get("id"),
                    "term": word.get("term"),
                    "review_flags": word["review_flags"],
                    "personal_notes": word.get("personal_notes", []),
                }
            )
    return hard_errors, review


def write_review_queue(review: list[dict[str, Any]], path: Path = REVIEW_PATH) -> None:
    counts = Counter(flag for item in review for flag in item["review_flags"])
    atomic_write_json(
        path,
        {
            "schema_version": 1,
            "summary": {
                "flagged_words": len(review),
                "flag_occurrences": sum(counts.values()),
                "by_flag": dict(sorted(counts.items(), key=lambda item: (-item[1], item[0]))),
            },
            "words": review,
        },
    )


def validate_files(
    words_path: Path = WORDS_PATH,
    schema_path: Path = ROOT / "data" / "words.schema.json",
    review_path: Path = REVIEW_PATH,
    *,
    write: bool = False,
) -> tuple[list[str], list[dict[str, Any]]]:
    payload = load_wordbank(words_path)
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    hard_errors, review = validate_payload(payload, schema)
    if write:
        save_wordbank(payload, words_path)
        write_review_queue(review, review_path)
    return hard_errors, review


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Validate data/words.json and build the review queue.")
    parser.add_argument("--words", type=Path, default=WORDS_PATH)
    parser.add_argument("--schema", type=Path, default=ROOT / "data" / "words.schema.json")
    parser.add_argument("--review-queue", type=Path, default=REVIEW_PATH)
    parser.add_argument("--write", action="store_true", help="write computed flags and review_queue.json")
    args = parser.parse_args(argv)
    hard_errors, review = validate_files(
        args.words, args.schema, args.review_queue, write=args.write
    )
    counts = Counter(flag for item in review for flag in item["review_flags"])
    print(f"words: {len(load_wordbank(args.words).get('words', []))}")
    print(f"flagged words: {len(review)}")
    print(f"flag occurrences: {sum(counts.values())}")
    for flag, count in counts.most_common():
        marker = "" if flag in OBJECTIVE_FLAGS else " (custom)"
        print(f"  {flag}: {count}{marker}")
    if hard_errors:
        print("hard validation errors:", file=sys.stderr)
        for error in hard_errors:
            print(f"  - {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
