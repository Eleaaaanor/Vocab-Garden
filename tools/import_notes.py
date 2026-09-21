from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote

from wordbank import (
    CACHE_DIR,
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
    merge_dictionary_result,
    merge_llm_curated,
    parse_markdown_notes,
    preferred_pos_for_groups,
    reconcile_group_flags,
    save_wordbank,
    unique_strings,
    write_raw_records,
)

sys.path.insert(0, str(ROOT / "diction_lookup"))
from dictionary_lookup import DictionaryClient, DictionaryLookupError, FREE_DICTIONARY_URL  # noqa: E402


def dictionary_only_lookup(client: DictionaryClient, term: str) -> dict:
    payload = client._primary_lookup(term)
    english, chinese, synonyms, examples = client._extract_primary(payload, term)
    if not english:
        raise DictionaryLookupError(f"no English definitions returned for: {term}")
    source = payload.get("source") or {}
    source_url = source.get("url") or FREE_DICTIONARY_URL.format(word=quote(term, safe="-'"))
    license_name = (source.get("license") or {}).get("name") or "CC BY-SA 4.0"
    return {
        "word": term,
        "chinese_meanings": chinese,
        "english": english,
        "synonyms": synonyms,
        "examples": [
            {
                "english": sentence,
                "chinese": None,
                "source": "Wiktionary via FreeDictionaryAPI",
                "source_url": source_url,
                "author": None,
                "license": license_name,
            }
            for sentence in examples[:3]
        ],
        "sources": [
            {"name": "FreeDictionaryAPI", "url": source_url, "license": license_name}
        ],
        "warnings": [],
        "retrieved_at": datetime.now(timezone.utc).isoformat(),
    }


def load_cached(path: Path) -> dict | None:
    if not path.exists():
        return None
    with path.open(encoding="utf-8") as handle:
        return json.load(handle)


def enrich_api(
    words: list[dict], records: list[dict], *, mode: str, batch_size: int
) -> None:
    preferred_synonyms = {
        word["term"].casefold(): list(word.get("synonyms", [])) for word in words
    }
    preferred_chinese = {
        word["term"].casefold(): list(word.get("chinese", [])) for word in words
    }
    if mode == "none":
        for word in words:
            word["review_flags"] = unique_strings(
                [*word["review_flags"], "api_mismatch"]
            )
        return
    with DictionaryClient() as client:
        for batch_number, batch in enumerate(chunks(words, batch_size), 1):
            for word in batch:
                cache_path = dictionary_cache_path(word["term"])
                result = load_cached(cache_path)
                if result is None:
                    try:
                        result = (
                            client.lookup(word["term"])
                            if mode == "full"
                            else dictionary_only_lookup(client, word["term"])
                        )
                    except (ValueError, DictionaryLookupError) as error:
                        word["review_flags"] = unique_strings(
                            [*word["review_flags"], "api_mismatch"]
                        )
                        print(f"API miss: {word['term']}: {error}", file=sys.stderr)
                        continue
                    atomic_write_json(cache_path, result)
                merge_dictionary_result(word, result)
            save_wordbank({"schema_version": 1, "words": words})
            print(f"API batch {batch_number}: checkpointed {min(batch_number * batch_size, len(words))}/{len(words)}")
    group_pos = preferred_pos_for_groups(words, records)
    for word in words:
        key = word["term"].casefold()
        curate_api_baseline(
            word,
            preferred_synonyms=preferred_synonyms[key],
            preferred_chinese=preferred_chinese[key],
            preferred_pos=group_pos.get(key, []),
        )
    save_wordbank({"schema_version": 1, "words": words})


def enrich_missing_examples(words: list[dict], *, batch_size: int) -> None:
    pending = [word for word in words if not word.get("examples")]
    with DictionaryClient() as client:
        for batch_number, batch in enumerate(chunks(pending, batch_size), 1):
            for word in batch:
                cache_path = CACHE_DIR / "examples" / f'{word["id"]}.json'
                cached = load_cached(cache_path)
                if cached is None:
                    try:
                        examples = client._tatoeba_examples(word["term"])
                    except DictionaryLookupError as error:
                        print(f"Tatoeba miss: {word['term']}: {error}", file=sys.stderr)
                        continue
                    cached = {"examples": examples}
                    atomic_write_json(cache_path, cached)
                examples = cached.get("examples") or []
                if not examples:
                    continue
                word["examples"] = [
                    {
                        "sentence": item["english"],
                        "translation": item.get("chinese"),
                        "source_type": "tatoeba",
                        "author": item.get("author"),
                        "url": item.get("source_url"),
                        "license": item.get("license"),
                    }
                    for item in examples[:1]
                ]
                word["sources"]["examples"] = [
                    {
                        "name": "Tatoeba",
                        "url": "https://tatoeba.org/",
                        "license": "per-sentence license in each example",
                    }
                ]
            save_wordbank({"schema_version": 1, "words": words})
            print(
                f"example batch {batch_number}: checkpointed "
                f"{min(batch_number * batch_size, len(pending))}/{len(pending)}"
            )


def enrich_llm(words: list[dict], *, model: str, batch_size: int) -> None:
    pending: list[dict] = []
    by_term = {word["term"].casefold(): word for word in words}
    for word in words:
        cached = load_cached(llm_cache_path(word["term"]))
        if cached:
            by_term[word["term"].casefold()] = merge_llm_curated(
                word, llm_word_to_record(cached)
            )
        else:
            pending.append(word)

    for batch_number, batch in enumerate(chunks(pending, batch_size), 1):
        enriched = enrich_batch_with_openai(batch, model=model)
        expected = {word["term"].casefold() for word in batch}
        returned = set()
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
        words[:] = sorted(by_term.values(), key=lambda item: item["term"].casefold())
        save_wordbank({"schema_version": 1, "words": words})
        print(f"LLM batch {batch_number}: checkpointed {min(batch_number * batch_size, len(pending))}/{len(pending)}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Rebuild the structured wordbank from verbalword.md.")
    parser.add_argument("--source", type=Path, default=ROOT / "verbalword.md")
    parser.add_argument("--api-mode", choices=("none", "dictionary", "full"), default="dictionary")
    parser.add_argument("--llm", action="store_true", help="run OpenAI enrichment after dictionary lookup")
    parser.add_argument("--fill-missing-examples", action="store_true")
    parser.add_argument("--model", default=None, help="OpenAI model; required with --llm")
    parser.add_argument("--batch-size", type=int, default=40)
    args = parser.parse_args(argv)
    if not 1 <= args.batch_size <= 50:
        parser.error("--batch-size must be between 1 and 50")
    if args.llm and not args.model:
        parser.error("--model is required with --llm")

    records = parse_markdown_notes(args.source)
    words = build_words_from_records(records)
    write_raw_records(records)
    save_wordbank({"schema_version": 1, "words": words})
    print(f"parsed {len(records)} source rows into {len(words)} unique terms")
    enrich_api(words, records, mode=args.api_mode, batch_size=args.batch_size)
    reconcile_group_flags(words, records)
    if args.fill_missing_examples:
        enrich_missing_examples(words, batch_size=args.batch_size)
    if args.llm:
        enrich_llm(words, model=args.model, batch_size=args.batch_size)
    save_wordbank({"schema_version": 1, "words": words})

    from validate import validate_files

    hard_errors, review = validate_files(write=True)
    print(f"review queue: {len(review)} words")
    if hard_errors:
        for error in hard_errors:
            print(error, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
