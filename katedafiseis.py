#!/usr/bin/env python3
"""Χαρτογράφηση αδειών κατεδάφισης από τη Διαύγεια.

Παράδειγμα:
    python3 katedafiseis.py --area "Δήμος Δράμας" --from 2021-01-01 --to 2021-12-31 -o drama.xlsx
"""

import argparse
import shutil
import sys
from datetime import date
from pathlib import Path

from katedafiseis.areas import normalize, resolve_area
from katedafiseis.diavgeia import search_permits, issue_date
from katedafiseis.geocode import Geocoder
from katedafiseis.output import write_xlsx
from katedafiseis.pdfparse import parse_decision

E_ADEIES_START = date(2018, 10, 1)


def parse_args():
    p = argparse.ArgumentParser(
        description="Άδειες κατεδάφισης από τη Διαύγεια (e-Άδειες/ΤΕΕ) σε spreadsheet.",
        epilog="Περιοχή: δήμος («Δήμος Δράμας»), νομός/ΠΕ («Νομός Καβάλας», "
               "«ΠΕ Καβάλας»), περιφέρεια («Περιφέρεια Κρήτης»), «Ελλάδα», "
               "ή πολλές χωρισμένες με κόμμα.",
    )
    p.add_argument("--area", required=True, help="περιοχή ενδιαφέροντος")
    p.add_argument("--from", dest="from_date", type=date.fromisoformat,
                   default=E_ADEIES_START,
                   help="από ημερομηνία (YYYY-MM-DD, default 2018-10-01)")
    p.add_argument("--to", dest="to_date", type=date.fromisoformat,
                   default=date.today(), help="έως ημερομηνία (default σήμερα)")
    p.add_argument("-o", "--output", default="katedafiseis",
                   help="φάκελος εξόδου του run (θα περιέχει το .xlsx και pdf/)")
    p.add_argument("--no-geocode", action="store_true",
                   help="χωρίς συντεταγμένες (πιο γρήγορο)")
    p.add_argument("--cache-dir", default=str(Path(__file__).parent / "cache"))
    return p.parse_args()


def main():
    args = parse_args()
    if args.from_date < E_ADEIES_START:
        print(f"Προσοχή: το e-Άδειες ξεκίνησε τον 10/2018· πριν από "
              f"{E_ADEIES_START} δεν υπάρχουν ομοιόμορφα δεδομένα.", file=sys.stderr)
        args.from_date = max(args.from_date, E_ADEIES_START)
    if args.from_date > args.to_date:
        sys.exit("Η αρχή του διαστήματος είναι μετά το τέλος του.")

    area_label, munis = resolve_area(args.area, args.cache_dir)
    print(f"Περιοχή: {area_label} ({len(munis)} δήμοι)")
    print(f"Διάστημα: {args.from_date} – {args.to_date}")

    print("Αναζήτηση στη Διαύγεια…")
    decisions = search_permits(args.from_date, args.to_date, munis, args.cache_dir)
    print(f"Σύνολο: {len(decisions)} άδειες κατεδάφισης")
    if not decisions:
        sys.exit("Καμία άδεια στο διάστημα/περιοχή.")

    print("Κατέβασμα και ανάλυση PDF…")
    rows = []
    seen_building = set()
    for i, d in enumerate(decisions, 1):
        row = parse_decision(d, args.cache_dir)
        dt = issue_date(d)
        row["date"] = dt
        row["year"] = dt.year
        row["dimos"] = munis[d["extraFieldValues"]["municipality"]]
        row["flags"] = ""
        # ίδιο κτίσμα με >1 τελικές άδειες (επανεκδόσεις) — συχνό φαινόμενο
        key = (d["extraFieldValues"]["municipality"],
               normalize(row["perigrafi"]), normalize(row["odos"]),
               row["ar_apo"])
        if row["perigrafi"] and key in seen_building:
            row["flags"] = "πιθανό διπλό"
        seen_building.add(key)
        rows.append(row)
        if i % 25 == 0 or i == len(decisions):
            ok = sum(1 for r in rows if r["parse_ok"])
            print(f"  {i}/{len(decisions)} (επιτυχής ανάλυση: {ok})")
    dups = sum(1 for r in rows if r["flags"])
    if dups:
        print(f"  Σημειώθηκαν {dups} πιθανά διπλά (ίδιος δήμος/διεύθυνση/περιγραφή).")

    # κάθε run = ένας φάκελος με το spreadsheet και υποφάκελο pdf/<δήμος>/<έτος>/
    out = Path(args.output)
    run_dir = out.with_suffix("") if out.suffix == ".xlsx" else out
    run_dir.mkdir(parents=True, exist_ok=True)
    pdf_root = run_dir / "pdf"
    copied = 0
    for row in rows:
        src = Path(args.cache_dir) / "pdf" / f"{row['ada']}.pdf"
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
    print(f"PDF: {pdf_root}/ (αντιγράφηκαν {copied})")

    if not args.no_geocode:
        print("Γεωκωδικοποίηση (Nominatim, ~1 αίτημα/δευτ. όταν δεν υπάρχει cache)…")
        geocoder = Geocoder(args.cache_dir)
        try:
            for i, row in enumerate(rows, 1):
                row["lat"], row["lon"], row["precision"] = \
                    geocoder.geocode_row(row, row["dimos"])
                if row["precision"] in ("οδός+αριθμός", "οδός", "οικισμός"):
                    dist = geocoder.dimos_distance_km(row, row["dimos"])
                    if dist and dist > 60:
                        row["flags"] = (row["flags"] + "; " if row["flags"]
                                        else "") + f"~{dist:.0f}km από τον δήμο"
                if i % 25 == 0 or i == len(rows):
                    hit = sum(1 for r in rows[:i] if r.get("lat"))
                    print(f"  {i}/{len(rows)} (με συντεταγμένες: {hit})")
        finally:
            geocoder.close()

    xlsx_path = run_dir / (run_dir.name + ".xlsx")
    write_xlsx(rows, xlsx_path)
    print(f"Γράφτηκε: {xlsx_path} ({len(rows)} γραμμές)")


if __name__ == "__main__":
    main()
