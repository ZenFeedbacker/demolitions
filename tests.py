#!/usr/bin/env python3
"""Δοκιμές χωρίς δίκτυο: python3 tests.py

Χρειάζονται μόνο το cache/kallikratis.json (υπάρχει μετά το πρώτο run).
"""

import json
import tempfile
import unittest
from pathlib import Path

CACHE = str(Path(__file__).parent / "cache")

from demolitions.areas import AreaError, list_areas, normalize, resolve_area
from demolitions.diavgeia import KIND_KATEDAFISI, KIND_OIKODOMIKI, permit_kind
from demolitions.egsa87 import egsa87_to_wgs84
from demolitions.geocode import _poli_variants, _strip_dimos
from demolitions.greek import dimos_display, greek_title, pretty_area
from demolitions.output import COLUMNS, write_xlsx
from demolitions.pdfparse import (_clean, detect_extent, detect_floors,
                                   extract_polygon, parse_fields)


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

    def test_aporriptontai(self):
        for s in ("Προέγκριση Άδειας Κατεδάφισης: ...",
                  "Αναθεώρηση Άδειας Κατεδάφισης: ...",
                  "Ενημέρωση Οικοδομικής Άδειας: ΚΑΤΕΔΑΦΙΣΗ ...",
                  "Προέγκριση Οικοδομικής Άδειας: ΚΑΤΕΔΑΦΙΣΗ ...",
                  "Οικοδομική Άδεια (ν.4759/2020): ΑΝΕΓΕΡΣΗ ΚΑΤΟΙΚΙΑΣ",
                  "Έγκριση Εκτέλεσης Εργασιών: ΚΑΤΕΔΑΦΙΣΗ ΕΠΙΚΙΝΔΥΝΟΥ"):
            self.assertIsNone(permit_kind(s), s)


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


class TestGeocodeHelpers(unittest.TestCase):
    def test_strip_dimos(self):
        self.assertEqual(_strip_dimos("Δήμος Δράμας"), "Δράμας")
        self.assertEqual(_strip_dimos("ΔΗΜΟΣ ΔΡΑΜΑΣ"), "Δραμας")

    def test_poli_variants(self):
        self.assertEqual(_poli_variants("Οικισμός Ποταμιας Θάσου", "Θάσου"),
                         ["Ποταμιας", "Ποταμιας Θάσου", "Οικισμός Ποταμιας Θάσου"])
        self.assertEqual(_poli_variants("Δράμα/Προαστειο", "Δράμας"),
                         ["Δράμα/Προαστειο", "Δράμα", "Προαστειο"])
        self.assertEqual(_poli_variants("Δράμα", "Δράμας"), ["Δράμα"])
        self.assertEqual(_poli_variants("", "Δράμας"), [])


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
            self.assertEqual(ws.cell(2, col["pdf_path"]).hyperlink.target,
                             "pdf/Δήμος Δράμας/2021/ΑΔΑ1.pdf")
            self.assertIsNone(ws.cell(3, col["pdf_path"]).hyperlink)  # χωρίς PDF
            self.assertEqual(ws.cell(3, col["parse_ok"]).value, "ΟΧΙ")
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
                if r["pdf_path"]:
                    self.assertTrue((run_dir / r["pdf_path"]).exists(),
                                    f"{run_dir.name}: λείπει {r['pdf_path']}")


class TestR2Storage(unittest.TestCase):
    """Έλεγχος του R2 backend με εικονικό S3 (moto), αν είναι διαθέσιμο."""

    def setUp(self):
        import importlib.util
        if importlib.util.find_spec("moto") is None:
            self.skipTest("moto δεν είναι εγκατεστημένο")

    def _make_staging(self, store, run_id, n_pdfs, pdf_bytes=b"%PDF-1.4 test"):
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
        (d / "run.json").write_text(json.dumps(
            {"run_id": run_id, "n_rows": n_pdfs, "has_pdfs": True}), "utf-8")

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
            self._make_staging(store, "old", 3)
            store.save_run("old")
            self._make_staging(store, "new", 3)
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


if __name__ == "__main__":
    unittest.main(verbosity=2)
