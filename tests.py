#!/usr/bin/env python3
"""Δοκιμές χωρίς δίκτυο: python3 tests.py

Χρειάζονται μόνο το cache/kallikratis.json (υπάρχει μετά το πρώτο run).
"""

import io
import json
import os
import tempfile
import unittest
import zipfile
from datetime import datetime, timezone
from pathlib import Path

CACHE = str(Path(__file__).parent / "cache")

from demolitions.areas import (AreaError, list_areas, municipality_labels,
                               normalize, resolve_area)
from demolitions.diavgeia import (KIND_KATEDAFISI, KIND_OIKODOMIKI,
                                   _search_query, issue_date, permit_kind)
from demolitions.egsa87 import egsa87_to_wgs84
from demolitions.geocode import _poli_variants, _strip_dimos
from demolitions.greek import dimos_display, greek_title, pretty_area
from demolitions.output import COLUMNS, write_xlsx
from demolitions.pdfparse import (_clean, _pdf_url, detect_extent, detect_floors,
                                   extract_polygon, is_nonbuilding, parse_fields)


class TestNormalize(unittest.TestCase):
    def test_accents_case_hyphens(self):
        self.assertEqual(normalize("Δήμος Δράμας"), "ΔΗΜΟΣ ΔΡΑΜΑΣ")
        self.assertEqual(normalize("ΚΕΑΣ - ΚΥΘΝΟΥ"), "ΚΕΑΣ ΚΥΘΝΟΥ")
        self.assertEqual(normalize("  πολλά   κενά "), "ΠΟΛΛΑ ΚΕΝΑ")
        self.assertEqual(normalize("Αχαΐας"), "ΑΧΑΙΑΣ")


class TestResolveArea(unittest.TestCase):
    def test_dimos(self):
        label, munis = resolve_area("Δήμος Δράμας", CACHE)
        self.assertEqual(label, "ΔΗΜΟΣ ΔΡΑΜΑΣ")
        self.assertEqual(list(munis), ["0201"])

    def test_nomos_vs_pe(self):
        _, nomos = resolve_area("Νομός Καβάλας", CACHE)
        _, pe = resolve_area("ΠΕ Καβάλας", CACHE)
        # ο νομός περιλαμβάνει και τη Θάσο (πρόθεμα 04), η ΠΕ όχι
        self.assertTrue(set(pe) < set(nomos))
        self.assertTrue(any(c.startswith("04") for c in nomos))
        self.assertFalse(any(c.startswith("04") for c in pe))

    def test_perifereia_kai_ellada(self):
        _, kriti = resolve_area("Περιφέρεια Κρήτης", CACHE)
        self.assertEqual(len(kriti), 24)
        _, ellada = resolve_area("Ελλάδα", CACHE)
        self.assertEqual(len(ellada), 326)

    def test_polles_perioxes(self):
        label, munis = resolve_area("Δήμος Δράμας, Δήμος Θάσου", CACHE)
        self.assertEqual(len(munis), 2)
        self.assertIn(",", label)

    def test_amfisimi_kai_agnosti(self):
        # μοναδικό διπλώνυμο ζευγάρι δήμων στην Ελλάδα
        with self.assertRaises(AreaError):
            resolve_area("Δήμος Ηρακλείου", CACHE)
        with self.assertRaises(AreaError):
            resolve_area("Δήμος Ασγκαμπάτ", CACHE)
        with self.assertRaises(AreaError):
            resolve_area("  ", CACHE)

    def test_omonimoi_dimoi_me_prosdiorismo(self):
        _, kriti = resolve_area("Δήμος Ηρακλείου (Κρήτης)", CACHE)
        _, attiki = resolve_area("Δήμος Ηρακλείου (Αττικής)", CACHE)
        self.assertEqual(list(kriti)[0][:2], "71")
        self.assertEqual(list(attiki)[0][:2], "46")

    def test_ola_ta_autocomplete_labels_epiluontai(self):
        """Κάθε επιλογή του autocomplete πρέπει να γίνεται δεκτή."""
        for a in list_areas(CACHE):
            label, munis = resolve_area(a["label"], CACHE)
            self.assertTrue(munis, f"κενή επίλυση: {a['label']}")

    def test_omonimoi_dimoi_exoun_diaforetikes_etiketes(self):
        labels = municipality_labels(("4604", "7101"), CACHE)
        self.assertEqual(labels["4604"]["display"], "Δήμος Ηρακλείου (Αττικής)")
        self.assertEqual(labels["7101"]["display"], "Δήμος Ηρακλείου (Κρήτης)")
        self.assertEqual(labels["4604"]["geocode"], "Δήμος Ηρακλείου Αττικής")
        self.assertEqual(labels["7101"]["geocode"], "Δήμος Ηρακλείου Κρήτης")


class TestGreek(unittest.TestCase):
    def test_lexiko(self):
        self.assertEqual(greek_title("ΝΕΑ ΑΜΙΣΟΣ"), "Νέα Αμισός")
        self.assertEqual(greek_title("28ΗΣ ΟΚΤΩΒΡΙΟΥ"), "28ης Οκτωβρίου")
        self.assertEqual(greek_title("ΕΘΝΙΚΗΣ ΑΜΥΝΗΣ"), "Εθνικής Αμύνης")

    def test_agnostes_lexeis_xoris_tono(self):
        self.assertEqual(greek_title("ΧΩΡΙΣΤΗ"), "Χωριστή")  # στο λεξικό
        self.assertEqual(greek_title("ΑΓΝΩΣΤΟΧΩΡΙ"), "Αγνωστοχωρι")

    def test_teliko_sigma_kai_latinika(self):
        self.assertEqual(greek_title("ΛΙΜΕΝΑΣ ΘΑΣΟΥ"), "Λιμένας Θάσου")
        self.assertEqual(greek_title("OK 12"), "Ok 12")
        self.assertEqual(greek_title(""), "")

    def test_idempotent(self):
        for s in ("Νέα Αμισός", "Δήμος Δράμας", "Υψηλάντου 12"):
            self.assertEqual(greek_title(s), s)

    def test_dimoi(self):
        self.assertEqual(dimos_display("ΔΗΜΟΣ ΔΡΑΜΑΣ"), "Δήμος Δράμας")
        self.assertEqual(dimos_display("ΔΗΜΟΣ ΑΓΙΟΥ ΔΗΜΗΤΡΙΟΥ"),
                         "Δήμος Αγίου Δημητρίου")

    def test_pretty_area(self):
        self.assertEqual(pretty_area("ΔΗΜΟΣ ΔΡΑΜΑΣ, ΠΕΡΙΦΕΡΕΙΑ ΚΡΗΤΗΣ"),
                         "Δήμος Δράμας, Περιφέρεια Κρήτης")


