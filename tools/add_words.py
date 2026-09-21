from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from wordbank import (
    ROOT,
    WORDS_PATH,
    atomic_write_json,
    build_words_from_records,
    chunks,
    curate_api_baseline,
    dictionary_cache_path,
    enrich_batch_with_openai,
    llm_cache_path,
    llm_word_to_record,
    load_wordbank,
    merge_dictionary_result,
    merge_llm_curated,
    merge_word,
    parse_inbox,
    preferred_pos_for_groups,
    save_wordbank,
    unique_strings,
)

sys.path.insert(0, str(ROOT / "diction_lookup"))
from dictionary_lookup import DictionaryClient, DictionaryLookupError  # noqa: E402


def load_json(path: Path) -> dict | None:
    if not path.exists():
        return None
    with path.open(encoding="utf-8") as handle:
        return json.load(handle)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Merge new inbox terms into data/words.json.")
    parser.add_argument("--inbox", type=Path, default=ROOT / "inbox.txt")
    parser.add_argument("--words", type=Path, default=WORDS_PATH)
    parser.add_argument("--batch-size", type=int, default=40)
    parser.add_argument("--model", default=os.environ.get("OPENAI_MODEL"))
    parser.add_argument("--skip-api", action="store_true")
    parser.add_argument("--skip-llm", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    if not 1 <= args.batch_size <= 50:
        parser.error("--batch-size must be between 1 and 50")
    if not args.skip_llm and not args.model:
        parser.error("set OPENAI_MODEL or pass --model (or use --skip-llm)")
    if not args.skip_llm and not os.environ.get("OPENAI_API_KEY"):
        parser.error("OPENAI_API_KEY is required unless --skip-llm is used")

    records = parse_inbox(args.inbox)
    incoming = build_words_from_records(records)
    payload = load_wordbank(args.words)
    existing_by_term = {word["term"].casefold(): word for word in payload["words"]}
    new_words: list[dict] = []
    updated_existing = 0
    for candidate in incoming:
        key = candidate["term"].casefold()
        if key in existing_by_term:
            before = json.dumps(existing_by_term[key], ensure_ascii=False, sort_keys=True)
            existing_by_term[key] = merge_word(existing_by_term[key], candidate)
            if json.dumps(existing_by_term[key], ensure_ascii=False, sort_keys=True) != before:
                updated_existing += 1
        else:
            new_words.append(candidate)

    print(f"inbox rows: {len(records)}; new terms: {len(new_words)}; existing terms updated: {updated_existing}")
    if args.dry_run:
        for word in new_words:
            print(word["term"])
        return 0

    if not args.skip_api:
        preferred_synonyms = {
            word["term"].casefold(): list(word.get("synonyms", [])) for word in new_words
        }
        preferred_chinese = {
            word["term"].casefold(): list(word.get("chinese", [])) for word in new_words
        }
        with DictionaryClient() as client:
            for batch in chunks(new_words, args.batch_size):
                for word in batch:
                    cache_path = dictionary_cache_path(word["term"])
                    result = load_json(cache_path)
                    if result is None:
                        try:
                            result = client.lookup(word["term"])
                        except (ValueError, DictionaryLookupError) as error:
                            word["review_flags"] = unique_strings(
                                [*word["review_flags"], "api_mismatch"]
                            )
                            print(f"API miss: {word['term']}: {error}", file=sys.stderr)
                            continue
                        atomic_write_json(cache_path, result)
                    merge_dictionary_result(word, result)
        group_pos = preferred_pos_for_groups(new_words, records)
        for word in new_words:
            key = word["term"].casefold()
            curate_api_baseline(
                word,
                preferred_synonyms=preferred_synonyms[key],
                preferred_chinese=preferred_chinese[key],
                preferred_pos=group_pos.get(key, []),
            )
    else:
        for word in new_words:
            word["review_flags"] = unique_strings([*word["review_flags"], "api_mismatch"])

    if not args.skip_llm:
        by_term = {word["term"].casefold(): word for word in new_words}
        pending: list[dict] = []
        for word in new_words:
            cached = load_json(llm_cache_path(word["term"]))
            if cached:
                by_term[word["term"].casefold()] = merge_llm_curated(
                    word, llm_word_to_record(cached)
                )
            else:
                pending.append(word)
        for batch in chunks(pending, args.batch_size):
            enriched = enrich_batch_with_openai(batch, model=args.model)
            returned: set[str] = set()
            expected = {word["term"].casefold() for word in batch}
            for item in enriched:
                key = item["term"].casefold()
                if key not in expected:
                    continue
                returned.add(key)
                atomic_write_json(llm_cache_path(item["term"]), item)
                by_term[key] = merge_llm_curated(by_term[key], llm_word_to_record(item))
            for missing in expected - returned:
                by_term[missing]["review_flags"] = unique_strings(
                    [*by_term[missing]["review_flags"], "api_mismatch"]
                )
            new_words = list(by_term.values())
            checkpoint = {**existing_by_term, **{word["term"].casefold(): word for word in new_words}}
            save_wordbank({"schema_version": 1, "words": list(checkpoint.values())}, args.words)
    final_by_term = {**existing_by_term, **{word["term"].casefold(): word for word in new_words}}
    save_wordbank({"schema_version": 1, "words": list(final_by_term.values())}, args.words)

    from validate import validate_files

    hard_errors, review = validate_files(words_path=args.words, write=True)
    print(f"merged {len(new_words)} new terms; review queue contains {len(review)} words")
    if hard_errors:
        for error in hard_errors:
            print(error, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
