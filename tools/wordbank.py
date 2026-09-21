from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
import time
from collections import defaultdict
from pathlib import Path
from typing import Any, Iterable, Literal

from pydantic import BaseModel, ConfigDict, Field


ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = ROOT / "data"
WORDS_PATH = DATA_DIR / "words.json"
REVIEW_PATH = DATA_DIR / "review_queue.json"
RAW_RECORDS_PATH = DATA_DIR / "raw_records.jsonl"
CACHE_DIR = DATA_DIR / "enrichment_cache"

ALLOWED_POS = {
    "noun",
    "verb",
    "adjective",
    "adverb",
    "preposition",
    "conjunction",
    "pronoun",
    "determiner",
    "interjection",
    "phrase",
    "other",
}

OBJECTIVE_FLAGS = {
    "ambiguous_term",
    "multiple_possible_pos",
    "api_mismatch",
    "missing_definition",
    "possible_wrong_synonym",
    "possible_wrong_derivative",
    "possible_confusable",
    "missing_example",
    "source_incomplete",
    "missing_chinese",
    "invalid_pos",
    "self_reference",
    "schema_error",
}

POS_ALIASES = {
    "n": "noun",
    "noun": "noun",
    "proper noun": "noun",
    "v": "verb",
    "verb": "verb",
    "a": "adjective",
    "adj": "adjective",
    "adjective": "adjective",
    "adverb": "adverb",
    "adv": "adverb",
    "preposition": "preposition",
    "conjunction": "conjunction",
    "pronoun": "pronoun",
    "determiner": "determiner",
    "interjection": "interjection",
    "phrase": "phrase",
}

TABLE_ROW_RE = re.compile(r"^\|\s*(.*?)\s*\|\s*(.*?)\s*\|\s*$")
TERM_RE = re.compile(r"^[A-Za-z][A-Za-z' -]*$")
HAS_CJK_RE = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]")


def clean_text(value: Any) -> str:
    return " ".join(str(value or "").strip().split())


def unique_strings(values: Iterable[Any], *, casefold: bool = True) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        item = clean_text(value)
        key = item.casefold() if casefold else item
        if item and key not in seen:
            seen.add(key)
            result.append(item)
    return result


def slugify(term: str) -> str:
    value = term.casefold().replace("'", "")
    value = re.sub(r"[^a-z0-9]+", "-", value).strip("-")
    if not value:
        digest = hashlib.sha256(term.encode("utf-8")).hexdigest()[:12]
        return f"term-{digest}"
    return value


def normalize_term(term: str) -> str:
    term = clean_text(term).strip(".,;:")
    term = term.replace("’", "'").replace("–", "-").replace("—", "-")
    return term.casefold()


def normalize_pos(value: str) -> str:
    normalized = clean_text(value).casefold().rstrip(".")
    return POS_ALIASES.get(normalized, "other")


def infer_pos_from_note(note: str, term: str) -> list[str]:
    match = re.match(r"^\s*(n|v|a|adj|adv)\.(?:\s|$|-)", note, re.IGNORECASE)
    if match:
        marker = match.group(1).casefold()
        return [{"n": "noun", "v": "verb", "a": "adjective", "adj": "adjective", "adv": "adverb"}[marker]]
    if " " in term:
        return ["phrase"]
    return []


def extract_chinese(note: str) -> list[str]:
    if not HAS_CJK_RE.search(note):
        return []
    value = clean_text(note)
    value = re.sub(r"^\s*(?:n|v|a|adj|adv)\.(?:\s*[-–]\s*)?", "", value, flags=re.I)
    value = re.sub(r"\([^()]*\)", lambda m: m.group(0) if HAS_CJK_RE.search(m.group(0)) else "", value)
    value = re.sub(r"[A-Za-z][A-Za-z .'-]*", "", value)
    value = value.replace("(...)...", "...")
    value = re.sub(r"\(\s*\)", "", value)
    value = re.sub(r"\s*[,，]\s*[,，]+", "，", value)
    value = re.sub(r"\s*[:：]\s*", "：", value)
    value = clean_text(value).strip(" ,，;；:/")
    if value.startswith("(") and value.endswith(")"):
        value = value[1:-1].strip()
    return [value] if value and HAS_CJK_RE.search(value) else []


