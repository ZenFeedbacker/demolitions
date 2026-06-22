# Review packet — `demolitions`

**Audience:** an LLM performing a fresh, independent review round on this repo.
**Your job:** scrutinize the uncommitted changeset described below for correctness,
security, robustness, and maintainability; confirm or refute the claims here; surface
anything missed. Everything below is **uncommitted** in the working tree on top of the
last commit (`HEAD = 61386f9`). Treat the **working tree** as the code under review.

## 0. How to run

```bash
cd ~/claude/demolitions
python3 tests.py                                   # offline, deterministic
python3 -m pyflakes demolitions/ webui.py tests.py # lint
```
Current state: **100 tests OK, pyflakes clean.** Tests need no network; the R2 tests
skip if `moto` is absent (it's installed here, so they run). CI is offline.

## 1. What the project is

A Flask web app + CLI that maps Greek **demolition permits** from the Diavgeia
open-data API. Pipeline per run: search permits (org `99201077` ΤΕΕ, decision type
`2.4.6.1`, ≤6-month windows, client-side municipality filter) → download & parse each
permit PDF (`pdftotext -layout`, extracts the building's ΕΓΣΑ87 coordinate polygon →
WGS84; ~98% of permits have coordinates) → optional Nominatim geocoding for the rest →
write xlsx → serve table/map/history + zip download.

**Architecture:** `demolitions/` package (`areas`, `diavgeia`, `egsa87`, `geocode`,
`greek`, `output`, `pdfparse`, `pipeline`, `storage`, `__main__`); `webui.py` (Flask);
`templates/index.html` (front-end JS); `tests.py`; bundled data in `demolitions/data/`
(`kallikratis.json`, `pe_centroids.json`).

**Deployment:** Render free tier — **512 MB RAM**, ephemeral `/tmp` disk, instance
**sleeps after ~15 min** with no inbound HTTP. Durable run history in Cloudflare R2
(S3-compatible, free 10 GB). Storage is abstracted (`storage.py`): `LocalStorage`
(disk, default/CLI) vs `R2Storage` (hosted). `run.json` is uploaded **last** so a run
only "appears" in history once complete. PDFs are cached in R2 for recent runs and
evicted oldest-first past a size cap; evicted runs rebuild their zip on demand from
Diavgeia. Single-job model: one run at a time, on a background thread, driven by a
global `job`; `gunicorn -w 1 --threads N`.

**Constraints any change must respect:** tests stay offline (bundle data, don't hit
Diavgeia/Nominatim in tests); Nominatim politeness ~1 req/s serial; the geocode cache
(`geocode.json`) is persisted to R2 (`store.pull_cache`/`push_cache`) so a bounded
one-time set of lookups is OK but per-row/per-run live lookups are not; keep the
front-end «Χωρίς ύποπτη θέση» filter and history-run compatibility working.

## 2. The changeset under review (all uncommitted, on top of `61386f9`)

Diffstat:
```
 Dockerfile              |  10 +-
 demolitions/areas.py    |   6 +-
 demolitions/diavgeia.py |   9 ++
 demolitions/geocode.py  |  96 +++++------
 demolitions/pdfparse.py |  20 ++-
 demolitions/pipeline.py |  57 ++++---
 demolitions/storage.py  |  10 ++
 tests.py                | 413 ++++++++++++++++++++++++++++++++++--------
 + NEW demolitions/data/pe_centroids.json   (75 PE centroids)
 + NEW docs/homonym-out-of-region-issue.md  (issue spec for the feature below)
```

It bundles three waves of work. Each is summarized with rationale, the key code, and
**residual risks for you to probe**.

---

### A. Feature: out-of-region permit detection (the headline change)

**Problem.** Two municipalities are both officially «ΔΗΜΟΣ ΗΡΑΚΛΕΙΟΥ»: code `7101`
(Heraklion, **Crete**) and `4604` (Νέο Ηράκλειο, **Attica**). In the e-Άδειες dropdown
they are identical strings, so submitters of Crete permits often pick the Attica code.
Diavgeia then tags a Crete building with an Attica municipality code, and a «Περιφέρεια
Αττικής» search returns it. ~25 such Crete entries were leaking into an Attica search.
Full spec: `docs/homonym-out-of-region-issue.md`.

**Design history (important — two earlier approaches were rejected):**
1. `Geocoder.dimos_distance_km` (Nominatim per declared municipality *name*) — failed
   because querying "Δήμος Ηρακλείου Αττικής" returns the Crete city (homonym
   ambiguity) → ~0 km → never flagged. Removed.
2. A data-driven IQR bounding box over all geocoded points — failed when the mis-tagged
   cluster exceeded ~25% of results (the quartiles got contaminated and the fence
   swallowed both clusters), and additionally **false-positived** on geographically
   wide single regions (Νότιο/Βόρειο Αιγαίο, Ιόνιο), flagging legitimate remote
   islands. Both the IQR and a later median+MAD variant were **removed entirely**.

**Final design (current):** a per-row, **code-anchored** check.
`demolitions/geocode.py`:
- `PE_CENTROIDS` — loaded once at import from the bundled `demolitions/data/pe_centroids.json`,
  75 entries keyed by the 2-digit Kallikratis prefix (= Περιφερειακή Ενότητα). The
  test `test_bundled_gazetteer_covers_every_pe_prefix` asserts every `areas.PREFIX_PE`
  prefix is present and in Greece's bbox.
- `row_out_of_region(row, *, distance_km=PE_DISTANCE_KM)` — for a row with
  high-precision coordinates, flags it `"~Xkm εκτός περιοχής αναζήτησης"` if its
  coordinates are farther than `PE_DISTANCE_KM` from the centroid of **its own declared
  PE** (the 2-digit prefix of `row["muni_code"]`). Judges each row independently → immune
  to the contamination fraction and to homonym name ambiguity (anchored by code, not
  name). Skips rows without high-precision coords or without a known PE prefix.
```python
_HIGH_PREC = frozenset(("κτίσμα (PDF)", "οδός+αριθμός", "οδός", "οικισμός"))
PE_DISTANCE_KM = 250.0
def row_out_of_region(row, *, distance_km=PE_DISTANCE_KM):
    if not row.get("lat") or row.get("precision") not in _HIGH_PREC:
        return None
    code = row.get("muni_code") or ""
    center = PE_CENTROIDS.get(code[:2])
    if not center:
        return None
    dist = _haversine(row["lat"], row["lon"], center[0], center[1])
    if dist <= distance_km:
        return None
    return f"~{dist:.0f}km εκτός περιοχής αναζήτησης"
```
`demolitions/pipeline.py`: `enrich_geocode` runs a second pass calling
`_flag_out_of_region(rows)` (just the per-row loop now — the bbox complement was
removed). `templates/index.html`: the «Χωρίς ύποπτη θέση» filter matches `flags`
containing `"από τον δήμο"` (legacy) or `"εκτός περιοχής"`.

**Threshold rationale (`PE_DISTANCE_KM = 250`):** the most remote *legitimate* points
from their own PE centroid measure ~191 km (Αντικύθηρα→ΠΕ Νήσων Αττικής), ~173 km
(Ορμένιο→ΠΕ Έβρου), ~161 km (Στρογγύλη→ΠΕ Ρόδου); the Crete mis-tag is ~323 km. 250
leaves ~59 km above the worst legitimate case and ~73 km below the bug case.

**Verified:** 25/25 mis-tags flagged at all realistic ratios (2.4%→55.6%); 0 false
positives on clean single-region, whole-country, near-border (Ορεστιάδα), and the
**wide-Aegean** regression test (Rhodes/Kos/Karpathos/Kastellorizo correctly NOT
flagged). Tests: `TestRowOutOfRegion`, `TestFlagOutOfRegionPipeline`,
`TestEnrichGeocodeOutOfRegion`.

**Residual risks to probe:**
- `pe_centroids.json` provenance: centroids were geocoded once via Nominatim by PE name
  and two were hand-corrected (73 ΡΕΘΥΜΝΟΥ, 99 ΑΓΙΟΥ ΟΡΟΥΣ). They are **area centroids**,
  not capitals. Are any so off that a legitimate building exceeds 250 km from its PE
  centroid? (Αντικύθηρα margin is only ~59 km.)
- A mis-tag to a *nearby* wrong municipality in the **same** region (< 250 km) is not
  caught. Acceptable? The previous bbox tried to catch these but caused false positives;
  we chose precision over recall here.
- Rows with `precision == "δήμος"` or no coords are never checked (no reliable point).

---

### B. First review round — findings & resolutions

A full read-the-codebase review was done (no P0 issues). Resolutions:

| ID | Finding | Status |
|----|---------|--------|
| P1-1 | bbox complement false-flagged legitimate islands in wide single-region searches (Aegean/Ionian) — would hide real permits behind the filter | **Fixed** — bbox complement removed entirely; per-row check only. Regression test added (`test_wide_single_region_aegean_no_false_positives`). |
| P1-2 | a genuinely out-of-region row flagged twice with two different distances; inflated the "N εγγραφές εκτός" count | **Fixed** — removing the second pass eliminates it; integration test asserts exactly one flag. |
| P2-3 | `n_dups` counted *all* flags (incl. "μη κτίσμα"), mislabeled "πιθανά διπλά" | **Fixed** — `pipeline.py` now counts only `"πιθανό διπλό"`. |
| P2-1 | 250 km threshold rationale (was 200 with ~9 km margin) | **Fixed** — bumped to 250, comment documents the tightest legitimate cases. |
| P2-2 | dead `dimos_distance_km` + dead `dimos_query` row field | **Fixed** — see wave C (dimos_query). |

**Done-well (from that review, still true):** path-traversal defenses on both backends;
resilient R2 streaming with Range-resume; orphan sweeping; workers operate on a passed-in
`Job` (not the global) so a new run can't corrupt an in-flight one; xlsx metadata scrubbed.

---

### C. Deferred issues — one investigation + one fix each

**P2-4 — OOM / SIGKILL 137 on a large search.**
Investigation **measured** the data-side pipeline: peak RSS ~100–150 MB for ~2800
permits — **no single structure approaches 512 MB** (the prior suspect, the `decisions`
list, peaks ~6–7 MB; `del decisions` already frees it before the xlsx is built). The
likely real trigger is a **concurrency amplifier** (`--threads 8`: zip downloads +
status polling stacking on an active run) and/or **glibc arena fragmentation** from
repeated multi-MB PDF alloc/free across upload threads. Fixes applied (the safe,
high-confidence levers):
- `pdfparse.py download_pdf`: now **streams** (`stream=True`, validate `%PDF` on first
  chunk, write incrementally) instead of buffering `r.content` + a copy per permit;
  cleans up partial files on failure.
- `Dockerfile`: `ENV MALLOC_ARENA_MAX=2`; gunicorn `--threads 8 → 4`.
- `pipeline.py`: the "Εκτιμώμενο μέγεθος PDF" estimate used 300 KB/PDF; real mean is
  ~3 MB → constant corrected to 3000 KB (cosmetic log accuracy).
- Tests: `TestDownloadPdf` (asserts streaming, `.content` never accessed, partial-file
  cleanup).
- **Deliberately NOT done** (probe whether you agree): openpyxl `write_only` (workbook
  peak only ~21 MB; risks breaking hyperlinks/fonts/freeze-panes/auto-filter);
  rewriting `serve_zip`'s `rows.json` buffering (would add a streaming-JSON dependency).
- **Open question for you:** the OOM root cause is **inferred, not reproduced**. Is the
  concurrency hypothesis right? Is `--threads 4` enough headroom for polling + a couple
  of concurrent zip downloads during a long run? Could the real culprit be ephemeral
  **disk** fill (staging PDFs) rather than RAM on some runs?

**P2-5 — stale `has_pdfs=True` with `pdf_bytes=0`.**
Reachable (out-of-band R2 deletion; crash between `delete_pdfs` and manifest rewrite)
but impact is ~nil because `serve_zip`→`_zip_member`→`_diavgeia_pdf` falls back to
Diavgeia and every row always has an `ada`. Fix: `R2Storage.enforce_pdf_cap` now
reconciles — when a run claims `has_pdfs` but has 0 bytes, it rewrites `has_pdfs=False`
and skips it (self-heals on the next cap pass, which runs after every search).
`sizes_by_run`/`list_runs` stay pure (hot paths); `LocalStorage` unchanged (can't reach
the state). Tests: reconcile-clears-flag, doesn't-evict-valid-newest, idempotent.

**P2-6 — `permit_kind` leading-prefix assumption.**
Investigation queried **16,823 real Diavgeia subjects** across 2019–2025: **0** carry a
leading classification code; codes like "6.4.6.1" appear only *inside* the description.
**Not a real bug.** A code-tolerant classifier produced **0 reclassifications**. Fix =
lock the assumption: an explicit early-`return None` exclusion guard for non-final acts
(`ΠΡΟΕΓΚΡΙΣΗ`, `ΑΝΑΘΕΩΡΗΣΗ`, `ΕΝΗΜΕΡΩΣΗ`, `ΕΓΚΡΙΣΗ ΕΚΤΕΛΕΣΗΣ`) keyed on the subject head,
plus regression tests derived from real subjects (dotted codes in descriptions classify
correctly; excluded kinds stay excluded). **Behavior is byte-identical on current data** —
those kinds were already dropped by the positive `startswith` checks not matching.
```python
head = subj.split(":", 1)[0]
for excluded in ("ΠΡΟΕΓΚΡΙΣΗ", "ΑΝΑΘΕΩΡΗΣΗ", "ΕΝΗΜΕΡΩΣΗ", "ΕΓΚΡΙΣΗ ΕΚΤΕΛΕΣΗΣ"):
    if head.startswith(excluded):
        return None
```

**dimos_query dead-data cleanup.**
`dimos_distance_km` (removed earlier) was the only reader of `row["dimos_query"]` and of
the `"geocode"` field that `areas.municipality_labels` produced. Confirmed dead repo-wide
(front-end reads only `r.dimos`; `output.write_xlsx` uses the fixed `COLUMNS` list with
`row.get`, so old `rows.json` with the field still load fine). Removed: the write in
`run_pipeline`, the `"geocode"` computation in `municipality_labels` (now returns
`{"display": ...}`), and the two test assertions / fixture fields. Backward-compatible
with history runs; no migration.

## 3. Open items / levers deliberately NOT pursued (weigh these)

- **OOM (P2-4):** root cause inferred not reproduced; `openpyxl write_only` and
  `serve_zip` rows.json streaming were left undone (see C). A real reproduction or a
  memory budget in CI would be the next step.
- **Within-region mis-tags** (< 250 km) are not flagged by the out-of-region check.
- **`permit_kind` methodology** depends on Diavgeia never prefixing subjects — true
  today across 16,823 samples; a periodic live re-check is advisable but not automated.
- **`api_status` reads the global `job` without the lock** (benign per the prior review,
  since workers act on their own `Job`); confirm you agree it's safe.
- **`pe_centroids.json` accuracy** for the tightest islands (Αντικύθηρα ~59 km margin).

## 4. File-by-file change map

- `demolitions/geocode.py` — removed `dimos_distance_km`, `rows_centroid_bbox`,
  `area_flag`, `_median`; added `PE_CENTROIDS`/`load_pe_centroids`, `PE_DISTANCE_KM`,
  `row_out_of_region`, `_haversine`. `_HIGH_PREC` retained.
- `demolitions/pipeline.py` — `_flag_out_of_region(rows)` simplified to the per-row pass
  (removed `_single_region` + bbox branch); `n_dups` counts only "πιθανό διπλό"; removed
  `row["dimos_query"]`; PDF size estimate constant 300→3000.
- `demolitions/areas.py` — `municipality_labels` drops the `"geocode"` field.
- `demolitions/diavgeia.py` — `permit_kind` exclusion guard.
- `demolitions/pdfparse.py` — `download_pdf` streams to disk.
- `demolitions/storage.py` — `R2Storage.enforce_pdf_cap` reconciles stale `has_pdfs`.
- `Dockerfile` — `MALLOC_ARENA_MAX=2`; `--threads 8→4`.
- `demolitions/data/pe_centroids.json` — NEW (75 PE centroids).
- `docs/homonym-out-of-region-issue.md` — NEW (feature A spec).
- `tests.py` — +~40 tests / fixture updates across all of the above; removed the
  obsolete bbox tests; 100 total, all green.

## 5. Suggested focus for this review round

1. **Re-derive P2-4.** Is the concurrency/arena hypothesis sound, or is there a real
   memory leak/peak we missed (e.g. the upload `ThreadPoolExecutor` futures list, R2
   `_resilient_body`, openpyxl, or disk vs RAM)? Is `--threads 4` the right call?
2. **Out-of-region precision/recall.** Stress `row_out_of_region` against the bundled
   centroids for every PE; find any legitimate point > 250 km from its PE centroid (false
   positive) and any plausible mis-tag < 250 km (false negative). Sanity-check
   `pe_centroids.json` values.
3. **`enforce_pdf_cap` reconcile** — any path where it could wrongly clear `has_pdfs` for
   a run whose PDFs are merely mid-upload? (The single-job model should preclude it —
   confirm.)
4. **General second look** at concurrency (global `job`/`lock`/`rate_hits`), the zip
   streaming + Diavgeia fallback, and security of the public endpoints — independent of
   the prior review's conclusions.
