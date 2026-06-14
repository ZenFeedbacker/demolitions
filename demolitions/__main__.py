#!/usr/bin/env python3
"""Χαρτογράφηση αδειών κατεδάφισης από τη Διαύγεια.

Παράδειγμα:
    python3 -m demolitions --area "Δήμος Δράμας" --from 01/01/2021 --to 31/12/2021 -o drama
"""

import argparse
import sys
from datetime import date, datetime
from pathlib import Path

from demolitions.areas import AreaError
from demolitions.pipeline import (E_ADEIES_START, NoPermitsFound,
                                  enrich_geocode, run_pipeline)


def parse_date(s):
    for fmt in ("%d/%m/%Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            pass
    raise argparse.ArgumentTypeError(
        f"μη έγκυρη ημερομηνία «{s}» (δεκτά: ΗΗ/ΜΜ/ΕΕΕΕ ή ΕΕΕΕ-ΜΜ-ΗΗ)")


def parse_args():
    p = argparse.ArgumentParser(
        description="Άδειες κατεδάφισης από τη Διαύγεια (e-Άδειες/ΤΕΕ) σε spreadsheet.",
        epilog="Περιοχή: δήμος («Δήμος Δράμας»), νομός/ΠΕ («Νομός Καβάλας», "
               "«ΠΕ Καβάλας»), περιφέρεια («Περιφέρεια Κρήτης»), «Ελλάδα», "
               "ή πολλές χωρισμένες με κόμμα.",
    )
    p.add_argument("--area", required=True, help="περιοχή ενδιαφέροντος")
    p.add_argument("--from", dest="from_date", type=parse_date,
                   default=E_ADEIES_START,
                   help="από ημερομηνία (ΗΗ/ΜΜ/ΕΕΕΕ, default 01/10/2018)")
    p.add_argument("--to", dest="to_date", type=parse_date,
                   default=date.today(), help="έως ημερομηνία (default σήμερα)")
    p.add_argument("-o", "--output", default="demolitions",
                   help="φάκελος εξόδου του run (θα περιέχει το .xlsx και pdf/)")
    p.add_argument("--no-geocode", action="store_true",
                   help="χωρίς συντεταγμένες (πιο γρήγορο)")
    p.add_argument("--cache-dir",
                   default=str(Path(__file__).parent.parent / "cache"))
    return p.parse_args()


def main():
    args = parse_args()
    if args.from_date > args.to_date:
        sys.exit("Η αρχή του διαστήματος είναι μετά το τέλος του.")
    try:
        result = run_pipeline(args.area, args.from_date, args.to_date,
                              args.output, cache_dir=args.cache_dir)
        if not args.no_geocode:
            enrich_geocode(result.run_dir, cache_dir=args.cache_dir)
    except (AreaError, NoPermitsFound) as e:
        sys.exit(str(e))


if __name__ == "__main__":
    main()
