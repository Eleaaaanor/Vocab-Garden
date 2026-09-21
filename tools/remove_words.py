#!/usr/bin/env python3
"""Safely remove word entries from the GRE wordbank.

Usage
-----
    python tools\\remove_words.py abate                  # dry run: show the full plan
    python tools\\remove_words.py abate abdicate --apply # actually remove them
    python tools\\remove_words.py abate --apply --purge-cache --purge-source-notes

A word never lives alone, so a removal touches several files:

1. ``data/words.json``        the entry itself is deleted
2. other entries              dangling ``synonyms`` / ``derivatives`` / ``confusables`` that
                              point at a removed term are pruned (``--keep-refs`` skips this)
3. ``data/curation/*.json``   curation entries for a removed term are deleted, otherwise
                              ``tools\\curate_gre.py`` exits 1 on "unknown" terms
                              (``--keep-curation`` skips this)
4. ``data/lists.json``        removed ids are dropped from custom lists (build never keeps
                              stale auto lists, but custom lists should stay clean)
5. ``data/review_queue.json`` regenerated through ``tools/validate.py`` (flags + queue)
6. ``verbalword.md`` / ``inbox.txt``
                              reported, because ``tools\\import_notes.py`` rebuilds the whole
                              wordbank from those files; ``--purge-source-notes`` removes the
                              matching rows too (a backup is written first)
7. ``data/enrichment_cache/`` optional, ``--purge-cache``
8. ``data/raw_records.jsonl`` optional, ``--purge-raw`` (import log only, never read back)

Safety rails
------------
* dry run by default: nothing is written without ``--apply``
* before writing, ``data/words.json`` is copied to ``data/backups/`` (``--no-backup``)
* refuses to delete more than half of the bank unless ``--force``
* exits 1 when a requested word cannot be found (``--ignore-missing`` to continue)
* ``--json`` prints a machine-readable summary for agents
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable

from wordbank import (
    DATA_DIR,
    RAW_RECORDS_PATH,
    REVIEW_PATH,
    ROOT,
    WORDS_PATH,
    atomic_write_json,
    clean_text,
    load_wordbank,
    normalize_term,
    save_wordbank,
    slugify,
    split_raw_term,
)

CURATION_DIR = ROOT / "data" / "curation"
LISTS_PATH = ROOT / "data" / "lists.json"
BACKUP_DIR = DATA_DIR / "backups"
SCHEMA_PATH = DATA_DIR / "words.schema.json"
CACHE_DIR = DATA_DIR / "enrichment_cache"
SOURCE_FILES = (ROOT / "verbalword.md", ROOT / "inbox.txt")

MAX_REMOVAL_FRACTION = 0.5

GENERATED_LIST_PATTERN = re.compile(r"^(?:all|part-\d+)$")


# --------------------------------------------------------------------------- #
# target resolution
# --------------------------------------------------------------------------- #
def resolve_targets(
    words: list[dict[str, Any]], queries: Iterable[str]
) -> tuple[list[dict[str, Any]], list[str]]:
    """Match queries against word ``id``, ``term`` and ``lemma`` (case-insensitive)."""
    index: dict[str, list[dict[str, Any]]] = {}
    for word in words:
        for key in (word.get("id"), word.get("term"), word.get("lemma")):
            normalized = normalize_term(str(key or ""))
            if normalized:
                index.setdefault(normalized, []).append(word)

    matched: list[dict[str, Any]] = []
    seen: set[str] = set()
    missing: list[str] = []
    for query in queries:
        candidates = index.get(normalize_term(clean_text(query))) or []
        if not candidates:
            missing.append(query)
            continue
        for word in candidates:
            word_id = str(word.get("id"))
            if word_id not in seen:
                seen.add(word_id)
                matched.append(word)
    return matched, missing


# --------------------------------------------------------------------------- #
# read-only scanning
# --------------------------------------------------------------------------- #
def scan_references(
    words: list[dict[str, Any]], removed_keys: set[str], removed_ids: set[str]
) -> list[dict[str, str]]:
    hits: list[dict[str, str]] = []
    for word in words:
        if str(word.get("id")) in removed_ids:
            continue
        for value in word.get("synonyms") or []:
            if normalize_term(str(value)) in removed_keys:
                hits.append({"word": str(word.get("term")), "field": "synonyms", "value": str(value)})
        for field in ("derivatives", "confusables"):
            for item in word.get(field) or []:
                if isinstance(item, dict) and normalize_term(str(item.get("term", ""))) in removed_keys:
                    hits.append(
                        {"word": str(word.get("term")), "field": field, "value": str(item.get("term"))}
                    )
    return hits


def scan_curation(directory: Path, removed_keys: set[str]) -> list[dict[str, Any]]:
    hits: list[dict[str, Any]] = []
    if not directory.exists():
        return hits
    for path in sorted(directory.glob("*.json")):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            continue
        entries = payload.get("words") if isinstance(payload, dict) else None
        if not isinstance(entries, dict):
            continue
        for term in entries:
            if normalize_term(term) in removed_keys:
                hits.append({"file": path.name, "term": term})
    return hits


def scan_lists(path: Path, removed_ids: set[str]) -> list[dict[str, Any]]:
    hits: list[dict[str, Any]] = []
    if not path.exists():
        return hits
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return hits
    for item in (payload.get("lists") if isinstance(payload, dict) else None) or []:
        if not isinstance(item, dict):
            continue
        ids = [word_id for word_id in item.get("word_ids") or [] if isinstance(word_id, str)]
        dropped = [word_id for word_id in ids if word_id in removed_ids]
        if dropped:
            hits.append({"list": str(item.get("id")), "dropped": dropped})
    return hits


def display_path(path: Path) -> str:
    try:
        return str(path.relative_to(ROOT))
    except ValueError:
        return str(path)


def scan_cache(terms: Iterable[str], cache_dir: Path) -> list[str]:
    hits: list[str] = []
    for name in ("dictionary", "llm"):
        for term in terms:
            path = cache_dir / name / f"{slugify(term)}.json"
            if path.exists():
                hits.append(str(path))
    return hits


def scan_raw_records(path: Path, removed_keys: set[str]) -> int:
    if not path.exists():
        return 0
    count = 0
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        raw_term = record.get("raw_term") or record.get("term") or ""
        terms = [term for term in split_raw_term(str(raw_term))[0]] or [str(raw_term)]
        if any(normalize_term(term) in removed_keys for term in terms):
            count += 1
    return count


ROW_TERM_RE = re.compile(r"^\|\s*([^|]+?)\s*\|")


def scan_source_notes(paths: Iterable[Path], removed_keys: set[str]) -> list[dict[str, Any]]:
    hits: list[dict[str, Any]] = []
    for path in paths:
        if not path.exists():
            continue
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
            cell = None
            match = ROW_TERM_RE.match(line)
            if match:
                cell = match.group(1)
            elif line.strip() and not line.lstrip().startswith("#") and not line.startswith("|"):
                cell = line.split("|")[0]
            if not cell:
                continue
            raw = clean_text(cell)
            if not raw or raw.startswith("---"):
                continue
            terms = split_raw_term(raw)[0] or [raw]
            if any(normalize_term(term) in removed_keys for term in terms):
                hits.append({"file": path.name, "path": str(path), "line": number, "text": line.strip()})
    return hits


# --------------------------------------------------------------------------- #
# planning / applying
# --------------------------------------------------------------------------- #
def build_plan(
    payload: dict[str, Any],
    targets: list[dict[str, Any]],
    *,
    lists_path: Path = LISTS_PATH,
    curation_dir: Path = CURATION_DIR,
    cache_dir: Path = CACHE_DIR,
    raw_records_path: Path = RAW_RECORDS_PATH,
    source_files: Iterable[Path] = SOURCE_FILES,
) -> dict[str, Any]:
    words = payload.get("words") or []
    removed_ids = {str(word.get("id")) for word in targets}
    removed_keys = {normalize_term(str(word.get("term"))) for word in targets}
    terms = [str(word.get("term")) for word in targets]

    return {
        "targets": [
            {"id": str(word.get("id")), "term": str(word.get("term")), "pos": list(word.get("pos") or [])}
            for word in targets
        ],
        "removed_ids": removed_ids,
        "removed_keys": removed_keys,
        "bank_size": len(words),
        "references": scan_references(words, removed_keys, removed_ids),
        "curation": scan_curation(curation_dir, removed_keys),
        "lists": scan_lists(lists_path, removed_ids),
        "cache": scan_cache(terms, cache_dir),
        "raw_records": scan_raw_records(raw_records_path, removed_keys),
        "source_notes": scan_source_notes(source_files, removed_keys),
        "paths": {
            "lists": lists_path,
            "curation_dir": curation_dir,
            "cache_dir": cache_dir,
            "raw_records": raw_records_path,
            "source_files": [str(path) for path in source_files],
        },
    }


def apply_plan(
    payload: dict[str, Any],
    plan: dict[str, Any],
    *,
    prune_refs: bool = True,
    prune_curation: bool = True,
    purge_cache: bool = False,
    purge_raw: bool = False,
) -> dict[str, int]:
    """Mutate ``payload`` in place and clean the side files. Returns counters."""
    words = payload.get("words") or []
    removed_ids: set[str] = plan["removed_ids"]
    removed_keys: set[str] = plan["removed_keys"]
    paths = plan["paths"]
    counters = {"words_removed": 0, "references_pruned": 0, "curation_entries": 0, "lists_updated": 0, "cache_files": 0, "raw_records": 0, "source_rows": 0}

    kept: list[dict[str, Any]] = []
    for word in words:
        if str(word.get("id")) in removed_ids:
            counters["words_removed"] += 1
            continue
        kept.append(word)
    payload["words"] = kept

    if prune_refs and plan["references"]:
        for word in payload["words"]:
            word_id = str(word.get("id"))
            if word_id in removed_ids:
                continue
            synonyms = word.get("synonyms")
            if isinstance(synonyms, list):
                words_kept = [value for value in synonyms if normalize_term(str(value)) not in removed_keys]
                if len(words_kept) != len(synonyms):
                    counters["references_pruned"] += len(synonyms) - len(words_kept)
                    word["synonyms"] = words_kept
            for field in ("derivatives", "confusables"):
                items = word.get(field)
                if not isinstance(items, list):
                    continue
                items_kept = [
                    item for item in items
                    if not (isinstance(item, dict) and normalize_term(str(item.get("term", ""))) in removed_keys)
                ]
                if len(items_kept) != len(items):
                    counters["references_pruned"] += len(items) - len(items_kept)
                    word[field] = items_kept

    curation_dir: Path = paths["curation_dir"]
    if prune_curation:
        for entry in plan["curation"]:
            path = curation_dir / entry["file"]
            if not path.exists():
                continue
            curation = json.loads(path.read_text(encoding="utf-8"))
            entries = curation.get("words")
            if isinstance(entries, dict):
                for term in [key for key in list(entries) if normalize_term(key) in removed_keys]:
                    entries.pop(term, None)
                    counters["curation_entries"] += 1
                atomic_write_json(path, curation)

    lists_path: Path = paths["lists"]
    if plan["lists"] and lists_path.exists():
        lists_payload = json.loads(lists_path.read_text(encoding="utf-8"))
        for item in lists_payload.get("lists") or []:
            if not isinstance(item, dict) or GENERATED_LIST_PATTERN.match(str(item.get("id"))):
                continue
            ids = [word_id for word_id in item.get("word_ids") or [] if isinstance(word_id, str)]
            kept_ids = [word_id for word_id in ids if word_id not in removed_ids]
            if len(kept_ids) != len(ids):
                item["word_ids"] = kept_ids
                counters["lists_updated"] += 1
        atomic_write_json(lists_path, lists_payload)

    cache_dir: Path = paths["cache_dir"]
    if purge_cache:
        for target in plan["cache"]:
            path = Path(target)
            if path.exists():
                path.unlink()
                counters["cache_files"] += 1

    raw_records: Path = paths["raw_records"]
    if purge_raw and plan["raw_records"] and raw_records.exists():
        kept_lines = []
        for line in raw_records.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                kept_lines.append(line)
                continue
            raw_term = record.get("raw_term") or record.get("term") or ""
            terms = split_raw_term(str(raw_term))[0] or [str(raw_term)]
            if any(normalize_term(term) in removed_keys for term in terms):
                counters["raw_records"] += 1
                continue
            kept_lines.append(line)
        raw_records.write_text("\n".join(kept_lines) + "\n", encoding="utf-8", newline="\n")

    return counters


def purge_source_rows(plan: dict[str, Any], backup_dir: Path = BACKUP_DIR) -> int:
    """Delete the matching rows from verbalword.md / inbox.txt (backup first)."""
    hits = plan["source_notes"]
    if not hits:
        return 0
    by_file: dict[str, set[int]] = {}
    for hit in hits:
        by_file.setdefault(hit.get("path") or str(ROOT / hit["file"]), set()).add(hit["line"])
    removed = 0
    for target, lines in by_file.items():
        path = Path(target)
        if not path.exists():
            continue
        backup_file(path, backup_dir)
        content = path.read_text(encoding="utf-8").splitlines()
        kept = [line for number, line in enumerate(content, start=1) if number not in lines]
        removed += len(content) - len(kept)
        path.write_text("\n".join(kept) + "\n", encoding="utf-8", newline="\n")
    return removed


# --------------------------------------------------------------------------- #
# runner
# --------------------------------------------------------------------------- #
def backup_file(path: Path, backup_dir: Path = BACKUP_DIR) -> Path | None:
    if not path.exists():
        return None
    backup_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    target = backup_dir / f"{path.stem}-{stamp}{path.suffix}"
    shutil.copy2(path, target)
    return target


def refresh_review_queue(words_path: Path, schema_path: Path, review_path: Path) -> dict[str, Any]:
    try:
        from validate import validate_files
    except ImportError as error:  # pragma: no cover - dependency missing
        return {"refreshed": False, "reason": str(error)}
    hard_errors, review = validate_files(words_path, schema_path, review_path, write=True)
    return {"refreshed": True, "flagged_words": len(review), "hard_errors": hard_errors}


def run(
    queries: Iterable[str],
    *,
    words_path: Path = WORDS_PATH,
    lists_path: Path = LISTS_PATH,
    curation_dir: Path = CURATION_DIR,
    review_path: Path = REVIEW_PATH,
    schema_path: Path = SCHEMA_PATH,
    cache_dir: Path = CACHE_DIR,
    raw_records_path: Path = RAW_RECORDS_PATH,
    source_files: Iterable[Path] = SOURCE_FILES,
    backup_dir: Path = BACKUP_DIR,
    apply: bool = False,
    prune_refs: bool = True,
    prune_curation: bool = True,
    purge_cache: bool = False,
    purge_raw: bool = False,
    purge_source_notes: bool = False,
    backup: bool = True,
    force: bool = False,
    ignore_missing: bool = False,
) -> tuple[int, dict[str, Any]]:
    payload = load_wordbank(words_path)
    words = payload.get("words") or []
    targets, missing = resolve_targets(words, queries)

    if missing and not ignore_missing:
        return 1, {"error": "unknown words", "missing": missing, "removed": []}
    if not targets:
        return 1, {"error": "nothing to remove", "missing": missing, "removed": []}
    if len(targets) > len(words) * MAX_REMOVAL_FRACTION and not force:
        return 1, {
            "error": (
                f"refusing to delete {len(targets)}/{len(words)} words "
                f"(> {int(MAX_REMOVAL_FRACTION * 100)}%); use --force to override"
            ),
            "missing": missing,
            "removed": [],
        }

    plan = build_plan(
        payload,
        targets,
        lists_path=lists_path,
        curation_dir=curation_dir,
        cache_dir=cache_dir,
        raw_records_path=raw_records_path,
        source_files=source_files,
    )

    summary: dict[str, Any] = {
        "applied": False,
        "missing": missing,
        "plan": {
            "targets": plan["targets"],
            "references": plan["references"],
            "curation": plan["curation"],
            "lists": plan["lists"],
            "cache": plan["cache"],
            "raw_records": plan["raw_records"],
            "source_notes": plan["source_notes"],
        },
        "counters": {},
    }
    if not apply:
        return 0, summary

    backup_path = backup_file(words_path, backup_dir) if backup else None
    summary["backup"] = str(backup_path) if backup_path else None

    counters = apply_plan(
        payload,
        plan,
        prune_refs=prune_refs,
        prune_curation=prune_curation,
        purge_cache=purge_cache,
        purge_raw=purge_raw,
    )
    save_wordbank(payload, words_path)

    if purge_source_notes:
        counters["source_rows"] = purge_source_rows(plan, backup_dir)

    queue = refresh_review_queue(words_path, schema_path, review_path) if review_path else {"refreshed": False}

    summary["applied"] = True
    summary["counters"] = counters
    summary["review_queue"] = queue
    summary["bank_size"] = len(payload.get("words") or [])
    return 0, summary


# --------------------------------------------------------------------------- #
# reporting / cli
# --------------------------------------------------------------------------- #
def describe(summary: dict[str, Any], *, applied: bool) -> list[str]:
    plan = summary["plan"]
    lines: list[str] = []
    lines.append(f"targets ({len(plan['targets'])}):")
    for item in plan["targets"]:
        pos = f" [{', '.join(item['pos'])}]" if item["pos"] else ""
        lines.append(f"  - {item['term']}{pos} (id={item['id']})")
    if summary["missing"]:
        lines.append(f"not found ({len(summary['missing'])}): {', '.join(summary['missing'])}")

    def section(title: str, items: list[str], note: str = "") -> None:
        suffix = f" — {note}" if note and items else ""
        lines.append(f"{title} ({len(items)}){suffix}:")
        lines.extend(f"  - {item}" for item in items[:40])
        if len(items) > 40:
            lines.append(f"  ... {len(items) - 40} more")

    section(
        "dangling references to prune",
        [f"{ref['word']}.{ref['field']} -> {ref['value']}" for ref in plan["references"]],
        "skip with --keep-refs",
    )
    section(
        "curation entries to delete",
        [f"{entry['file']}: {entry['term']}" for entry in plan["curation"]],
        "needed so curate_gre.py keeps passing; skip with --keep-curation",
    )
    section(
        "custom lists to update",
        [f"{item['list']}: drop {', '.join(item['dropped'])}" for item in plan["lists"]],
    )
    section("cache files", [display_path(Path(item)) for item in plan["cache"]], "delete with --purge-cache")

    if plan["raw_records"]:
        lines.append(f"raw_records.jsonl matches ({plan['raw_records']}) — purge with --purge-raw")
    if plan["source_notes"]:
        lines.append(
            f"source notes still mentioning these words ({len(plan['source_notes'])}) — "
            "import_notes.py would re-add them; purge with --purge-source-notes:"
        )
        lines.extend(f"  - {hit['file']}:{hit['line']} {hit['text']}" for hit in plan["source_notes"][:20])
        if len(plan["source_notes"]) > 20:
            lines.append(f"  ... {len(plan['source_notes']) - 20} more rows")

    if applied:
        counters = summary["counters"]
        lines.append(
            "applied: "
            f"{counters.get('words_removed', 0)} words removed, "
            f"{counters.get('references_pruned', 0)} references pruned, "
            f"{counters.get('curation_entries', 0)} curation entries deleted, "
            f"{counters.get('lists_updated', 0)} custom lists updated, "
            f"{counters.get('cache_files', 0)} cache files deleted, "
            f"{counters.get('raw_records', 0)} raw records dropped, "
            f"{counters.get('source_rows', 0)} source rows dropped"
        )
        if summary.get("backup"):
            lines.append(f"backup: {summary['backup']}")
        queue = summary.get("review_queue") or {}
        if queue.get("refreshed"):
            lines.append(f"review queue refreshed: {queue.get('flagged_words')} flagged words")
        else:
            lines.append("review queue not refreshed (run tools\\validate.py --write)")
        lines.append("next: python tools\\build.py")
    else:
        lines.append("dry run: nothing was written. Re-run with --apply to remove them.")

    return lines


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Safely remove words from data/words.json and the files that reference them."
    )
    parser.add_argument("queries", nargs="+", metavar="WORD", help="word id / term / lemma")
    parser.add_argument("--words", type=Path, default=WORDS_PATH)
    parser.add_argument("--lists", type=Path, default=LISTS_PATH)
    parser.add_argument("--curation-dir", type=Path, default=CURATION_DIR)
    parser.add_argument("--review-queue", type=Path, default=REVIEW_PATH)
    parser.add_argument("--schema", type=Path, default=SCHEMA_PATH)
    parser.add_argument("--cache-dir", type=Path, default=CACHE_DIR)
    parser.add_argument("--raw-records", type=Path, default=RAW_RECORDS_PATH)
    parser.add_argument("--apply", action="store_true", help="write the changes (default: dry run)")
    parser.add_argument("--keep-refs", action="store_true", help="keep synonyms/derivatives/confusables pointing at removed words")
    parser.add_argument("--keep-curation", action="store_true", help="keep matching entries in data/curation/*.json")
    parser.add_argument("--purge-cache", action="store_true", help="delete enrichment cache files")
    parser.add_argument("--purge-raw", action="store_true", help="drop matching lines from raw_records.jsonl")
    parser.add_argument("--purge-source-notes", action="store_true", help="delete matching rows from verbalword.md / inbox.txt")
    parser.add_argument("--no-backup", action="store_true", help="skip the data/backups/ copy")
    parser.add_argument("--backup-dir", type=Path, default=BACKUP_DIR, help="where to keep the pre-change copy")
    parser.add_argument("--ignore-missing", action="store_true", help="continue when a word cannot be found")
    parser.add_argument("--force", action="store_true", help="allow deleting more than half of the wordbank")
    parser.add_argument("--json", action="store_true", help="print a machine-readable summary")
    args = parser.parse_args(argv)

    code, summary = run(
        args.queries,
        words_path=args.words,
        lists_path=args.lists,
        curation_dir=args.curation_dir,
        review_path=args.review_queue,
        schema_path=args.schema,
        cache_dir=args.cache_dir,
        raw_records_path=args.raw_records,
        apply=args.apply,
        prune_refs=not args.keep_refs,
        prune_curation=not args.keep_curation,
        purge_cache=args.purge_cache,
        purge_raw=args.purge_raw,
        purge_source_notes=args.purge_source_notes,
        backup=not args.no_backup,
        backup_dir=args.backup_dir,
        force=args.force,
        ignore_missing=args.ignore_missing,
    )

    if args.json:
        print(json.dumps(summary, ensure_ascii=False, indent=2))
        return code

    if "error" in summary:
        print(f"[error] {summary['error']}", file=sys.stderr)
        for item in summary.get("missing", []):
            print(f"  - not found: {item}", file=sys.stderr)
        return code

    for line in describe(summary, applied=bool(summary.get("applied"))):
        print(line)
    return code


if __name__ == "__main__":
    raise SystemExit(main())
