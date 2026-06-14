"""Η ροή ενός run, επαναχρησιμοποιήσιμη από CLI και web UI.

Δύο φάσεις: `run_pipeline` (αναζήτηση -> PDF -> xlsx, χωρίς συντεταγμένες)
και `enrich_geocode` (προσθέτει συντεταγμένες σε υπάρχον run και ξαναγράφει
το xlsx). Έτσι το spreadsheet είναι διαθέσιμο αμέσως και η αργή
γεωκωδικοποίηση (~1 αίτημα/δευτ.) τρέχει ως εμπλουτισμός.

Callbacks: `log(msg)` για κείμενο προόδου, `step(phase, i, n)` για μπάρα
προόδου (phases: search/pdf/geocode), `cancel()` -> bool για ακύρωση.
"""

import json
import shutil
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from .areas import municipality_labels, normalize, resolve_area
from .diavgeia import KIND_KATEDAFISI, issue_date, permit_kind, search_permits
from .geocode import Geocoder
from .greek import pretty_area
from .output import write_xlsx
from .pdfparse import parse_decision

E_ADEIES_START = date(2018, 10, 1)


class CancelledRun(Exception):
    """Ο χρήστης ακύρωσε το run."""


class NoPermitsFound(Exception):
    """Καμία άδεια στο διάστημα/περιοχή."""


@dataclass
class RunResult:
    run_dir: Path
    xlsx_path: Path
    rows: list
    n_dups: int


def _check(cancel):
    if cancel and cancel():
        raise CancelledRun()