class TestDetectFloors(unittest.TestCase):
    CASES = [
        ("ΚΑΤΕΔΑΦΙΣΗ ΔΙΩΡΟΦΗΣ ΚΑΤΟΙΚΙΑΣ", 2),
        ("ΚΑΤΕΔΑΦΙΣΗ ΙΣΟΓΕΙΑΣ ΑΠΟΘΗΚΗΣ", 1),
        ("ΚΑΤΕΔΑΦΙΣΗ ΤΡΙΟΡΩΦΟΥ ΚΤΙΡΙΟΥ", 3),       # ορθογραφικό
        ("ΚΑΤΕΔΑΦΙΣΗ ΚΑΤΟΙΚΙΑΣ 2 ΟΡΟΦΩΝ", 2),
        ("ΚΑΤΕΔΑΦΙΣΗ ΚΤΙΡΙΟΥ ΤΡΙΩΝ ΟΡΟΦΩΝ", 3),
        ("ΙΣΟΓΕΙΑ ΚΑΤΟΙΚΙΑ ΚΑΙ ΔΙΩΡΟΦΗ ΑΠΟΘΗΚΗ", 2),  # max
        ("ΚΑΤΕΔΑΦΙΣΗ ΣΤΟ ΟΤ 15", None),               # όχι αριθμός ΟΤ
        ("ΚΑΤΕΔΑΦΙΣΗ ΚΑΤΟΙΚΙΑΣ", None),
    ]

    def test_cases(self):
        for desc, want in self.CASES:
            self.assertEqual(detect_floors(normalize(desc)), want, desc)


class TestNonBuilding(unittest.TestCase):
    def test_walls_fences_etc(self):
        for desc in ("ΚΑΤΕΔΑΦΙΣΗ ΤΟΙΧΙΟΥ", "ΚΑΤΕΔΑΦΙΣΗ ΜΑΝΤΡΟΤΟΙΧΟΥ",
                     "ΚΑΤΕΔΑΦΙΣΗ ΠΕΡΙΦΡΑΞΗΣ ΟΙΚΟΠΕΔΟΥ",
                     "ΚΑΤΕΔΑΦΙΣΗ ΚΟΛΥΜΒΗΤΙΚΗΣ ΔΕΞΑΜΕΝΗΣ"):
            self.assertTrue(is_nonbuilding(normalize(desc)), desc)

    def test_buildings_not_flagged(self):
        for desc in ("ΚΑΤΕΔΑΦΙΣΗ ΔΙΩΡΟΦΗΣ ΚΑΤΟΙΚΙΑΣ",
                     "ΚΑΤΕΔΑΦΙΣΗ ΙΣΟΓΕΙΑΣ ΑΠΟΘΗΚΗΣ",
                     "ΚΑΤΕΔΑΦΙΣΗ ΚΑΤΟΙΚΙΑΣ ΚΑΙ ΠΕΡΙΦΡΑΞΗΣ",   # έχει κτίριο
                     "ΚΑΤΕΔΑΦΙΣΗ ΚΤΙΡΙΟΥ"):
            self.assertFalse(is_nonbuilding(normalize(desc)), desc)


class TestDetectExtent(unittest.TestCase):
    def test_cases(self):
        for desc, want in [
            ("ΚΑΤΕΔΑΦΙΣΗ ΔΙΩΡΟΦΗΣ ΚΑΤΟΙΚΙΑΣ", "ολική"),
            ("ΚΑΤΕΔΑΦΙΣΗ ΤΜΗΜΑΤΟΣ ΚΤΙΡΙΟΥ", "τμηματική/μερική"),
            ("ΜΕΡΙΚΗ ΚΑΤΕΔΑΦΙΣΗ ΙΣΟΓΕΙΟΥ", "τμηματική/μερική"),
            ("ΤΜΗΜΑΤΙΚΗ ΚΑΤΕΔΑΦΙΣΗ ΑΠΟΘΗΚΗΣ", "τμηματική/μερική"),
        ]:
            self.assertEqual(detect_extent(normalize(desc)), want, desc)


class TestParseFields(unittest.TestCase):
    # απόσπασμα όπως το βγάζει το pdftotext -layout από τη φόρμα e-Άδειες
    SAMPLE = """\
Στοιχεία διεύθυνσης
  Οδός                 ΥΨΗΛΑΝΤΟΥ
  Αρ. από              12
  Πόλη/Οικισμός        ΔΡΑΜΑ
  Δήμος                ΔΡΑΜΑΣ
  Δημοτική Ενότητα /   ΝΕΑ ΑΜΙΣΟΣ
  ΟΤ                   45
  ΚΑΕΚ                 -
  Περιγραφή έργου      ΚΑΤΕΔΑΦΙΣΗ ΔΙΩΡΟΦΗΣ ΚΑΤΟΙΚΙΑΣ
\fΔεύτερη σελίδα
  Οδός                 ΑΛΛΗ ΟΔΟΣ ΠΟΥ ΔΕΝ ΜΕΤΡΑΕΙ
"""

    def test_fields(self):
        f = parse_fields(self.SAMPLE)
        self.assertEqual(f["odos"], "ΥΨΗΛΑΝΤΟΥ")
        self.assertEqual(f["ar_apo"], "12")
        self.assertEqual(f["poli"], "ΔΡΑΜΑ")
        self.assertEqual(f["dim_enotita"], "ΝΕΑ ΑΜΙΣΟΣ")
        self.assertEqual(f["kaek"], "")          # παύλα -> κενό
        self.assertEqual(f["perigrafi"], "ΚΑΤΕΔΑΦΙΣΗ ΔΙΩΡΟΦΗΣ ΚΑΤΟΙΚΙΑΣ")

    def test_proti_selida_mono(self):
        f = parse_fields(self.SAMPLE)
        self.assertNotIn("ΑΛΛΗ", f["odos"])

    def test_clean(self):
        self.assertEqual(_clean(" - "), "")
        self.assertEqual(_clean("τιμή "), "τιμή")


class TestPermitKind(unittest.TestCase):
    def test_katedafisi(self):
        self.assertEqual(permit_kind(
            "Άδεια Κατεδάφισης (ν.4759/2020): ΚΑΤΕΔΑΦΙΣΗ ΔΙΩΡΟΦΗΣ"),
            KIND_KATEDAFISI)

    def test_oikodomiki_me_katedafisi(self):
        self.assertEqual(permit_kind(
            "Οικοδομική Άδεια (ν.4759/2020): ΑΔΕΙΑ ΚΑΤΕΔΑΦΙΣΗΣ & ΑΝΕΓΕΡΣΗ "
            "ΝΕΟΥ ΔΙΩΡΟΦΟΥ"), KIND_OIKODOMIKI)
        self.assertEqual(permit_kind(
            "Οικοδομική άδεια Κατηγορίας 1 χωρίς προέγκριση: Κατεδάφιση και "
            "ανέγερση"), KIND_OIKODOMIKI)
        self.assertEqual(permit_kind(
            "Οικοδομική Άδεια (ν.4759/2020): ΚΑΤΕΔΑΦΙΣΗ BARBECUE, ΑΝΕΓΕΡΣΗ ΝΕΑΣ "
            "ΙΣΟΓΕΙΟΥ ΟΙΚΟΔΟΜΗΣ"), KIND_OIKODOMIKI)

    def test_aporriptontai(self):
        for s in ("Προέγκριση Άδειας Κατεδάφισης: ...",
                  "Αναθεώρηση Άδειας Κατεδάφισης: ...",
                  "Ενημέρωση Οικοδομικής Άδειας: ΚΑΤΕΔΑΦΙΣΗ ...",
                  "Προέγκριση Οικοδομικής Άδειας: ΚΑΤΕΔΑΦΙΣΗ ...",
                  "Οικοδομική Άδεια (ν.4759/2020): ΑΝΕΓΕΡΣΗ ΚΑΤΟΙΚΙΑΣ",
                  "Οικοδομική Άδεια (ν.4759/2020): ΚΑΤΑΣΚΕΥΗ ΜΟΝΑΔΑΣ "
                  "ΔΙΑΧΕΙΡΙΣΗΣ ΑΠΟΒΛΗΤΩΝ ΕΚΣΚΑΦΩΝ, ΚΑΤΑΣΚΕΥΩΝ ΚΑΙ ΚΑΤΕΔΑΦΙΣΕΩΝ",
                  "Έγκριση Εκτέλεσης Εργασιών: ΚΑΤΕΔΑΦΙΣΗ ΕΠΙΚΙΝΔΥΝΟΥ"):
            self.assertIsNone(permit_kind(s), s)

    def test_search_query_is_broad_enough_for_bundled_permits(self):
        q = _search_query("2024-01-01", "2024-06-30")
        self.assertIn('subject:"Κατεδάφιση"', q)
        self.assertNotIn('subject:"Άδεια Κατεδάφισης"', q)