def _looks_like_term(value: str) -> bool:
    return bool(TERM_RE.fullmatch(clean_text(value)))


def _same_word_family(left: str, right: str) -> bool:
    left_key = re.sub(r"[^a-z]", "", left.casefold())
    right_key = re.sub(r"[^a-z]", "", right.casefold())
    shorter, longer = sorted((left_key, right_key), key=len)
    if len(shorter) < 4:
        return False
    return longer.startswith(shorter) or (
        shorter.endswith("e") and longer.startswith(shorter[:-1])
    )


def split_raw_term(raw_term: str) -> tuple[list[str], str, list[str], dict[str, str]]:
    """Return normalized terms, relation, review flags, and explicit lemmas."""
    value = clean_text(raw_term)
    flags: list[str] = []
    lemmas: dict[str, str] = {}

    if re.fullmatch(r".+?\s*\(to\)", value, re.I):
        phrase = normalize_term(re.sub(r"\s*\(to\)\s*$", " to", value, flags=re.I))
        return [phrase], "single", flags, lemmas

    plural_match = re.fullmatch(r"(.+?)\s*\(pl\.\s*([^()]+)\)", value, re.I)
    if plural_match:
        singular = normalize_term(plural_match.group(1))
        plural = normalize_term(plural_match.group(2))
        return [singular], "inflection_note", flags, {plural: singular}

    lemma_match = re.fullmatch(r"([A-Za-z][A-Za-z' -]*)\s*\(([A-Za-z][A-Za-z' -]*)\)", value)
    if lemma_match:
        surface = normalize_term(lemma_match.group(1))
        lemma = normalize_term(lemma_match.group(2))
        if surface != lemma:
            lemmas[surface] = lemma
            return [lemma], "inflection_note", flags, lemmas

    gloss_match = re.fullmatch(r"(.+?)\s*\(=\s*[^()]+\)", value)
    if gloss_match:
        value = gloss_match.group(1)
    if " / " in value:
        candidates = [normalize_term(item) for item in value.split(" / ")]
        relation = "related_forms" if all(
            _same_word_family(candidates[0], item) for item in candidates[1:]
        ) else "slash_group"
        if relation == "slash_group":
            flags.append("ambiguous_term")
    elif "," in value:
        candidates = [normalize_term(item) for item in value.split(",")]
        relation = "synonym_group"
        flags.append("possible_wrong_synonym")
    else:
        candidates = [normalize_term(value)]
        relation = "single"

    terms = unique_strings(item for item in candidates if _looks_like_term(item))
    if len(terms) != len(candidates) or not terms:
        flags.append("ambiguous_term")
    return terms, relation, unique_strings(flags), lemmas


def parse_markdown_notes(path: Path) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        match = TABLE_ROW_RE.match(line)
        if not match:
            continue
        raw_term, raw_note = (clean_text(match.group(1)), clean_text(match.group(2)))
        if (
            raw_term in {"英文单词 / 短语", "---"}
            or raw_term.casefold() in {"english", "word", "term"}
            or raw_term.startswith("英文")
        ):
            continue
        terms, relation, flags, lemmas = split_raw_term(raw_term)
        records.append(
            {
                "source_file": path.name,
                "source_line": line_number,
                "raw_term": raw_term,
                "raw_note": raw_note,
                "normalized_terms": terms,
                "relation": relation,
                "lemmas": lemmas,
                "review_flags": flags,
            }
        )
    return records


