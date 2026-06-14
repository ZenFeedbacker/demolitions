# Reviewer orientation

Context for someone reviewing this codebase. User-facing docs are in
[README.md](README.md) (Greek); this file is the developer/reviewer brief.

## What it is

A Flask web app + CLI that maps Greek demolition permits from the **Diavgeia**
open-government API. Given an area (δήμος / νομός / περιφέρεια / «Ελλάδα») and a
date range, it finds the final demolition permits, parses each permit PDF for
address + building outline, geocodes, and produces an xlsx + an interactive
table/map. Runs are persisted (local disk or Cloudflare R2).

## Run it

```sh
pip install -r requirements.txt          # + system: poppler-utils (pdftotext)
python3 -m demolitions --area "Δήμος Δράμας" --from 01/01/2021 --to 31/12/2021 -o drama
python3 webui.py                         # local web UI on :8741
python3 tests.py                         # offline test suite (pip install "moto[s3]" for the R2 test)
```

Hosted: Docker (`Dockerfile`) → gunicorn; storage on R2 (env `DEMOLITIONS_STORAGE=r2`
+ `R2_*`). Deploy is Render, gated by GitHub Actions (`.github/workflows/ci.yml`).

## Module map

| File | Responsibility |
|------|----------------|
| `webui.py` | Flask app, single background job, polling API, streaming zip, storage wiring |
| `demolitions/__main__.py` | CLI entry (`python3 -m demolitions`) |
| `demolitions/pipeline.py` | `run_pipeline` (search→PDF→xlsx) + `enrich_geocode`; callbacks `log/step/cancel` |
| `demolitions/diavgeia.py` | search client, date-window chunking, `permit_kind` |
| `demolitions/pdfparse.py` | PDF download + field/floors/extent/polygon extraction |
| `demolitions/egsa87.py` | ΕΓΣΑ87 (EPSG:2100) → WGS84 transform |
| `demolitions/areas.py` | Kallikratis resolution, autocomplete list, homonym handling |
| `demolitions/geocode.py` | Nominatim fallback geocoder (cached) |
| `demolitions/greek.py`, `dimoi_data.py` | toponym accenting (lexicon + Wikipedia list) |
| `demolitions/output.py` | xlsx writer + per-year/δήμος pivots |
| `demolitions/storage.py` | `LocalStorage` / `R2Storage` backends |
| `templates/index.html` | the whole SPA (autocomplete, polling, table, Leaflet map, history, about) |

## Non-obvious domain facts (these justify otherwise-odd code)

- **Diavgeia API**: all building permits are published by org `99201077` (ΤΕΕ),
  `decisionTypeUid 2.4.6.1`. The `issueDate` window is capped at ~6 months →
  `diavgeia._windows` chunks into 179-day spans. `extraFieldValues.municipality`
  is **not** queryable server-side → filtered client-side. Subject search is
  stemmed (returns προεγκρίσεις/αναθεωρήσεις too) → `permit_kind()` re-filters.
- **No CORS** on Diavgeia (verified) → a browser cannot call it; that is the only
  reason a server backend exists rather than a pure static site.
- **Coverage starts Oct 2018** (e-Άδειες launch = `E_ADEIES_START`), even though
  Diavgeia-the-platform began 2010. The default/min "from" date is 2018-10-01.
- **Coordinates**: ~98% of permit PDFs carry a «Συντεταγμένες» field = the
  building polygon in **ΕΓΣΑ87 (EPSG:2100)**. `egsa87.py` is a self-contained
  inverse Transverse-Mercator (Snyder series) + **EPSG:1272 datum shift** to
  WGS84 (≈1 m; validated against street geocodes). Nominatim is only the fallback
  for the ~2% without the field.
- **Accents cannot be derived from all-caps unaccented text** → δήμοι names come
  from a generated `dimoi_data.py` (Greek Wikipedia list), other toponyms from a
  word lexicon in `greek.py`. Unknown words are title-cased without an accent.
- **Data pathologies, surfaced not hidden**: the same building often gets several
  final permits (flagged «πιθανό διπλό»); Ηράκλειο Κρήτης permits are frequently
  mis-tagged to the homonymous Attica δήμος (flagged «~Xkm από τον δήμο»).
- The tool counts only **final** permits, two kinds (`eidos`): standalone «Άδεια
  Κατεδάφισης», and «Οικοδομική Άδεια» whose subject mentions κατεδάφιση (rare).

## Data schemas

`run.json` (manifest): `run_id, area, area_query, from, to, created, n_rows,
n_dups, geocoded, has_pdfs`.

`rows.json` (list); each row: `ada, url, date (ISO), year, dimos, eidos, ektasi,
dim_enotita, poli, odos, ar_apo, ar_eos, ot, kaek, perigrafi, orofoi, lat, lon,
poly (list of [lat,lon]), precision, parse_ok, pdf_path, flags`.

Storage layout (both backends): `runs/<run_id>/{run.json, rows.json,
<run_id>.xlsx, pdf/<δήμος>/<έτος>/<ΑΔΑ>.pdf}`.

## Key invariants & the hosted flow

- **One job at a time** (no concurrency by design). `webui._start` refuses a
  second run (409). Workers mutate the `Job` instance passed to them, never the
  module global, so a later run can't corrupt an earlier one's state.
- **Hosted run flow**: `run_pipeline` writes a staging dir → `store.save_run`
  uploads to R2 (`run.json` uploaded **last**, so a half-upload never shows as a
  finished run) → `free_local_pdfs` drops staging PDFs → `enrich_geocode` adds
  coordinates → `save_meta` re-uploads only json/xlsx (PDFs not re-uploaded).
- **PDF cache cap**: `enforce_pdf_cap` keeps the newest run's PDFs unconditionally,
  then keeps older runs until `DEMOLITIONS_PDF_CACHE_LIMIT` (default 1 GB) is hit;
  beyond that it deletes only PDF bytes (metadata stays, zip rebuilds on-demand).
- **Local mode is the default** and behaves exactly as before R2 existed
  (`LocalStorage` no-ops the upload/cap/free methods).
- **Traversal safety**: `LocalStorage.open_member/_dir` resolve and check
  `is_relative_to` the runs root; route `<run_id>` segments can't contain `/`.

## What the tests cover / don't

`tests.py` (offline) covers: area resolution + homonyms, Greek casing, floors &
extent detection, PDF field + polygon parsing (incl. long polygons & missing
poppler), ΕΓΣΑ87, geocode helpers, xlsx output + pivots, `permit_kind`,
`LocalStorage` lifecycle, `R2Storage` via **moto** (roundtrip + eviction), and the
Flask endpoints via the test client (runs/sizes, about, serve, zip, clear-PDF,
delete, validation, traversal, healthz).

**Not** covered (needs live network / a browser): the real Diavgeia search, real
R2, the on-demand zip re-fetch from Diavgeia, and Leaflet rendering. Verify those
manually against the deployed site.

## Known, accepted limitations (not bugs)

- `rows.json` + xlsx are built in memory; fine until nationwide («Ελλάδα») runs.
- `parse_fields` assumes `pdftotext -layout`'s multi-space columns.
- Geocoding for a νομός/country can be slow (1 req/s) — but it rarely runs now
  that coordinates come from the PDF.
- A very large area downloads many PDFs to the host's ephemeral disk transiently
  before upload; the R2 cap protects long-term storage, not that transient peak —
  prefer local runs for whole-country sweeps.