class TestIssueDate(unittest.TestCase):
    def test_issue_date_uses_greece_timezone(self):
        ts = int(datetime(2024, 4, 16, 21, 5, 11, tzinfo=timezone.utc).timestamp() * 1000)
        self.assertEqual(issue_date({"issueDate": ts}).isoformat(), "2024-04-17")

    def test_issue_date_midday_is_stable(self):
        ts = int(datetime(2024, 4, 16, 12, 0, 0, tzinfo=timezone.utc).timestamp() * 1000)
        self.assertEqual(issue_date({"issueDate": ts}).isoformat(), "2024-04-16")


class TestEgsa87(unittest.TestCase):
    def test_drama(self):
        lat, lon = egsa87_to_wgs84(512801.149, 4555388.78)
        self.assertAlmostEqual(lat, 41.152291, places=5)
        self.assertAlmostEqual(lon, 24.154344, places=5)

    def test_athina(self):
        lat, lon = egsa87_to_wgs84(476000, 4203000)   # κέντρο Αθήνας
        self.assertAlmostEqual(lat, 37.9769, places=3)
        self.assertAlmostEqual(lon, 23.7284, places=3)

    def test_ektos_orion(self):
        self.assertIsNone(egsa87_to_wgs84(0, 0))
        self.assertIsNone(egsa87_to_wgs84(512801, 9999999))


class TestExtractPolygon(unittest.TestCase):
    SAMPLE = """\
Στοιχεία
Συντεταγμένες   512801.1493522985 4555388.77952756,512817.2889679111
                4555393.674329015,512821.78689357365 4555375.153458641,512806.04415375483
                4555370.655532978




                                                              Σελίδα 3 από 4
"""

    def test_polygon(self):
        poly = extract_polygon(self.SAMPLE)
        self.assertEqual(len(poly), 4)
        for lat, lon in poly:
            self.assertAlmostEqual(lat, 41.152, places=2)
            self.assertAlmostEqual(lon, 24.154, places=2)

    def test_xoris_pedio(self):
        self.assertIsNone(extract_polygon("Άλλο κείμενο χωρίς συντεταγμένες"))

    def test_long_polygon(self):
        # 15 κορυφές, μία ανά γραμμή — δεν πρέπει να κόβονται (παλιό όριο: 7)
        pairs = [f"{512800 + i}.0 {4555300 + i}.0" for i in range(15)]
        text = ("Συντεταγμένες   " + pairs[0] + ",\n"
                + "".join(f"                {p},\n" for p in pairs[1:-1])
                + f"                {pairs[-1]}\n\n            Σελίδα 3 από 4\n")
        poly = extract_polygon(text)
        self.assertEqual(len(poly), 15)


class TestPdfText(unittest.TestCase):
    def test_pdf_url_ignores_untrusted_document_url(self):
        self.assertEqual(_pdf_url("ΑΔΑ123"), "https://diavgeia.gov.gr/doc/ΑΔΑ123")

    def test_missing_binary_returns_none(self):
        from unittest import mock
        from demolitions import pdfparse
        with mock.patch.object(pdfparse.subprocess, "run",
                               side_effect=FileNotFoundError):
            self.assertIsNone(pdfparse.pdf_text("/nonexistent.pdf"))

    def test_nonzero_exit_returns_none(self):
        from unittest import mock
        from demolitions import pdfparse
        fake = mock.Mock(returncode=1, stdout=b"")
        with mock.patch.object(pdfparse.subprocess, "run", return_value=fake):
            self.assertIsNone(pdfparse.pdf_text("/x.pdf"))


class TestGeocodeHelpers(unittest.TestCase):
    def test_strip_dimos(self):
        self.assertEqual(_strip_dimos("Δήμος Δράμας"), "Δράμας")
        self.assertEqual(_strip_dimos("ΔΗΜΟΣ ΔΡΑΜΑΣ"), "Δραμας")
        self.assertEqual(_strip_dimos("Δήμος Ηρακλείου (Αττικής)"), "Ηρακλείου")

    def test_poli_variants(self):
        self.assertEqual(_poli_variants("Οικισμός Ποταμιας Θάσου", "Θάσου"),
                         ["Ποταμιας", "Ποταμιας Θάσου", "Οικισμός Ποταμιας Θάσου"])
        self.assertEqual(_poli_variants("Δράμα/Προαστειο", "Δράμας"),
                         ["Δράμα/Προαστειο", "Δράμα", "Προαστειο"])
        self.assertEqual(_poli_variants("Δράμα", "Δράμας"), ["Δράμα"])
        self.assertEqual(_poli_variants("", "Δράμας"), [])

    def test_transient_failures_do_not_become_cached_misses(self):
        from unittest import mock
        from demolitions import geocode
        with tempfile.TemporaryDirectory() as tmp:
            g = geocode.Geocoder(tmp)
            with mock.patch.object(geocode.session, "get",
                                   side_effect=geocode.requests.RequestException), \
                 mock.patch.object(geocode.time, "sleep"):
                self.assertIsNone(g._query("Δράμα, Ελλάδα"))
                self.assertIsNone(g._query("Δράμα, Ελλάδα"))
            self.assertEqual(g.cache, {})

    def test_empty_hits_cache_are_reused(self):
        from unittest import mock
        from demolitions import geocode
        fake = mock.Mock(ok=True)
        fake.json.return_value = []
        with tempfile.TemporaryDirectory() as tmp:
            g = geocode.Geocoder(tmp)
            with mock.patch.object(geocode.session, "get", return_value=fake) as get, \
                 mock.patch.object(geocode.time, "sleep"):
                self.assertIsNone(g._query("Άγνωστο μέρος, Ελλάδα"))
                self.assertIsNone(g._query("Άγνωστο μέρος, Ελλάδα"))
            self.assertEqual(get.call_count, 1)
            self.assertEqual(g.cache["Άγνωστο μέρος, Ελλάδα"]["status"], "miss")