def _write_run_files(run_dir, rows, manifest):
    write_xlsx(rows, run_dir / (run_dir.name + ".xlsx"))
    (run_dir / "rows.json").write_text(
        json.dumps(rows, ensure_ascii=False), encoding="utf-8")
    (run_dir / "run.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=1), encoding="utf-8")


def run_pipeline(area, from_date, to_date, out_dir, *, cache_dir,
                 log=print, step=None, cancel=None):
    """Αναζήτηση + PDF + xlsx (χωρίς συντεταγμένες). Επιστρέφει RunResult."""
    if from_date < E_ADEIES_START:
        log(f"Προσοχή: το e-Άδειες ξεκίνησε τον 10/2018· πριν από "
            f"{E_ADEIES_START:%d/%m/%Y} δεν υπάρχουν ομοιόμορφα δεδομένα.")
        from_date = E_ADEIES_START

    area_label, munis = resolve_area(area, cache_dir)
    muni_labels = municipality_labels(munis, cache_dir)
    area_label = pretty_area(area_label)
    log(f"Περιοχή: {area_label} ({len(munis)} δήμοι)")
    log(f"Διάστημα: {from_date:%d/%m/%Y} – {to_date:%d/%m/%Y}")

    def search_progress(msg):
        _check(cancel)
        log(msg)

    log("Αναζήτηση στη Διαύγεια…")
    if step:
        step("search", 0, 0)
    decisions = search_permits(from_date, to_date, munis, cache_dir,
                               progress=search_progress)
    log(f"Σύνολο: {len(decisions)} άδειες κατεδάφισης")
    if not decisions:
        raise NoPermitsFound("Καμία άδεια στο διάστημα/περιοχή.")

    log("Κατέβασμα και ανάλυση PDF…")
    rows = []
    seen_building = set()
    for i, d in enumerate(decisions, 1):
        _check(cancel)
        if step:
            step("pdf", i, len(decisions))
        row = parse_decision(d, cache_dir)
        dt = issue_date(d)
        muni_code = d["extraFieldValues"]["municipality"]
        row["date"] = dt.isoformat()
        row["year"] = dt.year
        row["muni_code"] = muni_code
        row["dimos"] = muni_labels[muni_code]["display"]
        row["dimos_query"] = muni_labels[muni_code]["geocode"]
        row["eidos"] = permit_kind(d.get("subject", "")) or KIND_KATEDAFISI
        row["flags"] = ""
        # ίδιο κτίσμα με >1 τελικές άδειες (επανεκδόσεις) — συχνό φαινόμενο
        key = (muni_code,
               normalize(row["perigrafi"]), normalize(row["odos"]),
               row["ar_apo"])
        if row["perigrafi"] and key in seen_building:
            row["flags"] = "πιθανό διπλό"
        seen_building.add(key)
        rows.append(row)
        if i % 25 == 0 or i == len(decisions):
            ok = sum(1 for r in rows if r["parse_ok"])
            log(f"  {i}/{len(decisions)} (επιτυχής ανάλυση: {ok})")
    n_dups = sum(1 for r in rows if r["flags"])
    if n_dups:
        log(f"  Σημειώθηκαν {n_dups} πιθανά διπλά (ίδιος δήμος/διεύθυνση/περιγραφή).")

    # κάθε run = ένας φάκελος με το spreadsheet και υποφάκελο pdf/<δήμος>/<έτος>/
    out = Path(out_dir)
    run_dir = out.with_suffix("") if out.suffix == ".xlsx" else out
    run_dir.mkdir(parents=True, exist_ok=True)
    pdf_root = run_dir / "pdf"
    copied = 0
    for row in rows:
        _check(cancel)
        src = Path(cache_dir) / "pdf" / f"{row['ada']}.pdf"
        if not src.exists():
            row["pdf_path"] = ""
            continue
        dest_dir = pdf_root / row["dimos"] / str(row["year"])
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest = dest_dir / f"{row['ada']}.pdf"
        if not dest.exists():
            shutil.copy2(src, dest)
            copied += 1
        row["pdf_path"] = str(dest.relative_to(run_dir))
    log(f"PDF: {pdf_root}/ (αντιγράφηκαν {copied})")

    manifest = {
        "run_id": run_dir.name,
        "area": area_label,
        "area_query": area,
        "from": from_date.isoformat(),
        "to": to_date.isoformat(),
        "created": date.today().isoformat(),
        "n_rows": len(rows),
        "n_dups": n_dups,
        "geocoded": False,
        "has_pdfs": any(r["pdf_path"] for r in rows),
    }
    _write_run_files(run_dir, rows, manifest)
    xlsx_path = run_dir / (run_dir.name + ".xlsx")
    log(f"Γράφτηκε: {xlsx_path} ({len(rows)} γραμμές)")
    return RunResult(run_dir, xlsx_path, rows, n_dups)


def enrich_geocode(run_dir, *, cache_dir, log=print, step=None, cancel=None):
    """Συντεταγμένες σε υπάρχον run· ξαναγράφει xlsx/rows.json/run.json.

    Σε ακύρωση στη μέση, τα μερικά αποτελέσματα σώζονται και το run μένει
    geocoded=False ώστε να μπορεί να συνεχιστεί (η cache κάνει τα ήδη
    γεωκωδικοποιημένα σχεδόν ακαριαία).
    """
    run_dir = Path(run_dir)
    rows = json.loads((run_dir / "rows.json").read_text(encoding="utf-8"))
    manifest = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))

    log("Γεωκωδικοποίηση (Nominatim, ~1 αίτημα/δευτ. όταν δεν υπάρχει cache)…")
    geocoder = Geocoder(cache_dir)
    completed = False
    try:
        for i, row in enumerate(rows, 1):
            _check(cancel)
            if step:
                step("geocode", i, len(rows))
            if row.get("lat") is None:
                row["lat"], row["lon"], row["precision"] = \
                    geocoder.geocode_row(row, row["dimos"])
            # σημείο μακριά από τον δήμο των μεταδεδομένων = ύποπτο
            # (λάθος δήλωση δήμου στο e-Άδειες) — και για συντεταγμένες PDF
            if row.get("lat") is not None and row["precision"] in (
                    "κτίσμα (PDF)", "οδός+αριθμός", "οδός", "οικισμός"):
                dist = geocoder.dimos_distance_km(row, row["dimos"])
                if dist and dist > 60:
                    flag = f"~{dist:.0f}km από τον δήμο"
                    if flag not in row["flags"]:
                        row["flags"] = (row["flags"] + "; " if row["flags"]
                                        else "") + flag
            if i % 25 == 0 or i == len(rows):
                hit = sum(1 for r in rows[:i] if r.get("lat"))
                log(f"  {i}/{len(rows)} (με συντεταγμένες: {hit})")
        completed = True
    finally:
        geocoder.close()
        manifest["geocoded"] = completed
        _write_run_files(run_dir, rows, manifest)
    log(f"Γράφτηκε: {run_dir / (run_dir.name + '.xlsx')} (με συντεταγμένες)")
