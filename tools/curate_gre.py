"""Apply hand-curated, GRE-oriented corrections to ``data/words.json``.

The editorial decisions live in ``data/curation/*.json`` so that they stay
reviewable and re-runnable. Every entry keyed by term may override the
learner-facing fields of that word and clear review flags whose root cause has
been fixed. Provenance (``sources``) and the learner's own ``personal_notes``
are always preserved; curated notes are appended to them.

Curation file shape::

    {
      "words": {
        "belie": {
          "pos": ["verb"],
          "chinese": ["显出…的虚假；与…不符"],
          "english_definitions": [
            {"pos": "verb", "definition": "To show (something) to be false."}
          ],
          "synonyms": ["contradict", "give the lie to"],
          "example": {"sentence": "...", "translation": "..."},
          "confusables": [{"term": "belie", "distinction": "..."}],
          "derivatives": [{"term": "belied", "relation": "past tense"}],
          "notes": ["GRE 常考搭配：X belies Y（X 掩盖/推翻了 Y）"],
          "clear_flags": ["api_mismatch"],
          "lemma": "belie"
        }
      }
    }
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from wordbank import (
    ROOT,
    WORDS_PATH,
    load_wordbank,
    save_wordbank,
    unique_objects,
    unique_strings,
)

CURATION_DIR = ROOT / "data" / "curation"

EXAMPLE_SOURCE_TYPE = "gre_authored"

EDITABLE_FIELDS = (
    "lemma",
    "pos",
    "chinese",
    "english_definitions",
    "synonyms",
    "derivatives",
    "confusables",
)

FLAG_FIELDS = (
    "possible_wrong_synonym",
    "ambiguous_term",
    "multiple_possible_pos",
    "api_mismatch",
    "possible_wrong_derivative",
    "possible_confusable",
    "missing_definition",
)


def load_curation(directory: Path = CURATION_DIR) -> dict[str, dict[str, Any]]:
    """Merge every ``data/curation/*.json`` file into one term-keyed mapping."""
    merged: dict[str, dict[str, Any]] = {}
    for path in sorted(directory.glob("*.json")):
        payload = json.loads(path.read_text(encoding="utf-8"))
        for term, entry in (payload.get("words") or {}).items():
            key = term.casefold()
            target = merged.setdefault(key, {})
            for field, value in entry.items():
                if field in {"confusables", "derivatives"} and target.get(field):
                    keys = ("term", "distinction") if field == "confusables" else ("term", "relation")
                    target[field] = unique_objects([*target[field], *value], keys)
                elif field == "notes" and target.get(field):
                    target[field] = unique_strings([*target[field], *value])
                else:
                    target[field] = value
    return merged


def _authored_example(entry: dict[str, Any]) -> dict[str, Any] | None:
    example = entry.get("example")
    if not example:
        return None
    return {
        "sentence": example["sentence"].strip(),
        "translation": (example.get("translation") or "").strip() or None,
        "source_type": entry.get("example_source_type", EXAMPLE_SOURCE_TYPE),
        "author": None,
        "url": None,
        "license": None,
    }


def apply_entry(word: dict[str, Any], entry: dict[str, Any]) -> list[str]:
    """Mutate ``word`` in place, returning a list of changed field names."""
    changed: list[str] = []

    for field in EDITABLE_FIELDS:
        value = entry.get(field)
        if not value:
            continue
        if field == "chinese":
            normalized: Any = unique_strings(value)
        elif field == "synonyms":
            normalized = unique_strings(value)[:12]
        else:
            normalized = value
        if word.get(field) != normalized:
            word[field] = normalized
            changed.append(field)

    example = _authored_example(entry)
    if example:
        before_examples = list(word.get("examples", []))
        if entry.get("example_mode") == "replace":
            word["examples"] = [example]
        else:
            # The curated sentence becomes the primary illustration.
            word["examples"] = unique_objects([example, *before_examples], ("sentence",))
        if word["examples"] != before_examples:
            changed.append("examples")

    notes = unique_strings(entry.get("notes") or [])
    if notes:
        before = list(word.get("personal_notes", []))
        word["personal_notes"] = unique_strings([*before, *notes])
        if word["personal_notes"] != before:
            changed.append("personal_notes")

    clear = set(entry.get("clear_flags") or [])
    if clear:
        before_flags = list(word.get("review_flags", []))
        word["review_flags"] = [flag for flag in before_flags if flag not in clear]
        if word["review_flags"] != before_flags:
            changed.append("review_flags")

    for extra in entry.get("add_flags") or []:
        word["review_flags"] = unique_strings([*word.get("review_flags", []), extra])
        changed.append("review_flags")

    # A word must never list itself as a synonym or a confusable.
    term_key = word["term"].casefold()
    word["synonyms"] = [item for item in word["synonyms"] if item.casefold() != term_key]
    word["confusables"] = [
        item for item in word["confusables"] if item.get("term", "").casefold() != term_key
    ]
    return changed


def curate(
    payload: dict[str, Any], curation: dict[str, dict[str, Any]]
) -> tuple[int, list[str]]:
    """Apply ``curation`` to ``payload``; return (touched, unknown terms)."""
    by_term = {word["term"].casefold(): word for word in payload.get("words", [])}
    touched = 0
    for key, entry in curation.items():
        word = by_term.get(key)
        if word is None:
            continue
        if apply_entry(word, entry):
            touched += 1
    unknown = sorted(key for key in curation if key not in by_term)
    return touched, unknown


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Apply GRE-oriented curation to data/words.json.")
    parser.add_argument("--words", type=Path, default=WORDS_PATH)
    parser.add_argument("--curation-dir", type=Path, default=CURATION_DIR)
    parser.add_argument("--check", action="store_true", help="report drift without writing")
    parser.add_argument("--allow-unknown", action="store_true", help="ignore curated terms missing from the wordbank")
    args = parser.parse_args(argv)

    curation = load_curation(args.curation_dir)
    payload = load_wordbank(args.words)
    touched, unknown = curate(payload, curation)
    print(f"curation entries: {len(curation)}; words changed: {touched}")
    if unknown:
        print(f"curated terms not present in the wordbank ({len(unknown)}):", file=sys.stderr)
        for term in unknown:
            print(f"  - {term}", file=sys.stderr)
        if not args.allow_unknown:
            return 1
    if args.check:
        return 0
    save_wordbank(payload, args.words)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