class TestOutput(unittest.TestCase):
    def test_xlsx(self):
        from openpyxl import load_workbook
        rows = [
            {"ada": "ΑΔΑ1", "url": "https://diavgeia.gov.gr/decision/view/ΑΔΑ1",
             "date": "2021-01-15", "year": 2021, "dimos": "Δήμος Δράμας",
             "dim_enotita": "", "poli": "Δράμα", "odos": "Υψηλάντου",
             "ar_apo": "12", "ar_eos": "", "ot": "", "kaek": "",
             "perigrafi": "ΚΑΤΕΔΑΦΙΣΗ", "orofoi": 2, "lat": 41.15, "lon": 24.14,
             "precision": "οδός", "parse_ok": True,
             "pdf_path": "pdf/Δήμος Δράμας/2021/ΑΔΑ1.pdf", "flags": ""},
            {"ada": "ΑΔΑ2", "url": "https://diavgeia.gov.gr/decision/view/ΑΔΑ2",
             "date": "2022-03-01", "year": 2022, "dimos": "Δήμος Θάσου",
             "dim_enotita": "", "poli": "", "odos": "", "ar_apo": "",
             "ar_eos": "", "ot": "", "kaek": "", "perigrafi": "",
             "orofoi": None, "lat": None, "lon": None, "precision": "",
             "parse_ok": False, "pdf_path": "", "flags": "πιθανό διπλό"},
        ]
        col = {key: i for i, (_, key, _) in enumerate(COLUMNS, 1)}
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "out.xlsx"
            write_xlsx(rows, path)
            wb = load_workbook(path)
            ws = wb["Κατεδαφίσεις"]
            self.assertEqual(ws.max_row, 3)
            self.assertEqual(ws.cell(2, col["ada"]).hyperlink.target,
                             "https://diavgeia.gov.gr/decision/view/ΑΔΑ1")
            self.assertEqual(ws.cell(2, col["date"]).number_format, "DD/MM/YYYY")
            # η στήλη PDF δείχνει στη Διαύγεια (προβολή), ποτέ τοπική διαδρομή
            self.assertEqual(ws.cell(2, col["pdf_path"]).hyperlink.target,
                             "https://diavgeia.gov.gr/doc/ΑΔΑ1?inline=true")
            self.assertEqual(ws.cell(3, col["pdf_path"]).hyperlink.target,
                             "https://diavgeia.gov.gr/doc/ΑΔΑ2?inline=true")
            self.assertEqual(ws.cell(3, col["parse_ok"]).value, "ΟΧΙ")
            self.assertEqual(wb.properties.creator, "demolitions")
            pivot = wb["Ανά έτος-δήμο"]
            self.assertEqual(pivot.cell(1, 2).value, "Δήμος Δράμας")
            self.assertEqual(pivot.cell(4, 4).value, 2)      # γενικό σύνολο
            self.assertNotIn("Οικοδομικές με κατεδάφιση", wb.sheetnames)

    def test_xlsx_me_oikodomikes(self):
        from openpyxl import load_workbook
        base = {"ada": "Χ", "url": "u", "dim_enotita": "", "poli": "",
                "odos": "", "ar_apo": "", "ar_eos": "", "ot": "", "kaek": "",
                "perigrafi": "", "orofoi": None, "lat": None, "lon": None,
                "precision": "", "parse_ok": True, "pdf_path": "", "flags": ""}
        rows = [
            {**base, "date": "2021-01-01", "year": 2021,
             "dimos": "Δήμος Δράμας", "eidos": "κατεδάφιση"},
            {**base, "date": "2021-02-01", "year": 2021,
             "dimos": "Δήμος Δράμας", "eidos": "οικοδομική με κατεδάφιση"},
        ]
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "out.xlsx"
            write_xlsx(rows, path)
            wb = load_workbook(path)
            self.assertIn("Οικοδομικές με κατεδάφιση", wb.sheetnames)
            # το βασικό pivot μετρά μόνο τις αυτοτελείς
            self.assertEqual(wb["Ανά έτος-δήμο"].cell(3, 3).value, 1)
            self.assertEqual(wb["Οικοδομικές με κατεδάφιση"].cell(3, 3).value, 1)


class TestRunsConsistency(unittest.TestCase):
    """Έλεγχοι ακεραιότητας στα αποθηκευμένα runs (αν υπάρχουν)."""

    def test_rows_json(self):
        for run_dir in (Path(__file__).parent / "runs").glob("*"):
            f = run_dir / "rows.json"
            if not f.exists():
                continue
            rows = json.loads(f.read_text(encoding="utf-8"))
            m = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
            self.assertEqual(m["n_rows"], len(rows), run_dir.name)
            for r in rows:
                self.assertIn("decision/view", r["url"], run_dir.name)
                # τα PDF υπάρχουν στον δίσκο μόνο αν δεν έχουν εκκαθαριστεί
                if r["pdf_path"] and m.get("has_pdfs", True):
                    self.assertTrue((run_dir / r["pdf_path"]).exists(),
                                    f"{run_dir.name}: λείπει {r['pdf_path']}")


