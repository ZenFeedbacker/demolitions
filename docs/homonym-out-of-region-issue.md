# Issue: Crete (Ηράκλειο) permits leak into a Περιφέρεια Αττικής search

**Status:** closed — implemented (commit `b390831`).

## Resolution

**Approach chosen:** bundled PE centroids + per-row haversine check, code-anchored.

A JSON gazetteer of 75 Περιφερειακή Ενότητα centroids is bundled at
`demolitions/data/pe_centroids.json`, keyed by the 2-digit PE prefix of
`muni_code`. For every row whose `precision` is one of `{"κτίσμα (PDF)",
"οδός+αριθμός", "οδός", "οικισμός"}`, `row_out_of_region(row)` (in
`demolitions/geocode.py`) computes the haversine distance from the row's
coordinates to the centroid of its **declared** PE and flags any row that is
**> 250 km** away. Because the anchor is looked up by PE *code* (not the
ambiguous municipality name), the check is immune to Nominatim homonym
ambiguity and is independent of the contamination fraction — each row is
judged individually.

All acceptance criteria from §8 are met:
- All 25 Crete permits in a Αττική result set are flagged regardless of
  contamination fraction (including the 29–56% cases where the old IQR bbox
  flagged 0/25).
- The detector generalises to any region/homonym; no Crete-specific code.
- No false positives on clean single-region, whole-country, or border searches.
- Covered by offline unit tests; `python3 tests.py` green; `pyflakes` clean.

**Remaining small gap:** rows with `precision="δήμος"` (~2%) are skipped —
their coordinates are the municipality centroid rather than the actual building
location, so a distance check would produce false positives.

**Owner of fix:** specialized coding agent.

## 1. Summary

When a user searches **Περιφέρεια Αττικής**, a number of demolition permits that
are physically located in **Heraklion, Crete** appear in the results. The most
recent run showed **~25** such entries that are **not** removed even when the user
applies the existing «Χωρίς ύποπτη θέση (π.χ. ομώνυμος δήμος)» location filter.

The fix must be **general** — it must detect *any* permit that is geographically
outside the searched area, not just Crete and not just the Ηράκλειο case.

## 2. Why this happens (the homonym)

There are two municipalities in Greece both officially named **«ΔΗΜΟΣ ΗΡΑΚΛΕΙΟΥ»**:

- **Code `7101`** — Ηράκλειο **Κρήτης** (PE prefix `71`, Περιφέρεια Κρήτης).
- **Code `4604`** — Ηράκλειο / *Νέο Ηράκλειο* **Αττικής** (PE prefix `46`,
  ΒΟΡΕΙΟΥ ΤΟΜΕΑ ΑΘΗΝΩΝ, Περιφέρεια Αττικής).

In the e-Άδειες (TEE) submission dropdown both appear with the **identical string**
«Δήμος Ηρακλείου». Submitters for Crete buildings frequently pick the wrong entry
(`4604`, the Attica one). Diavgeia therefore tags a Crete permit with municipality
code `4604`. A «Περιφέρεια Αττικής» search includes `4604`, so the Crete permit is
returned. `resolve_area` itself is correct — the data is mis-tagged at the source.

## 3. Evidence — a concrete example