def parse_inbox(path: Path) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    if not path.exists():
        return records
    for line_number, line in enumerate(path.read_text(encoding="utf-8-sig").splitlines(), 1):
        stripped = clean_text(line)
        if not stripped or stripped.startswith("#"):
            continue
        if "|" in stripped:
            raw_term, raw_note = (clean_text(part) for part in stripped.split("|", 1))
        elif "\t" in line:
            raw_term, raw_note = (clean_text(part) for part in line.split("\t", 1))
        else:
            raw_term, raw_note = stripped, ""
        terms, relation, flags, lemmas = split_raw_term(raw_term)
        records.append(
            {
                "source_file": path.name,
                "source_line": line_number,
                "raw_term": raw_term,
                "raw_note": raw_note,
                "normalized_terms": terms,
                "relation": relation,
                "lemmas": lemmas,
                "review_flags": flags,
            }
        )
    return records


def empty_sources() -> dict[str, list[dict[str, Any]]]:
    return {"dictionary": [], "synonyms": [], "translation": [], "examples": []}


def empty_word(term: str) -> dict[str, Any]:
    return {
        "id": slugify(term),
        "term": term,
        "lemma": term,
        "pos": ["phrase"] if " " in term else [],
        "chinese": [],
        "english_definitions": [],
        "synonyms": [],
        "examples": [],
        "derivatives": [],
        "confusables": [],
        "personal_notes": [],
        "sources": empty_sources(),
        "review_flags": [],
    }