class TestRunPipelinePdfCallback(unittest.TestCase):
    """Ο pdf_callback ανεβάζει/σβήνει κάθε PDF αμέσως — έτσι ο εφήμερος
    δίσκος δεν γεμίζει σε μεγάλες αναζητήσεις (το bug της Αττικής 2025)."""

    def _decision(self, i):
        ts = int(datetime(2021, 1, 15, 12, tzinfo=timezone.utc).timestamp() * 1000)
        return {"ada": f"ΑΔΑ{i}", "issueDate": ts,
                "subject": "Άδεια Κατεδάφισης (ν.4759/2020): ΚΑΤΕΔΑΦΙΣΗ ΚΑΤΟΙΚΙΑΣ",
                "extraFieldValues": {"municipality": "0201"}}  # Δήμος Δράμας

    def _fake_parse(self, decision, cache_dir):
        return {"ada": decision["ada"],
                "url": f"https://diavgeia.gov.gr/decision/view/{decision['ada']}",
                "perigrafi": "ΚΑΤΕΔΑΦΙΣΗ ΚΑΤΟΙΚΙΑΣ", "parse_ok": True,
                "odos": "Οδός", "ar_apo": str(decision["ada"][-1]), "ar_eos": "",
                "poli": "Δράμα", "dim_enotita": "", "ot": "", "kaek": "",
                "orofoi": 1, "ektasi": "ολική", "nonbuilding": False}

    def _run(self, tmp, n_decisions, n_cached, calls, **kwargs):
        from unittest import mock
        from demolitions import pipeline
        cache_dir = Path(tmp) / "cache"
        (cache_dir / "pdf").mkdir(parents=True)
        decisions = [self._decision(i) for i in range(n_decisions)]
        for i in range(n_cached):     # μόνο τα πρώτα n_cached έχουν cached PDF
            (cache_dir / "pdf" / f"ΑΔΑ{i}.pdf").write_bytes(b"%PDF-1.4 x" * 50)
        from datetime import date as _date
        with mock.patch.object(pipeline, "search_permits", return_value=decisions), \
             mock.patch.object(pipeline, "parse_decision", side_effect=self._fake_parse):
            return pipeline.run_pipeline(
                "Δήμος Δράμας", _date(2021, 1, 1), _date(2021, 12, 31),
                Path(tmp) / "run", cache_dir=cache_dir,
                log=lambda m: None,
                pdf_callback=lambda dest, rel: calls.append((dest, rel)),
                **kwargs)

    def test_callback_fires_once_per_cached_pdf(self):
        with tempfile.TemporaryDirectory() as tmp:
            calls = []
            result = self._run(tmp, n_decisions=3, n_cached=2, calls=calls)
            # 3 άδειες, 2 με cached PDF -> 2 κλήσεις
            self.assertEqual(len(calls), 2)
            for dest, rel in calls:
                self.assertTrue(rel.startswith("pdf/"))
                # το PDF υπάρχει στον δίσκο τη στιγμή του callback (το upload
                # το διαβάζει· ο R2 backend το σβήνει αμέσως μετά)
                self.assertTrue(Path(dest).is_file())
                self.assertEqual(str(Path(dest).relative_to(result.run_dir)), rel)
            # η άδεια χωρίς cached PDF έχει κενό pdf_path, χωρίς callback
            blank = [r for r in result.rows if not r["pdf_path"]]
            self.assertEqual(len(blank), 1)
            self.assertEqual(blank[0]["ada"], "ΑΔΑ2")

    def test_no_callback_when_not_provided(self):
        # χωρίς pdf_callback (CLI / τοπικό) το pipeline τρέχει κανονικά
        with tempfile.TemporaryDirectory() as tmp:
            from unittest import mock
            from demolitions import pipeline
            from datetime import date as _date
            cache_dir = Path(tmp) / "cache"
            (cache_dir / "pdf").mkdir(parents=True)
            (cache_dir / "pdf" / "ΑΔΑ0.pdf").write_bytes(b"%PDF-1.4 x")
            with mock.patch.object(pipeline, "search_permits",
                                   return_value=[self._decision(0)]), \
                 mock.patch.object(pipeline, "parse_decision",
                                   side_effect=self._fake_parse):
                result = pipeline.run_pipeline(
                    "Δήμος Δράμας", _date(2021, 1, 1), _date(2021, 12, 31),
                    Path(tmp) / "run", cache_dir=cache_dir, log=lambda m: None)
            self.assertEqual(result.rows[0]["pdf_path"],
                             "pdf/Δήμος Δράμας/2021/ΑΔΑ0.pdf")

    def test_pdf_staged_during_download_phase_not_after(self):
        """Η αντιγραφή/ανέβασμα γίνεται ΜΕΣΑ στη φάση download (overlap), όχι
        σε χωριστό δεύτερο loop — αλλιώς χάνεται η επικάλυψη upload/download."""
        from unittest import mock
        from demolitions import pipeline
        from datetime import date as _date
        with tempfile.TemporaryDirectory() as tmp:
            cache_dir = Path(tmp) / "cache"
            (cache_dir / "pdf").mkdir(parents=True)
            decisions = [self._decision(i) for i in range(3)]
            for i in range(3):
                (cache_dir / "pdf" / f"ΑΔΑ{i}.pdf").write_bytes(b"%PDF-1.4 x")
            events = []

            def parse(d, c):
                events.append(("parse", d["ada"]))
                return self._fake_parse(d, c)

            with mock.patch.object(pipeline, "search_permits", return_value=decisions), \
                 mock.patch.object(pipeline, "parse_decision", side_effect=parse):
                pipeline.run_pipeline(
                    "Δήμος Δράμας", _date(2021, 1, 1), _date(2021, 12, 31),
                    Path(tmp) / "run", cache_dir=cache_dir, log=lambda m: None,
                    pdf_callback=lambda dest, rel: events.append(
                        ("upload", Path(dest).stem)))

            first_upload = next(k for k, e in enumerate(events) if e[0] == "upload")
            last_parse = max(k for k, e in enumerate(events) if e[0] == "parse")
            # το πρώτο ανέβασμα πρέπει να συμβεί ΠΡΙΝ τελειώσουν όλα τα parse
            self.assertLess(first_upload, last_parse,
                            f"χωρίς overlap upload/download: {events}")

    def test_free_cache_deletes_cache_copy_after_staging(self):
        """Με free_cache=True (hosted) το PDF σβήνεται από την cache μόλις
        ανέβει — αλλιώς ο εφήμερος δίσκος γεμίζει με όλα τα PDF του run."""
        from unittest import mock
        from demolitions import pipeline
        from datetime import date as _date
        with tempfile.TemporaryDirectory() as tmp:
            cache_dir = Path(tmp) / "cache"
            (cache_dir / "pdf").mkdir(parents=True)
            (cache_dir / "pdf" / "ΑΔΑ0.pdf").write_bytes(b"%PDF-1.4 x")
            with mock.patch.object(pipeline, "search_permits",
                                   return_value=[self._decision(0)]), \
                 mock.patch.object(pipeline, "parse_decision",
                                   side_effect=self._fake_parse):
                pipeline.run_pipeline(
                    "Δήμος Δράμας", _date(2021, 1, 1), _date(2021, 12, 31),
                    Path(tmp) / "run", cache_dir=cache_dir, log=lambda m: None,
                    pdf_callback=lambda dest, rel: None, free_cache=True)
            # το PDF σβήστηκε από την cache, αλλά υπάρχει στο run_dir (staging)
            self.assertFalse((cache_dir / "pdf" / "ΑΔΑ0.pdf").exists())
            self.assertTrue((Path(tmp) / "run" / "pdf" / "Δήμος Δράμας" /
                             "2021" / "ΑΔΑ0.pdf").exists())

    def test_default_keeps_cache_copy(self):
        # default (τοπικό): η cache διατηρείται για ταχύτερα re-runs
        from unittest import mock
        from demolitions import pipeline
        from datetime import date as _date
        with tempfile.TemporaryDirectory() as tmp:
            cache_dir = Path(tmp) / "cache"
            (cache_dir / "pdf").mkdir(parents=True)
            (cache_dir / "pdf" / "ΑΔΑ0.pdf").write_bytes(b"%PDF-1.4 x")
            with mock.patch.object(pipeline, "search_permits",
                                   return_value=[self._decision(0)]), \
                 mock.patch.object(pipeline, "parse_decision",
                                   side_effect=self._fake_parse):
                pipeline.run_pipeline(
                    "Δήμος Δράμας", _date(2021, 1, 1), _date(2021, 12, 31),
                    Path(tmp) / "run", cache_dir=cache_dir, log=lambda m: None)
            self.assertTrue((cache_dir / "pdf" / "ΑΔΑ0.pdf").exists())

    def test_size_estimate_logged(self):
        with tempfile.TemporaryDirectory() as tmp:
            logs = []
            from unittest import mock
            from demolitions import pipeline
            from datetime import date as _date
            cache_dir = Path(tmp) / "cache"
            (cache_dir / "pdf").mkdir(parents=True)
            with mock.patch.object(pipeline, "search_permits",
                                   return_value=[self._decision(i) for i in range(2)]), \
                 mock.patch.object(pipeline, "parse_decision",
                                   side_effect=self._fake_parse):
                pipeline.run_pipeline(
                    "Δήμος Δράμας", _date(2021, 1, 1), _date(2021, 12, 31),
                    Path(tmp) / "run", cache_dir=cache_dir, log=logs.append)
            self.assertTrue(any("Εκτιμώμενο μέγεθος PDF" in m for m in logs))


