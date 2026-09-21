# Vocab Garden — A Wordbank

A personal, offline GRE vocabulary flashcard app. The whole wordbank is compiled into a
single self-contained HTML file you can open in any browser (desktop or phone) — no server,
no network, no external assets.

## Quick start

```powershell
# 1. install Python dependencies
pip install -r requirements.txt

# 2. build the app from the current wordbank (data/words.json)
python tools\build.py

# 3. open the result
start dist\En-735.html
```

`dist\En-735.html` is a checked-in example build (735 words). Rebuild it after changing
the word data or the app code — the output file name is `<lang>-<word count>.html`.

## Add your own words

1. Append new words to `inbox.txt`, one per line, as `term | note`.
2. Merge them into the wordbank:

   ```powershell
   python tools\add_words.py --dry-run    # preview what will change
   python tools\add_words.py --skip-llm   # dictionary APIs only, no OpenAI key needed
   ```

3. Rebuild the app: `python tools\build.py`.

The author's original notes file (`verbalword.md`) is a personal corpus and is not tracked
by Git — build your own starting from `inbox.txt`.

## Tests

```powershell
python -m unittest discover -s tests
```

## Layout

| Path | Purpose |
| --- | --- |
| `app/` | Source of the single-file app (`template.html`, `app.css`, `app.js`) |
| `tools/` | Maintenance scripts: build, add/remove words, curate, validate |
| `tests/` | Unit tests for the tools and the build |
| `diction_lookup/` | Standalone dictionary lookup helper (FreeDictionaryAPI / Wiktionary / Datamuse / Tatoeba) |
| `data/` | Wordbank data: `words.json` (single source of truth), schema, curation files |
| `dist/` | Generated builds; `En-735.html` is the checked-in example |
| `inbox.txt` | New words to import, one per line |

## Notes

- Never edit the built HTML by hand — change `data/` or `app/` and rebuild.
- Secrets (`.env`), API caches (`data/enrichment_cache/`), temp files and non-example
  builds under `dist/` are ignored by Git.