Example permit (given by the user): **ΑΔΑ `980Ζ46Ψ842-Ο4Δ`**
(https://diavgeia.gov.gr/doc/980Ζ46Ψ842-Ο4Δ?inline=true).

Parsed via the real pipeline parser (`pdftotext -layout` → `parse_fields` /
`extract_polygon`):

```
odos:        'ΛΕΩΦΟΡΟΣ ΙΚΑΡΟΥ'
ar_apo:      '148'
poli:        'ΗΡΑΚΛΕΙΟ'
dimos_pdf:   'Ηρακλείου'
dim_enotita: 'ΠΟΡΟΣ ΗΡΑΚΛΕΙΟΥ'        ← Πόρος Ηρακλείου = Crete locality
ot:          '2587'
polygon:     4 vertices, centroid lat=35.3400 lon=25.1504   ← CRETE
```

So the building has a **valid ΕΓΣΑ87 coordinate polygon** whose WGS84 centroid is in
Crete (`35.34, 25.15`). Its row therefore has `precision = "κτίσμα (PDF)"` (high
precision) and a real Crete `lat`/`lon`. **The current detector *should* catch it —
but doesn't, for the reason in §5.**

The authoritative selected municipality is available on every row as
`row["muni_code"]` (here `"4604"`, Attica) — set in `pipeline.run_pipeline`.

## 4. What has already been tried (history — do not repeat)

1. **`Geocoder.dimos_distance_km` (Nominatim per δήμος) — REMOVED.**
   Computed the distance from each permit's coordinates to the centroid of its
   *declared municipality name* via Nominatim, flagging `>60 km`.
   **Failed for homonyms:** querying `"Δήμος Ηρακλείου Αττικής, Ελλάδα"` returns the
   *Crete* city (more prominent in OSM), so a Crete permit measured ~0 km and was
   never flagged. It was also slow (one Nominatim call per municipality, ~1 req/s)
   and was removed in commit `4ffe87f`. The method still exists on `Geocoder` but is
   no longer called.

2. **Data-driven bounding box (`geocode.rows_centroid_bbox` + `area_flag`) — CURRENT,
   PARTIAL.** After geocoding, build a bbox from all high-precision points using a
   Tukey 1.5×IQR outlier filter, then flag any high-precision point outside the bbox
   as `"~Xkm εκτός περιοχής αναζήτησης"`. The UI filter «Χωρίς ύποπτη θέση» hides rows
   whose `flags` contain `"από τον δήμο"` or `"εκτός περιοχής"`. Network-free.
   See `demolitions/geocode.py` (`rows_centroid_bbox`, `area_flag`) and
   `demolitions/pipeline.py` (`enrich_geocode`, second pass).

## 5. Root cause of the current failure (verified)

The IQR bbox is computed from **all** points, *including* the mis-tagged ones. When
the mis-tagged cluster is a **large fraction** of the result set, the quartiles
themselves are contaminated: the lower quartile lands inside the Crete cluster, the
IQR blows up to several degrees, and the `1.5×IQR` fence expands to cover *both*
clusters — so nothing is flagged.

Reproduced against the actual code (`rows_centroid_bbox` + `area_flag`), 25 Crete
points + N Attica points, all `precision="κτίσμα (PDF)"`:

```
Αττική=1000 Κρήτη=25 ( 2.4%)  bbox_lat=(37.10,38.90)  Crete flagged: 25/25  ✓
Αττική= 200 Κρήτη=25 (11.1%)  bbox_lat=(37.10,38.90)  Crete flagged: 25/25  ✓
Αττική= 100 Κρήτη=25 (20.0%)  bbox_lat=(37.10,38.89)  Crete flagged: 25/25  ✓
Αττική=  60 Κρήτη=25 (29.4%)  bbox_lat=(34.74,38.90)  Crete flagged:  0/25  ✗
Αττική=  40 Κρήτη=25 (38.5%)  bbox_lat=(34.74,38.90)  Crete flagged:  0/25  ✗
Αττική=  20 Κρήτη=25 (55.6%)  bbox_lat=(34.74,38.85)  Crete flagged:  0/25  ✗
```

Breakdown begins around **>25%** contamination — exactly the regime of a narrower
Αττική search where the absolute Crete count (~25) is a big share of the total.

### Secondary gaps to handle too
- **Rows without PDF coordinates** (`precision == "δήμος"` or empty) are skipped by
  `area_flag`. A mis-tagged permit whose PDF lacks the «Συντεταγμένες» field, or
  whose `geocode_row` resolved only to the (wrong, Attica) municipality centroid,
  will sit *inside* the Attica region and never be flagged. Consider how to treat
  these (a per-row authoritative check, below, helps here too).
- **Whole-country / very large searches** (e.g. area «Ελλάδα», or a multi-region
  search) legitimately span the whole map. The detector must **not** produce false
  positives there — there is no single "region" to be outside of.

## 6. Constraints the fix must respect

- **Generalize** — no hard-coded Crete/Ηράκλειο logic. The mechanism must catch any
  permit located outside the searched area (any region, any homonym, any island).
- **Tests run fully offline** (`python3 tests.py`) — CI has no network and no warm
  cache. Any new data must be **bundled in the repo** (see `demolitions/data/`,
  e.g. the committed `kallikratis.json`) or be derivable offline. Do not add a test
  that requires hitting Diavgeia or Nominatim.
- **Nominatim politeness** — live lookups cost ~1 request/second and run serially.
  The geocode cache (`geocode.json`) is now **persisted to R2** across runs/spin-downs
  (`store.pull_cache`/`push_cache`), so a *bounded, one-time* set of lookups (e.g. a
  handful per Περιφερειακή Ενότητα) that warms the cache is acceptable; a per-row or
  per-municipality live lookup on every run is **not**.
- Keep the existing UI filter working; extend its trigger/strings if you add a new
  flag, and keep history runs (old flag strings) compatible.
- `pyflakes` clean; all existing tests stay green.

## 7. Suggested direction (the fixer decides & verifies)

**Preferred: a per-row, code-anchored geographic check** — robust to *any*
contamination fraction because each row is judged independently of the others.

Every row carries `row["muni_code"]` — the authoritative Kallikratis code the
submitter actually selected (`4604` for the example). The first two digits are the
**Περιφερειακή Ενότητα** prefix (`46`), and `areas.PREFIX_PE` maps every prefix to a
PE, with `muni_region` / `regions` giving the Περιφέρεια. The idea:

> Flag a row when its **PDF/high-precision coordinates** are far (e.g. `> ~120 km`,
> tune it) from the **expected location of its own declared PE/municipality**.

For the example: declared PE `46` (Β. Τομέας Αθηνών, Attica) but coordinates in
Crete → large distance → flagged. This is immune to how many such permits exist and
immune to the Nominatim homonym ambiguity, because the anchor is looked up **by code
/ by PE**, not by the ambiguous municipality *name*.

You need a source of PE (or municipality) reference coordinates. Options, pick what
is cleanest and offline-friendly:
- A small **bundled gazetteer** of the ~74 PE centroids (committed JSON in
  `demolitions/data/`), keyed by 2-digit prefix. Most robust; no network at all.
- Lazily geocode each **PE by name** once and rely on the persisted geocode cache
  (≤74 distinct, far less ambiguous than municipality names, e.g. «Περιφερειακή
  Ενότητα Ηρακλείου» / «ΒΟΡΕΙΟΥ ΤΟΜΕΑ ΑΘΗΝΩΝ»). Verify the names resolve sanely.
- Derive municipality centroids offline from a bundled dataset if you add one.

**Acceptable complement / fallback:** make `rows_centroid_bbox` robust to heavy
contamination (e.g. median + MAD instead of quartiles+IQR, which tolerates up to
~50% outliers), and/or disable the bbox flag entirely for whole-country searches.
The bbox can stay as a cheap second signal, but it should not be the *only* signal,
given §5.

The two approaches compose well: the code-anchored check is the reliable primary
signal; the (robustified) bbox catches anything the anchor misses.

## 8. Acceptance criteria

1. For a Περιφέρεια Αττικής result set containing 25 Crete permits, **all 25 are
   flagged** (and hidden by the «Χωρίς ύποπτη θέση» filter) **regardless of the total
   size** — specifically including the 29–56% contamination cases in §5 where the
   current code flags 0/25.
2. The detector is **generalized**: a unit test shows it also flags, say, a permit
   tagged to a mainland PE but located on a different island/region, with no
   Crete-specific code.
3. **No false positives** on:
   - a clean single-region search (all points genuinely in-area),
   - a whole-country / «Ελλάδα» search (nothing flagged as out-of-region),
   - permits legitimately near a region border (don't over-flag; tune the threshold).
4. New logic is covered by offline unit tests (no network). Reuse the real example’s
   numbers from §3/§5 where helpful. Add a regression test that encodes the §5
   contamination scenario.
5. `python3 tests.py` fully green; `python3 -m pyflakes demolitions/ webui.py tests.py`
   clean.

## 9. Key files & how to run

- `demolitions/geocode.py` — `rows_centroid_bbox`, `area_flag`, `Geocoder`,
  `_haversine`, `_HIGH_PREC`.
- `demolitions/pipeline.py` — `enrich_geocode` (second pass that applies the flag),
  `run_pipeline` (sets `row["muni_code"]`, `row["dimos"]`).
- `demolitions/areas.py` — `PREFIX_PE` (prefix→PE name), `load_kallikratis`
  (`regions`, `munis`, `muni_region`), `municipality_labels`.
- `demolitions/data/kallikratis.json` — bundled dictionary (example of committed data).
- `templates/index.html` — the «Χωρίς ύποπτη θέση» filter (visibility check + the
  `redraw()` predicate matching `"από τον δήμο"` / `"εκτός περιοχής"`).
- Tests: `python3 tests.py` (offline). Lint: `python3 -m pyflakes ...`.

A real Crete PDF for manual checking: ΑΔΑ `980Ζ46Ψ842-Ο4Δ` (network only; **not**
for CI tests).

## 10. Deliverable

Implement the fix + tests in the working tree, run the test suite and pyflakes, and
report a concise summary (root cause, approach chosen, what changed, test results).
**Do not commit or push** — the changes will be reviewed first.