class TestR2Storage(unittest.TestCase):
    """Έλεγχος του R2 backend με εικονικό S3 (moto), αν είναι διαθέσιμο."""

    def setUp(self):
        import importlib.util
        if importlib.util.find_spec("moto") is None:
            self.skipTest("moto δεν είναι εγκατεστημένο")

    def _make_staging(self, store, run_id, n_pdfs, pdf_bytes=b"%PDF-1.4 test",
                      created="2024-01-01", created_at=None):
        d = store.staging_dir(run_id)
        rows = []
        for i in range(n_pdfs):
            rel = f"pdf/Δήμος Χ/2021/ΑΔΑ{i}.pdf"
            p = d / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(pdf_bytes)
            rows.append({"ada": f"ΑΔΑ{i}", "pdf_path": rel})
        (d / "rows.json").write_text(json.dumps(rows, ensure_ascii=False), "utf-8")
        (d / (run_id + ".xlsx")).write_bytes(b"xlsx-bytes")
        manifest = {"run_id": run_id, "n_rows": n_pdfs, "has_pdfs": True,
                    "created": created}
        if created_at:
            manifest["created_at"] = created_at
        (d / "run.json").write_text(json.dumps(
            manifest), "utf-8")

    def test_roundtrip_and_eviction(self):
        from moto import mock_aws
        with mock_aws():
            import boto3
            boto3.client("s3", region_name="us-east-1").create_bucket(Bucket="demo-test-bucket")
            import os
            os.environ.update(R2_BUCKET="demo-test-bucket", R2_ACCOUNT_ID="acc",
                              R2_ACCESS_KEY_ID="k", R2_SECRET_ACCESS_KEY="s")
            from demolitions.storage import R2Storage
            store = R2Storage("demo-test-bucket", "acc", "k", "s", pdf_cap=40,
                              endpoint_url="https://s3.amazonaws.com")

            # δύο run, 3 PDF το καθένα (13 bytes -> 39 bytes/run)
            self._make_staging(store, "old", 3, created="2024-01-01",
                               created_at="2024-01-01T10:00:00+00:00")
            store.save_run("old")
            self._make_staging(store, "new", 3, created="2024-01-02",
                               created_at="2024-01-02T10:00:00+00:00")
            store.save_run("new")

            self.assertTrue(store.exists("new"))
            self.assertEqual(len(store.list_runs()), 2)
            # ανάγνωση member
            gen, size = store.open_member("new", "new.xlsx")
            self.assertEqual(b"".join(gen), b"xlsx-bytes")
            self.assertEqual(len(list(store.iter_pdfs("new"))), 3)

            # free_local_pdfs: σβήνει τοπικά, το R2 μένει ανέπαφο
            store.free_local_pdfs("new")
            self.assertFalse((store.staging_dir("new") / "pdf").exists())
            self.assertEqual(len(list(store.iter_pdfs("new"))), 3)
            store.save_meta("new")   # ανέβασμα μόνο json/xlsx, χωρίς σφάλμα

            # cap=40: χωράει 1 run (39b), το παλιότερο χάνει τα PDF του
            store.enforce_pdf_cap()
            kept = {m["run_id"]: m.get("has_pdfs") for m in store.list_runs()}
            self.assertEqual(kept["new"], True)
            self.assertEqual(kept["old"], False)
            self.assertEqual(len(list(store.iter_pdfs("old"))), 0)
            # μεταδεδομένα του old παραμένουν
            self.assertEqual(store.read_manifest("old")["n_rows"], 3)

            store.delete_run("new")
            self.assertFalse(store.exists("new"))

    def test_upload_pdf_immediate_uploads_and_deletes_local(self):
        from moto import mock_aws
        with mock_aws():
            import boto3
            boto3.client("s3", region_name="us-east-1").create_bucket(Bucket="demo-test-bucket")
            from demolitions.storage import R2Storage
            store = R2Storage("demo-test-bucket", "acc", "k", "s",
                              endpoint_url="https://s3.amazonaws.com")
            d = store.staging_dir("r1")
            p = d / "pdf" / "Δήμος Χ" / "2021" / "ΑΔΑ1.pdf"
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(b"%PDF-1.4 test")
            relpath = str(p.relative_to(d))

            store.upload_pdf_immediate("r1", relpath, p)

            self.assertFalse(p.exists())                        # τοπικό αντίγραφο σβήστηκε
            self.assertEqual(len(list(store.iter_pdfs("r1"))), 1)  # αρχείο στο R2

    def test_cleanup_removes_partial_r2_uploads(self):
        from moto import mock_aws
        with mock_aws():
            import boto3
            boto3.client("s3", region_name="us-east-1").create_bucket(Bucket="demo-test-bucket")
            from demolitions.storage import R2Storage
            store = R2Storage("demo-test-bucket", "acc", "k", "s",
                              endpoint_url="https://s3.amazonaws.com")
            d = store.staging_dir("r1")
            p = d / "pdf" / "Δήμος Χ" / "2021" / "ΑΔΑ1.pdf"
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(b"%PDF-1.4 test")
            relpath = str(p.relative_to(d))

            store.upload_pdf_immediate("r1", relpath, p)
            self.assertEqual(len(list(store.iter_pdfs("r1"))), 1)
            self.assertFalse(store.exists("r1"))   # run.json δεν έχει ανέβει ακόμα

            store.cleanup("r1")

            # cleanup αφαιρεί partial R2 uploads όταν run.json λείπει
            self.assertEqual(len(list(store.iter_pdfs("r1"))), 0)

    def test_resilient_body_resumes_after_dropped_read(self):
        """Αν κοπεί η σύνδεση R2 στη μέση, το _resilient_body ξανανοίγει με
        Range και ολοκληρώνει — δεν κόβει το zip (αιτία του 2.3 αντί 3.24 GB)."""
        from moto import mock_aws
        with mock_aws():
            import boto3
            boto3.client("s3", region_name="us-east-1").create_bucket(Bucket="demo-test-bucket")
            from demolitions.storage import R2Storage
            store = R2Storage("demo-test-bucket", "acc", "k", "s",
                              endpoint_url="https://s3.amazonaws.com")
            data = b"abcdefgh" * 30000          # 240 KB -> πολλά chunks
            key = "runs/r1/pdf/a.pdf"
            store.s3.put_object(Bucket="demo-test-bucket", Key=key, Body=data)

            class FlakyBody:
                """Σκάει στο 2ο read· μετά συμπεριφέρεται κανονικά."""
                def __init__(self, real):
                    self.real, self.reads = real, 0
                def read(self, n=-1):
                    self.reads += 1
                    if self.reads == 2:
                        raise ConnectionError("dropped")
                    return self.real.read(n)
                def close(self):
                    self.real.close()

            first = store.s3.get_object(Bucket="demo-test-bucket", Key=key)["Body"]
            out = b"".join(store._resilient_body(key, FlakyBody(first), len(data)))
            self.assertEqual(out, data)         # πλήρες, παρά την πτώση

    def test_eviction_uses_creation_time_not_manifest_mtime(self):
        from moto import mock_aws
        with mock_aws():
            import boto3
            boto3.client("s3", region_name="us-east-1").create_bucket(Bucket="demo-test-bucket")
            os.environ.update(R2_BUCKET="demo-test-bucket", R2_ACCOUNT_ID="acc",
                              R2_ACCESS_KEY_ID="k", R2_SECRET_ACCESS_KEY="s")
            from demolitions.storage import R2Storage
            store = R2Storage("demo-test-bucket", "acc", "k", "s", pdf_cap=40,
                              endpoint_url="https://s3.amazonaws.com")

            self._make_staging(store, "old", 3, created="2024-01-01",
                               created_at="2024-01-01T10:00:00+00:00")
            store.save_run("old")
            self._make_staging(store, "new", 3, created="2024-01-02",
                               created_at="2024-01-02T10:00:00+00:00")
            store.save_run("new")

            manifest = store.read_manifest("old")
            manifest["geocoded"] = True
            store.write_manifest("old", manifest)  # παλιό bug: το έκανε «νεότερο»

            store.enforce_pdf_cap()
            kept = {m["run_id"]: m.get("has_pdfs") for m in store.list_runs()}
            self.assertEqual(kept["new"], True)
            self.assertEqual(kept["old"], False)