def build_words_from_records(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    by_term: dict[str, dict[str, Any]] = {}
    for record in records:
        terms = record["normalized_terms"]
        for term in terms:
            word = by_term.setdefault(term.casefold(), empty_word(term))
            word["personal_notes"] = unique_strings(
                [*word["personal_notes"], f'{record["raw_term"]}: {record["raw_note"]}'.rstrip(": ")]
            )
            word["chinese"] = unique_strings(
                [*word["chinese"], *extract_chinese(record["raw_note"])]
            )
            word["pos"] = unique_strings(
                [*word["pos"], *infer_pos_from_note(record["raw_note"], term)]
            )
            word["review_flags"] = unique_strings(
                [*word["review_flags"], *record["review_flags"]]
            )

            for surface, lemma in record.get("lemmas", {}).items():
                if term == lemma:
                    word["derivatives"].append(
                        {"term": surface, "relation": "inflected form recorded in source notes"}
                    )

        if len(terms) > 1:
            for term in terms:
                word = by_term[term.casefold()]
                peers = [peer for peer in terms if peer.casefold() != term.casefold()]
                if record["relation"] == "related_forms":
                    for peer in peers:
                        word["derivatives"].append(
                            {"term": peer, "relation": "related form from the same source note"}
                        )
                elif {item.casefold() for item in terms} == {"dwindle", "swindle"}:
                    for peer in peers:
                        word["confusables"].append(
                            {"term": peer, "distinction": "similar spelling; meanings differ"}
                        )
                else:
                    word["synonyms"] = unique_strings([*word["synonyms"], *peers])

    for word in by_term.values():
        word["derivatives"] = unique_objects(word["derivatives"], ("term", "relation"))
        word["confusables"] = unique_objects(word["confusables"], ("term", "distinction"))
    return sorted(by_term.values(), key=lambda item: item["term"].casefold())


def unique_objects(values: Iterable[dict[str, Any]], keys: tuple[str, ...]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    seen: set[tuple[str, ...]] = set()
    for value in values:
        marker = tuple(clean_text(value.get(key)).casefold() for key in keys)
        if marker not in seen:
            seen.add(marker)
            result.append(value)
    return result


def source_bucket(source: dict[str, Any]) -> str:
    name = clean_text(source.get("name")).casefold()
    if "tatoeba" in name:
        return "examples"
    if "datamuse" in name:
        return "synonyms"
    if "chinese" in name or "中文" in name:
        return "translation"
    return "dictionary"


def merge_dictionary_result(word: dict[str, Any], result: dict[str, Any]) -> None:
    definitions: list[dict[str, str]] = []
    pos: list[str] = []
    for group in result.get("english") or []:
        normalized_pos = normalize_pos(group.get("part_of_speech", ""))
        pos.append(normalized_pos)
        for definition in group.get("definitions") or []:
            definitions.append({"pos": normalized_pos, "definition": clean_text(definition)})
    word["pos"] = unique_strings([*word.get("pos", []), *pos])
    word["english_definitions"] = unique_objects(
        [*word.get("english_definitions", []), *definitions], ("pos", "definition")
    )
    word["chinese"] = unique_strings(
        [*word.get("chinese", []), *(result.get("chinese_meanings") or [])]
    )
    word["synonyms"] = unique_strings(
        [*word.get("synonyms", []), *(result.get("synonyms") or [])]
    )

    mapped_examples = []
    for example in result.get("examples") or []:
        mapped_examples.append(
            {
                "sentence": clean_text(example.get("english")),
                "translation": clean_text(example.get("chinese")) or None,
                "source_type": clean_text(example.get("source")).casefold().replace(" ", "_") or "dictionary",
                "author": example.get("author"),
                "url": example.get("source_url"),
                "license": example.get("license"),
            }
        )
    word["examples"] = unique_objects(
        [*word.get("examples", []), *mapped_examples], ("sentence",)
    )
    sources = word.setdefault("sources", empty_sources())
    for source in result.get("sources") or []:
        enriched_source = dict(source)
        if result.get("retrieved_at"):
            enriched_source.setdefault("retrieved_at", result["retrieved_at"])
        bucket = source_bucket(enriched_source)
        sources[bucket] = unique_objects([*sources.get(bucket, []), enriched_source], ("name", "url"))
    if result.get("warnings"):
        note = "API warnings: " + "; ".join(result["warnings"])
        word["personal_notes"] = unique_strings([*word.get("personal_notes", []), note])


def merge_word(existing: dict[str, Any], incoming: dict[str, Any]) -> dict[str, Any]:
    result = json.loads(json.dumps(existing, ensure_ascii=False))
    for field in ("pos", "chinese", "synonyms", "personal_notes", "review_flags"):
        result[field] = unique_strings([*result.get(field, []), *incoming.get(field, [])])
    for field, keys in (
        ("english_definitions", ("pos", "definition")),
        ("examples", ("sentence",)),
        ("derivatives", ("term", "relation")),
        ("confusables", ("term", "distinction")),
    ):
        result[field] = unique_objects([*result.get(field, []), *incoming.get(field, [])], keys)
    if result.get("lemma") == result.get("term") and incoming.get("lemma"):
        result["lemma"] = incoming["lemma"]
    result.setdefault("sources", empty_sources())
    for bucket in empty_sources():
        result["sources"][bucket] = unique_objects(
            [*result["sources"].get(bucket, []), *(incoming.get("sources", {}).get(bucket, []))],
            ("name", "url"),
        )
    return result


def merge_llm_curated(existing: dict[str, Any], incoming: dict[str, Any]) -> dict[str, Any]:
    """Apply learner-facing LLM fields while retaining provenance and personal notes."""
    result = merge_word(existing, incoming)
    for field in ("pos", "chinese", "english_definitions", "synonyms", "derivatives", "confusables"):
        if incoming.get(field):
            result[field] = incoming[field]
    if incoming.get("lemma"):
        result["lemma"] = incoming["lemma"]
    if incoming.get("examples") and not existing.get("examples"):
        result["examples"] = incoming["examples"]
    return result


EDITORIAL_DEFINITIONS: dict[str, tuple[str, str]] = {
    "at odds with": ("phrase", "in disagreement or conflict with"),
    "attendant upon": ("phrase", "accompanying or resulting from something"),
    "auto company": ("phrase", "a company that manufactures or sells automobiles"),
    "balanced reflection": ("phrase", "a fair and even-handed representation or consideration"),
    "founder on": ("phrase", "to fail because of a particular obstacle or problem"),
    "indebted to": ("phrase", "owing gratitude or money to, or strongly influenced by"),
    "plagued by": ("phrase", "repeatedly troubled or afflicted by"),
    "terrible bore": ("phrase", "an extremely dull or tiresome person"),
    "belie": ("verb", "to give a false impression of, or to show something to be false"),
}

LOW_VALUE_MARKERS = (
    "obsolete", "archaic", "historical", "rare", "alternative form",
    "misspelling", "nonstandard", "dialectal",
)


def _explicit_note_pos(word: dict[str, Any]) -> list[str]:
    result: list[str] = []
    for note in word.get("personal_notes", []):
        content = note.split(":", 1)[-1].strip()
        marker = re.match(r"^(n|v|a|adj|adv)\.(?:\s|$|-)", content, re.I)
        if marker:
            result.append(normalize_pos(marker.group(1)))
        if re.match(r"^n\.-v\.", content, re.I):
            result.extend(("noun", "verb"))
    return unique_strings(result)


def curate_api_baseline(
    word: dict[str, Any], *, preferred_synonyms: Iterable[str] = (),
    preferred_chinese: Iterable[str] = (), preferred_pos: Iterable[str] = (),
) -> None:
    """Create a compact deterministic baseline before optional LLM editorial work."""
    term = word["term"].casefold()
    if term in EDITORIAL_DEFINITIONS:
        pos, definition = EDITORIAL_DEFINITIONS[term]
        word["pos"] = [pos]
        word["english_definitions"] = [{"pos": pos, "definition": definition}]
        if term == "belie":
            word["review_flags"] = unique_strings([*word["review_flags"], "api_mismatch"])

    definitions = word.get("english_definitions", [])
    available_pos = unique_strings(item.get("pos") for item in definitions)
    target_pos = unique_strings(preferred_pos) or _explicit_note_pos(word)
    source_notes = [note.split(":", 1)[-1] for note in word.get("personal_notes", [])]
    if not target_pos and " " in term:
        target_pos = ["phrase"] if "phrase" in available_pos or not available_pos else available_pos
    if not target_pos and term.endswith("ly") and "adverb" in available_pos:
        target_pos = ["adverb"]
    if not target_pos and word.get("chinese") and "adjective" in available_pos:
        endings = [clean_text(value).rstrip("。.!！") for value in word["chinese"]]
        if endings and all(value.endswith("的") for value in endings):
            target_pos = ["adjective"]
    noun_suffixes = (
        "tion", "sion", "ment", "ness", "ity", "ism", "ist", "ance", "ence",
        "cy", "ship", "hood", "dom", "ology", "ism", "tude",
    )
    if not target_pos and term.endswith(noun_suffixes) and "noun" in available_pos:
        target_pos = ["noun"]
    if not target_pos:
        has_multiple_senses = any("；" in note or ";" in note or " / " in note for note in source_notes)
        target_pos = (
            available_pos[:2] if has_multiple_senses else available_pos[:1]
        ) or word.get("pos", [])

    selected: list[dict[str, str]] = []
    per_pos_limit = 2 if len(target_pos) == 1 else 1
    for pos in target_pos:
        candidates = [item for item in definitions if item.get("pos") == pos]
        candidates.sort(
            key=lambda item: (
                any(marker in item.get("definition", "").casefold() for marker in LOW_VALUE_MARKERS),
                len(item.get("definition", "")) > 240,
            )
        )
        selected.extend(candidates[:per_pos_limit])
    if selected:
        for item in selected:
            item["definition"] = re.sub(
                r"^\([^)]*\)\s*", "", item["definition"]
            ).strip()
        word["english_definitions"] = selected[:4]
        word["pos"] = unique_strings(item["pos"] for item in selected)

    preferred = unique_strings(preferred_synonyms)
    api_synonyms = [
        synonym for synonym in word.get("synonyms", [])
        if synonym.casefold() != term
        and not synonym.casefold().startswith(term)
        and not term.startswith(synonym.casefold())
    ]
    word["synonyms"] = unique_strings([*preferred, *api_synonyms])[:6]
    word["examples"] = word.get("examples", [])[:1]
    original_chinese = unique_strings(preferred_chinese)
    word["chinese"] = original_chinese or unique_strings(word.get("chinese", []))[:3]


KNOWN_QUESTIONABLE_GROUPS = {
    frozenset(("antithesis", "paradox")),
    frozenset(("confer", "conference")),
    frozenset(("generates", "originate")),
    frozenset(("proxy", "represent")),
    frozenset(("retrospective", "surveying")),
    frozenset(("whim", "whimsical")),
    frozenset(("prevent", "preclude", "obscure")),
}


def preferred_pos_for_groups(
    words: list[dict[str, Any]], records: list[dict[str, Any]]
) -> dict[str, list[str]]:
    by_term = {word["term"].casefold(): word for word in words}
    result: dict[str, list[str]] = {}
    for record in records:
        terms = record.get("normalized_terms", [])
        group = frozenset(terms)
        if (
            record.get("relation") != "synonym_group"
            or len(terms) < 2
            or group in KNOWN_QUESTIONABLE_GROUPS
            or any(term not in by_term for term in terms)
        ):
            continue
        pos_sets = [
            {item["pos"] for item in by_term[term].get("english_definitions", [])}
            for term in terms
        ]
        shared = set.intersection(*pos_sets) if all(pos_sets) else set()
        if shared:
            ordered = [
                pos for pos in ("adjective", "verb", "noun", "adverb", "phrase", "other")
                if pos in shared
            ]
            for term in terms:
                result[term] = ordered
    return result


def reconcile_group_flags(words: list[dict[str, Any]], records: list[dict[str, Any]]) -> None:
    by_term = {word["term"].casefold(): word for word in words}
    grouped_terms = {
        frozenset(record["normalized_terms"])
        for record in records
        if record.get("relation") == "synonym_group" and len(record["normalized_terms"]) > 1
    }
    keep_flag: set[str] = set()
    for group in grouped_terms:
        pos_sets = [set(by_term[term]["pos"]) for term in group if term in by_term]
        comparable = [({"noun", "verb", "adjective", "adverb"} if pos == {"phrase"} else pos) for pos in pos_sets]
        shared_pos = set.intersection(*comparable) if comparable and all(comparable) else set()
        if not shared_pos or group in KNOWN_QUESTIONABLE_GROUPS:
            keep_flag.update(group)
    for word in words:
        term = word["term"].casefold()
        if term not in keep_flag:
            word["review_flags"] = [
                flag for flag in word.get("review_flags", [])
                if flag != "possible_wrong_synonym"
            ]
        else:
            questionable_peers = set().union(
                *(group - {term} for group in grouped_terms if term in group)
            )
            word["synonyms"] = [
                synonym for synonym in word.get("synonyms", [])
                if synonym.casefold() not in questionable_peers
            ]


def atomic_write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", newline="\n", dir=path.parent, delete=False
    ) as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
        temporary = Path(handle.name)
    for attempt in range(6):
        try:
            os.replace(temporary, path)
            break
        except PermissionError:
            if attempt == 5:
                temporary.unlink(missing_ok=True)
                raise
            time.sleep(0.1 * (attempt + 1))


def load_wordbank(path: Path = WORDS_PATH) -> dict[str, Any]:
    if not path.exists():
        return {"schema_version": 1, "words": []}
    with path.open(encoding="utf-8") as handle:
        return json.load(handle)


def save_wordbank(payload: dict[str, Any], path: Path = WORDS_PATH) -> None:
    payload["words"] = sorted(payload.get("words", []), key=lambda item: item["term"].casefold())
    atomic_write_json(path, payload)


class DefinitionModel(BaseModel):
    model_config = ConfigDict(extra="forbid")
    pos: str
    definition: str


class DerivativeModel(BaseModel):
    model_config = ConfigDict(extra="forbid")
    term: str
    relation: str


class ConfusableModel(BaseModel):
    model_config = ConfigDict(extra="forbid")
    term: str
    distinction: str


class LLMExampleModel(BaseModel):
    model_config = ConfigDict(extra="forbid")
    sentence: str
    translation: str


class EnrichedWordModel(BaseModel):
    model_config = ConfigDict(extra="forbid")
    term: str
    lemma: str
    pos: list[Literal[
        "noun", "verb", "adjective", "adverb", "preposition", "conjunction",
        "pronoun", "determiner", "interjection", "phrase", "other"
    ]]
    chinese: list[str]
    english_definitions: list[DefinitionModel]
    synonyms: list[str] = Field(max_length=8)
    derivatives: list[DerivativeModel]
    confusables: list[ConfusableModel]
    examples: list[LLMExampleModel] = Field(max_length=1)
    review_flags: list[str]


class EnrichmentBatchModel(BaseModel):
    model_config = ConfigDict(extra="forbid")
    words: list[EnrichedWordModel]


LLM_SYSTEM_PROMPT = """You edit compact GRE vocabulary cards. Return one card per input term.
Use only the GRE-relevant senses supported by the learner note and dictionary evidence. Write concise,
natural English definitions instead of copying long dictionary prose. Keep 2-6 useful strict synonyms
when available. Add derivatives and confusables only when they have clear learning value. Preserve the
exact input term. An LLM-created example must have no external attribution; provenance is added by the
caller. Use objective review flags only: ambiguous_term, multiple_possible_pos, api_mismatch,
missing_definition, possible_wrong_synonym, possible_wrong_derivative, possible_confusable,
missing_example, or source_incomplete. Never emit a confidence score."""


def enrich_batch_with_openai(
    candidates: list[dict[str, Any]], *, model: str
) -> list[dict[str, Any]]:
    from openai import OpenAI

    client = OpenAI()
    compact = []
    for word in candidates:
        compact.append(
            {
                "term": word["term"],
                "personal_notes": word.get("personal_notes", []),
                "current_chinese": word.get("chinese", []),
                "dictionary_definitions": word.get("english_definitions", []),
                "dictionary_pos": word.get("pos", []),
                "dictionary_synonyms": word.get("synonyms", []),
            }
        )
    response = client.responses.parse(
        model=model,
        input=[
            {"role": "system", "content": LLM_SYSTEM_PROMPT},
            {"role": "user", "content": json.dumps(compact, ensure_ascii=False)},
        ],
        text_format=EnrichmentBatchModel,
    )
    parsed = response.output_parsed
    if parsed is None:
        raise RuntimeError("OpenAI returned no parsed enrichment payload")
    return [item.model_dump() for item in parsed.words]


def llm_word_to_record(item: dict[str, Any]) -> dict[str, Any]:
    term = normalize_term(item["term"])
    record = empty_word(term)
    record.update({key: item[key] for key in (
        "lemma", "pos", "chinese", "english_definitions", "synonyms",
        "derivatives", "confusables", "review_flags"
    )})
    record["examples"] = [
        {
            "sentence": example["sentence"],
            "translation": example["translation"],
            "source_type": "llm",
            "author": None,
            "url": None,
            "license": None,
        }
        for example in item.get("examples", [])
    ]
    return record


def chunks(values: list[Any], size: int) -> Iterable[list[Any]]:
    for index in range(0, len(values), size):
        yield values[index:index + size]


def dictionary_cache_path(term: str) -> Path:
    return CACHE_DIR / "dictionary" / f"{slugify(term)}.json"


def llm_cache_path(term: str) -> Path:
    return CACHE_DIR / "llm" / f"{slugify(term)}.json"


def write_raw_records(records: list[dict[str, Any]], path: Path = RAW_RECORDS_PATH) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [json.dumps(record, ensure_ascii=False) for record in records]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")


def flag_counts(words: Iterable[dict[str, Any]]) -> dict[str, int]:
    counts: defaultdict[str, int] = defaultdict(int)
    for word in words:
        for flag in set(word.get("review_flags", [])):
            counts[flag] += 1
    return dict(sorted(counts.items(), key=lambda item: (-item[1], item[0])))
