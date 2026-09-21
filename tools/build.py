#!/usr/bin/env python3
"""Build the offline, single-file GRE vocabulary tool.

    data/words.json    <- 唯一词汇数据源 (由 curation / import 流程维护)
    data/lists.json    <- 练习 list 数据源
    app/template.html  (+ app/app.css, app/app.js)
                |
                v
    dist/En-735.html

``dist/`` 下的文件名默认是 ``[目标语言简称]-[本次 build 的词库词数].html``；
用 ``--name`` 自定义、``--lang`` 改语言简称、``--out`` 指定完整路径。同名文件直接覆盖。

The generated HTML embeds the raw data as ``VOCABULARY_DATA`` / ``LIST_DATA``
and reads nothing from the network at runtime.

Usage
-----
    python tools/build.py                  # dist/<lang>-<词数>.html，例如 dist/En-735.html
    python tools/build.py --name my-tool   # 自定义文件名 → dist/my-tool.html（同名直接覆盖）
    python tools/build.py --lang Zh        # 改默认文件名里的语言简称
    python tools/build.py --out out/x.html # 指定完整输出路径（优先于 --name）
    python tools/build.py --check          # validate data + template only, write nothing
    python tools/build.py --chunk-size 50  # override the auto list size (0 disables auto lists)

Notes
-----
* 默认输出文件名是 ``[目标语言简称]-[本次 build 的词库词数].html``（如 ``En-735.html``），
  每次 build 都按当前 ``data/words.json`` 重新计算；不检查重名，直接覆盖。
* ``data/lists.json`` only holds *configuration* (``chunk_size``) and optional
  hand-written lists. The ``all`` list and the ``part-NN`` lists are generated
  from ``data/words.json`` on every build, so removing or adding words can never
  leave a stale list behind. It is created automatically when missing.
* Data problems are reported as warnings; only structural problems that would
  break the app (missing template, unreadable JSON, bad placeholders) abort
  the build.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any, Iterable

ROOT = Path(__file__).resolve().parents[1]

WORDS_PATH = ROOT / "data" / "words.json"
LISTS_PATH = ROOT / "data" / "lists.json"
APP_DIR = ROOT / "app"
TEMPLATE_PATH = APP_DIR / "template.html"
CSS_PATH = APP_DIR / "app.css"
JS_PATH = APP_DIR / "app.js"
DIST_DIR = ROOT / "dist"
DEFAULT_LANGUAGE_CODE = "En"

WORDS_PLACEHOLDER = "/*__VOCABULARY_DATA__*/"
LISTS_PLACEHOLDER = "/*__LIST_DATA__*/"
CSS_PLACEHOLDER = "/*__APP_CSS__*/"
JS_PLACEHOLDER = "/*__APP_JS__*/"

ALL_LIST_ID = "all"
ALL_LIST_NAME = "All Words"
DEFAULT_CHUNK_SIZE = 30
GENERATED_LIST_PATTERN = re.compile(r"^(?:all|part-\d+)$")


class BuildError(RuntimeError):
    """Raised when the build cannot produce a usable HTML file."""


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except FileNotFoundError as error:  # pragma: no cover - friendly message
        raise BuildError(f"缺少文件: {path}") from error
    except UnicodeDecodeError as error:  # pragma: no cover - friendly message
        raise BuildError(f"文件不是 UTF-8: {path} ({error})") from error


def load_json(path: Path) -> Any:
    text = read_text(path)
    try:
        return json.loads(text)
    except json.JSONDecodeError as error:
        raise BuildError(f"{path} 不是合法 JSON: {error}") from error


def encode_for_script(payload: Any) -> str:
    """Compact JSON that is safe to inline inside a ``<script>`` element."""
    text = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    return (
        text.replace("<", "\\u003c")
        .replace(">", "\\u003e")
        .replace("&", "\\u0026")
        .replace("\u2028", "\\u2028")
        .replace("\u2029", "\\u2029")
    )


def string_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    result: list[str] = []
    for item in value:
        if isinstance(item, str) and item.strip():
            result.append(item)
    return result


def dedupe(values: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        if value not in seen:
            seen.add(value)
            result.append(value)
    return result


def default_out_name(word_count: int, language_code: str = DEFAULT_LANGUAGE_CODE) -> str:
    """``[目标语言简称]-[本次 build 的词库词数].html``，例如 ``En-735.html``。"""
    return f"{language_code}-{word_count}.html"


def default_out_path(word_count: int, language_code: str = DEFAULT_LANGUAGE_CODE) -> Path:
    return DIST_DIR / default_out_name(word_count, language_code)


def resolve_out_path(
    *,
    out: Path | None = None,
    name: str | None = None,
    out_dir: Path = DIST_DIR,
) -> Path | None:
    """把 ``--out`` / ``--name`` 解析成输出路径；两者都没给时返回 None（由词数决定默认名）。"""
    if out is not None:
        return out
    if name:
        filename = name if name.lower().endswith(".html") else f"{name}.html"
        return out_dir / filename
    return None


def shipped_out_path(language_code: str = DEFAULT_LANGUAGE_CODE) -> Path:
    """当前词库对应的默认产物路径（测试 / 文档用）。"""
    payload, _ = load_words()
    return default_out_path(len(payload["words"]), language_code)


# --------------------------------------------------------------------------- #
# data loading / validation
# --------------------------------------------------------------------------- #
def load_words(path: Path = WORDS_PATH) -> tuple[dict[str, Any], list[str]]:
    """Return the payload for ``VOCABULARY_DATA`` plus human-readable warnings."""
    payload = load_json(path)
    warnings: list[str] = []
    if not isinstance(payload, dict):
        raise BuildError(f"{path} 顶层必须是 JSON object")
    if payload.get("schema_version") != 1:
        warnings.append(f"schema_version={payload.get('schema_version')!r}（预期 1）")

    raw_words = payload.get("words")
    if not isinstance(raw_words, list):
        raise BuildError(f"{path} 缺少 words 数组")

    words: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    skipped = 0
    for index, raw in enumerate(raw_words):
        if not isinstance(raw, dict):
            skipped += 1
            continue
        word_id = raw.get("id")
        term = raw.get("term")
        if not isinstance(word_id, str) or not word_id.strip():
            skipped += 1
            continue
        if not isinstance(term, str) or not term.strip():
            skipped += 1
            continue
        if word_id in seen_ids:
            skipped += 1
            continue
        seen_ids.add(word_id)
        words.append(raw)

    if skipped:
        warnings.append(f"跳过 {skipped} 条无法使用的词条（缺少 id/term 或 id 重复）")
    if not words:
        warnings.append("words.json 中没有任何可用词条")

    return {"schema_version": 1, "words": words}, warnings


def default_lists_file() -> dict[str, Any]:
    """``data/lists.json`` 只保存配置与自定义 list；结构化 list 由 build 生成。"""
    return {"schema_version": 1, "chunk_size": DEFAULT_CHUNK_SIZE, "lists": []}


def all_list(words: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "id": ALL_LIST_ID,
        "name": ALL_LIST_NAME,
        "description": f"All {len(words)} words",
        "word_ids": [word["id"] for word in words],
    }


def chunk_lists(words: list[dict[str, Any]], chunk_size: int) -> list[dict[str, Any]]:
    """每 ``chunk_size`` 个词切成一个 list，最后不足一段的单独成为一个 list。

    ``chunk_size <= 0`` 表示不自动切分。
    """
    if chunk_size <= 0 or not words:
        return []
    total = len(words)
    part_count = (total + chunk_size - 1) // chunk_size
    width = max(2, len(str(part_count)))
    result: list[dict[str, Any]] = []
    for index, start in enumerate(range(0, total, chunk_size), start=1):
        part = words[start:start + chunk_size]
        if not part:
            continue
        first, last = start + 1, start + len(part)
        result.append(
            {
                "id": f"part-{index:0{width}d}",
                "name": f"Part {index:0{width}d}",
                "description": f"Words {first}-{last}",
                "word_ids": [word["id"] for word in part],
            }
        )
    return result


def configured_chunk_size(payload: dict[str, Any], warnings: list[str]) -> int:
    value = payload.get("chunk_size", DEFAULT_CHUNK_SIZE)
    if isinstance(value, bool) or not isinstance(value, int):
        warnings.append(
            f"lists.json 的 chunk_size 需要是整数，已改用 {DEFAULT_CHUNK_SIZE}"
        )
        return DEFAULT_CHUNK_SIZE
    if value < 0:
        warnings.append(f"lists.json 的 chunk_size 不能为负，已改用 {DEFAULT_CHUNK_SIZE}")
        return DEFAULT_CHUNK_SIZE
    return value


def load_lists(
    words: list[dict[str, Any]],
    path: Path = LISTS_PATH,
    chunk_size: int | None = None,
) -> tuple[dict[str, Any], list[str], bool]:
    """Return (LIST_DATA payload, warnings, created).

    ``lists.json`` 缺失时会自动创建（只含 ``chunk_size`` 与空的自定义列表）。
    自动生成的 list（``all``、``part-NN``）总是以 ``words.json`` 为准。
    """
    created = False
    if not path.exists():
        payload = default_lists_file()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        created = True
    else:
        payload = load_json(path)
        if not isinstance(payload, dict):
            raise BuildError(f"{path} 顶层必须是 JSON object")

    warnings: list[str] = []
    size = chunk_size if chunk_size is not None else configured_chunk_size(payload, warnings)

    raw_lists = payload.get("lists")
    if not isinstance(raw_lists, list):
        warnings.append(f"{path} 缺少 lists 数组，按空列表处理")
        raw_lists = []

    known_ids = {word["id"] for word in words}
    word_order = {word["id"]: index for index, word in enumerate(words)}

    manual: list[dict[str, Any]] = []
    seen_list_ids: set[str] = set()
    for raw in raw_lists:
        if not isinstance(raw, dict):
            warnings.append(f"跳过非 object 的 list: {raw!r}")
            continue
        list_id = raw.get("id")
        name = raw.get("name")
        if not isinstance(list_id, str) or not list_id.strip():
            warnings.append(f"跳过缺少 id 的 list: {name!r}")
            continue
        if GENERATED_LIST_PATTERN.match(list_id):
            warnings.append(
                f"list {list_id}: 该 id 由 build 自动生成，lists.json 里的定义已忽略"
            )
            continue
        if list_id in seen_list_ids:
            warnings.append(f"跳过重复的 list id: {list_id}")
            continue
        if not isinstance(name, str) or not name.strip():
            warnings.append(f"list {list_id} 缺少 name，已使用 id 作为名称")
            name = list_id
        seen_list_ids.add(list_id)

        requested = dedupe(string_list(raw.get("word_ids")))
        unknown = [word_id for word_id in requested if word_id not in known_ids]
        kept = [word_id for word_id in requested if word_id in known_ids]
        if unknown:
            warnings.append(
                f"list {list_id}: {len(unknown)} 个 word_id 不在 words.json 中，已忽略"
            )
        manual.append(
            {
                "id": list_id,
                "name": name,
                "description": raw.get("description")
                if isinstance(raw.get("description"), str)
                else "",
                "word_ids": kept,
            }
        )

    for item in manual:
        item["word_ids"].sort(key=lambda word_id: word_order.get(word_id, 0))

    lists = [all_list(words), *chunk_lists(words, size), *manual]
    return {"schema_version": 1, "lists": lists}, warnings, created


# --------------------------------------------------------------------------- #
# build
# --------------------------------------------------------------------------- #
def render_html(
    template: str,
    vocabulary_data: dict[str, Any],
    list_data: dict[str, Any],
    css: str,
    js: str,
) -> str:
    missing = [
        name
        for name, placeholder in (
            ("VOCABULARY_DATA", WORDS_PLACEHOLDER),
            ("LIST_DATA", LISTS_PLACEHOLDER),
            ("APP_CSS", CSS_PLACEHOLDER),
            ("APP_JS", JS_PLACEHOLDER),
        )
        if placeholder not in template
    ]
    if missing:
        raise BuildError(
            f"template.html 缺少占位符: {', '.join(missing)}"
        )

    # guard against a literal </script> inside the inlined assets
    safe_js = js.replace("</script", "<\\/script")
    safe_css = css.replace("</style", "<\\/style")

    html = template
    html = html.replace(CSS_PLACEHOLDER, safe_css)
    html = html.replace(WORDS_PLACEHOLDER, encode_for_script(vocabulary_data))
    html = html.replace(LISTS_PLACEHOLDER, encode_for_script(list_data))
    html = html.replace(JS_PLACEHOLDER, safe_js)
    return html


def build(
    out_path: Path | None = None,
    *,
    words_path: Path = WORDS_PATH,
    lists_path: Path = LISTS_PATH,
    app_dir: Path = APP_DIR,
    chunk_size: int | None = None,
    language_code: str = DEFAULT_LANGUAGE_CODE,
    check_only: bool = False,
    log: Any = print,
) -> dict[str, Any]:
    vocabulary_data, word_warnings = load_words(words_path)
    if out_path is None:
        out_path = default_out_path(len(vocabulary_data["words"]), language_code)
    list_data, list_warnings, lists_created = load_lists(
        vocabulary_data["words"], lists_path, chunk_size
    )
    if lists_created:
        log(f"未找到 {lists_path}，已创建（chunk_size={DEFAULT_CHUNK_SIZE}，无自定义 list）")

    for warning in [*word_warnings, *list_warnings]:
        log(f"[warn] {warning}")

    generated = sum(1 for item in list_data["lists"] if GENERATED_LIST_PATTERN.match(item["id"]))
    custom = len(list_data["lists"]) - generated

    if check_only:
        log(
            f"check ok: {len(vocabulary_data['words'])} words, {len(list_data['lists'])} lists "
            f"= 1 all + {generated - 1} parts + {custom} custom"
        )
        log(f"would write: {out_path}")
        return {
            "words": len(vocabulary_data["words"]),
            "lists": len(list_data["lists"]),
            "generated_lists": generated,
            "custom_lists": custom,
            "out_path": str(out_path),
            "written": None,
        }

    template = read_text(app_dir / "template.html")
    css = read_text(app_dir / "app.css")
    js = read_text(app_dir / "app.js")
    html = render_html(template, vocabulary_data, list_data, css, js)

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(html, encoding="utf-8")

    size_kb = len(html.encode("utf-8")) / 1024
    log(
        f"已生成 {out_path} ({size_kb:.0f} KB, {len(vocabulary_data['words'])} words, "
        f"{len(list_data['lists'])} lists = 1 all + {generated - 1} parts + {custom} custom)"
    )
    if size_kb > 2048:
        log("[warn] 输出文件超过 2 MB，移动端首次打开可能较慢")
    return {
        "words": len(vocabulary_data["words"]),
        "lists": len(list_data["lists"]),
        "generated_lists": generated,
        "custom_lists": custom,
        "out_path": str(out_path),
        "written": str(out_path),
        "bytes": len(html.encode("utf-8")),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build the single-file vocabulary app")
    parser.add_argument(
        "--out",
        type=Path,
        default=None,
        help="完整输出路径（优先于 --name；缺省按 [语言简称]-[词数].html 生成）",
    )
    parser.add_argument(
        "--name",
        default=None,
        help=f"输出文件名，放在 {DIST_DIR.name}\\ 下；不含 .html 会自动补上；同名直接覆盖",
    )
    parser.add_argument(
        "--lang",
        default=DEFAULT_LANGUAGE_CODE,
        help=f"目标语言简称，用于默认文件名（默认 {DEFAULT_LANGUAGE_CODE}）",
    )
    parser.add_argument("--check", action="store_true", help="只校验数据，不生成文件")
    parser.add_argument(
        "--chunk-size",
        type=int,
        default=None,
        help=f"自动 list 的词数（默认取 lists.json 的 chunk_size，缺失时 {DEFAULT_CHUNK_SIZE}；0 表示不自动切分）",
    )
    args = parser.parse_args(argv)

    try:
        build(
            out_path=resolve_out_path(out=args.out, name=args.name),
            chunk_size=args.chunk_size,
            language_code=args.lang,
            check_only=args.check,
        )
    except BuildError as error:
        print(f"[error] {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