class TestLocalStorage(unittest.TestCase):
    def test_lifecycle(self):
        from demolitions.storage import LocalStorage
        with tempfile.TemporaryDirectory() as tmp:
            st = LocalStorage(tmp)
            d = st.staging_dir("r1")
            (d / "pdf" / "Δήμος Χ" / "2021").mkdir(parents=True)
            (d / "pdf" / "Δήμος Χ" / "2021" / "a.pdf").write_bytes(b"PDF" * 100)
            (d / "rows.json").write_text("[]", "utf-8")
            (d / "run.json").write_text(
                json.dumps({"run_id": "r1", "has_pdfs": True, "n_rows": 0}), "utf-8")
            (d / "r1.xlsx").write_bytes(b"xlsx")

            self.assertTrue(st.exists("r1"))
            self.assertEqual([m["run_id"] for m in st.list_runs()], ["r1"])
            sizes = st.sizes_by_run()["r1"]
            self.assertEqual(sizes["pdf_bytes"], 300)
            self.assertGreater(sizes["total_bytes"], 300)
            self.assertEqual(st.usage()["pdf_bytes"], 300)
            gen, sz = st.open_member("r1", "r1.xlsx")
            self.assertEqual(b"".join(gen), b"xlsx")
            self.assertEqual(sz, 4)
            self.assertIsNone(st.open_member("r1", "../../etc"))   # traversal
            self.assertEqual(len(list(st.iter_pdfs("r1"))), 1)

            st.delete_pdfs("r1")
            self.assertEqual(list(st.iter_pdfs("r1")), [])
            self.assertTrue(st.exists("r1"))                       # μεταδεδομένα μένουν
            st.delete_run("r1")
            self.assertFalse(st.exists("r1"))


class TestWebUI(unittest.TestCase):
    """Ολοκληρωμένος έλεγχος των endpoints με τοπικό store σε temp φάκελο."""

    RID = "test-run_2021-01-01_2021-12-31_xyz"

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp()
        os.environ["DEMOLITIONS_STORAGE"] = "local"
        os.environ["DEMOLITIONS_RUNS_DIR"] = os.path.join(cls.tmp, "runs")
        os.environ["DEMOLITIONS_CACHE_DIR"] = os.path.join(cls.tmp, "cache")
        import importlib
        cls.webui = importlib.reload(importlib.import_module("webui"))
        cls.client = cls.webui.app.test_client()

    @classmethod
    def tearDownClass(cls):
        import shutil
        shutil.rmtree(cls.tmp, ignore_errors=True)
        for k in ("DEMOLITIONS_STORAGE", "DEMOLITIONS_RUNS_DIR", "DEMOLITIONS_CACHE_DIR"):
            os.environ.pop(k, None)

    def setUp(self):
        # φρέσκο ολοκληρωμένο run πριν από κάθε test (κάποια το διαγράφουν)
        from demolitions.output import write_xlsx
        self.webui.rate_hits.clear()
        self.webui.RATE_LIMIT_WINDOW_SECONDS = 60
        self.webui.RATE_LIMIT_MAX_REQUESTS = 12
        d = self.webui.store.staging_dir(self.RID)
        (d / "pdf" / "Δήμος Χ" / "2021").mkdir(parents=True, exist_ok=True)
        (d / "pdf" / "Δήμος Χ" / "2021" / "ΑΔΑ1.pdf").write_bytes(b"%PDF-1.4 x")
        rows = [{
            "ada": "ΑΔΑ1", "url": "https://diavgeia.gov.gr/decision/view/ΑΔΑ1",
            "date": "2021-01-15", "year": 2021, "dimos": "Δήμος Χ",
            "eidos": "κατεδάφιση", "ektasi": "ολική", "dim_enotita": "",
            "poli": "Χ", "odos": "Οδός", "ar_apo": "1", "ar_eos": "", "ot": "",
            "kaek": "", "perigrafi": "ΚΑΤΕΔΑΦΙΣΗ", "orofoi": 1,
            "lat": 41.1, "lon": 24.1, "precision": "κτίσμα (PDF)",
            "parse_ok": True, "pdf_path": "pdf/Δήμος Χ/2021/ΑΔΑ1.pdf",
            "flags": "", "poly": [[41.1, 24.1], [41.1, 24.11], [41.11, 24.11]],
        }]
        write_xlsx(rows, d / (self.RID + ".xlsx"))
        (d / "rows.json").write_text(json.dumps(rows, ensure_ascii=False), "utf-8")
        (d / "run.json").write_text(json.dumps({
            "run_id": self.RID, "area": "Δήμος Χ", "from": "2021-01-01",
            "to": "2021-12-31", "created": "2021-01-15", "n_rows": 1,
            "n_dups": 0, "geocoded": True, "has_pdfs": True}, ensure_ascii=False), "utf-8")

    def _url(self, path):
        from urllib.parse import quote
        return path.replace("<rid>", quote(self.RID))

    def test_index_and_areas(self):
        r = self.client.get("/")
        self.assertEqual(r.status_code, 200)
        self.assertIn(b"tab-about", r.data)
        self.assertIn(b'value="2018-10-01"', r.data)   # default «Από»
        self.assertGreater(len(self.client.get("/api/areas").get_json()), 300)

    def test_healthz(self):
        r = self.client.get("/healthz")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.data, b"ok")

    def test_about(self):
        a = self.client.get("/api/about").get_json()
        self.assertEqual(a["backend"], "local")
        self.assertGreaterEqual(a["n_runs"], 1)
        self.assertEqual(a["data_since"], "2018-10-01")

    def test_runs_has_sizes(self):
        m = next(x for x in self.client.get("/api/runs").get_json()
                 if x["run_id"] == self.RID)
        self.assertGreater(m["total_bytes"], 0)
        self.assertGreater(m["pdf_bytes"], 0)

    def test_serve_xlsx_and_rows(self):
        r = self.client.get(self._url("/runs/<rid>/<rid>.xlsx".replace(
            "<rid>.xlsx", __import__("urllib.parse", fromlist=["quote"]).quote(self.RID) + ".xlsx")))
        self.assertEqual(r.status_code, 200)
        self.assertIn("attachment", r.headers.get("Content-Disposition", ""))
        rows = self.client.get(self._url("/api/runs/<rid>/rows")).get_json()
        self.assertEqual(len(rows), 1)

    def test_zip_contains_xlsx_and_pdf(self):
        r = self.client.get(self._url("/zip/<rid>.zip"))
        self.assertEqual(r.status_code, 200)
        names = zipfile.ZipFile(io.BytesIO(r.data)).namelist()
        self.assertTrue(any(n.endswith(".xlsx") for n in names))
        self.assertTrue(any(n.endswith(".pdf") for n in names))

    def test_zip_opens_members_lazily(self):
        # τα μέλη (xlsx/PDF) ΔΕΝ πρέπει να ανοίγονται κατά την κατασκευή της
        # απόκρισης — αλλιώς N round-trips πριν το πρώτο byte (-> 502 hosted)
        w = self.webui
        calls = []
        orig = w.store.open_member
        w.store.open_member = lambda rid, rel: (calls.append(rel) or orig(rid, rel))
        try:
            with w.app.test_request_context():
                resp = w.serve_zip(self.RID)
                self.assertEqual(calls, ["rows.json"])   # μόνο αυτό κατά την κατασκευή
                body = b"".join(resp.response)            # τώρα streamάρει
            self.assertIn(self.RID + ".xlsx", calls)      # ανοίχτηκε κατά το stream
            self.assertTrue(any(c.endswith(".pdf") for c in calls))
            self.assertTrue(zipfile.ZipFile(io.BytesIO(body)).namelist())
        finally:
            w.store.open_member = orig

    def test_zip_falls_back_to_diavgeia_when_cached_pdf_missing(self):
        """has_pdfs=True αλλά λείπει αντικείμενο στο R2 -> το zip το παίρνει
        από τη Διαύγεια αντί για 0-byte εγγραφή (μικρότερο zip)."""
        from unittest import mock
        w = self.webui
        orig = w.store.open_member

        def fake_open(rid, rel):
            if rel.endswith(".pdf"):
                return None                  # cached PDF λείπει
            return orig(rid, rel)

        with mock.patch.object(w.store, "open_member", side_effect=fake_open), \
             mock.patch.object(w, "_diavgeia_pdf",
                               side_effect=lambda ada: iter([b"%PDF-FALLBACK"])):
            with w.app.test_request_context():
                resp = w.serve_zip(self.RID)
                body = b"".join(resp.response)
        zf = zipfile.ZipFile(io.BytesIO(body))
        pdfs = [n for n in zf.namelist() if n.endswith(".pdf")]
        self.assertTrue(pdfs)
        self.assertEqual(zf.read(pdfs[0]), b"%PDF-FALLBACK")   # όχι 0 bytes

    def test_zip_rate_limit(self):
        self.webui.RATE_LIMIT_WINDOW_SECONDS = 3600
        self.webui.RATE_LIMIT_MAX_REQUESTS = 1
        r1 = self.client.get(self._url("/zip/<rid>.zip"),
                             environ_base={"REMOTE_ADDR": "203.0.113.9"})
        self.assertEqual(r1.status_code, 200)
        r2 = self.client.get(self._url("/zip/<rid>.zip"),
                             environ_base={"REMOTE_ADDR": "203.0.113.9"})
        self.assertEqual(r2.status_code, 429)
        self.assertIn("Retry-After", r2.headers)

    def test_run_validation(self):
        r = self.client.post("/api/run", json={
            "area": "Δήμος Ασγκαμπάτ", "from": "2021-01-01", "to": "2021-12-31"})
        self.assertEqual(r.status_code, 400)
        r = self.client.post("/api/run", json={
            "area": "Δήμος Δράμας", "from": "2022-01-01", "to": "2021-01-01"})
        self.assertEqual(r.status_code, 400)

    def test_traversal_blocked(self):
        r = self.client.get(self._url("/runs/<rid>/..%2f..%2fwebui.py"))
        self.assertIn(r.status_code, (400, 404))

    def test_clear_pdfs_then_delete(self):
        r = self.client.delete(self._url("/api/runs/<rid>/pdfs"))
        self.assertEqual(r.status_code, 200)
        m = next(x for x in self.client.get("/api/runs").get_json()
                 if x["run_id"] == self.RID)
        self.assertFalse(m["has_pdfs"])
        self.assertEqual(m["pdf_bytes"], 0)
        self.assertTrue(self.webui.store.exists(self.RID))   # μεταδεδομένα μένουν
        r = self.client.delete(self._url("/api/runs/<rid>"))
        self.assertEqual(r.status_code, 200)
        self.assertFalse(self.webui.store.exists(self.RID))

    def test_delete_all(self):
        self.assertTrue(self.webui.store.exists(self.RID))
        r = self.client.delete("/api/runs")
        self.assertEqual(r.status_code, 200)
        self.assertGreaterEqual(r.get_json()["deleted"], 1)
        self.assertEqual(self.client.get("/api/runs").get_json(), [])

    def test_delete_all_pdfs(self):
        r = self.client.delete("/api/pdfs")
        self.assertEqual(r.status_code, 200)
        self.assertGreaterEqual(r.get_json()["cleared"], 1)
        m = next(x for x in self.client.get("/api/runs").get_json()
                 if x["run_id"] == self.RID)
        self.assertFalse(m["has_pdfs"])           # PDF καθαρίστηκαν
        self.assertEqual(m["pdf_bytes"], 0)
        self.assertTrue(self.webui.store.exists(self.RID))   # το run μένει

    def test_keepalive_noop_without_url(self):
        # τοπικά (χωρίς RENDER_EXTERNAL_URL) το keep-alive δεν κάνει αίτημα
        from unittest import mock
        w = self.webui
        with mock.patch.object(w, "KEEPALIVE_URL", None), \
             mock.patch.object(w.urllib.request, "urlopen") as urlopen:
            j = w.Job()
            j.state = "running"
            w._keepalive_loop(j)        # επιστρέφει αμέσως
            urlopen.assert_not_called()

    def test_keepalive_pings_then_stops_when_job_done(self):
        from unittest import mock
        w = self.webui
        j = w.Job()
        j.state = "running"

        # στο πρώτο ping το job «τελειώνει» -> ο βρόχος σταματά αμέσως μετά
        def stop(*a, **k):
            j.state = "done"
            return mock.MagicMock()

        with mock.patch.object(w, "KEEPALIVE_URL", "https://x.onrender.com"), \
             mock.patch.object(w.urllib.request, "urlopen",
                               side_effect=stop) as urlopen, \
             mock.patch.object(w.time, "sleep"):
            w._keepalive_loop(j)
        urlopen.assert_called_once()
        self.assertIn("/healthz", urlopen.call_args[0][0])

    def test_rate_limit_blocks_after_max(self):
        w = self.webui
        with w.rate_lock:
            w.rate_hits.clear()
        with w.app.test_request_context(environ_base={"REMOTE_ADDR": "7.7.7.7"}):
            results = [w._check_rate_limit("zip")
                       for _ in range(w.RATE_LIMIT_MAX_REQUESTS + 2)]
        self.assertIsNone(results[0])
        self.assertIsNotNone(results[-1])          # μπλοκάρεται μετά το όριο

    def test_rate_limit_prunes_stale_keys(self):
        w = self.webui
        with w.rate_lock:
            w.rate_hits.clear()
            w.rate_hits[("1.1.1.1", "run")] = [0.0]   # έληξε (timestamp 0)
            w.rate_hits[("2.2.2.2", "zip")] = []        # κενή
        with w.app.test_request_context(environ_base={"REMOTE_ADDR": "9.9.9.9"}):
            w._check_rate_limit("run")
        self.assertNotIn(("1.1.1.1", "run"), w.rate_hits)
        self.assertNotIn(("2.2.2.2", "zip"), w.rate_hits)
        self.assertIn(("9.9.9.9", "run"), w.rate_hits)


if __name__ == "__main__":
    unittest.main(verbosity=2)
