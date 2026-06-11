"""Κατέβασμα και ανάλυση των PDF αδειών (φόρμα e-Άδειες).

Η φόρμα έχει σταθερές ετικέτες πεδίων («Οδός», «Πόλη/Οικισμός» κ.λπ.)
αριστερά και τιμές δεξιά· το `pdftotext -layout` διατηρεί τη στοίχιση.
"""

import re
import subprocess
import time
from pathlib import Path

from .areas import normalize
from .diavgeia import session
from .egsa87 import egsa87_to_wgs84
from .greek import greek_title

FIELD_LABELS = {
    "odos": "Οδός",
    "ar_apo": "Αρ. από",
    "ar_eos": "Αρ. έως",
    "poli": "Πόλη/Οικισμός",
    "dimos_pdf": "Δήμος",
    "dim_enotita": "Δημοτική Ενότητα /",
    "ot": "ΟΤ",
    "kaek": "ΚΑΕΚ",
    "perigrafi": "Περιγραφή έργου",
}

# αριθμός ορόφων από την περιγραφή· πιάνει και συχνά ορθογραφικά (ΔΙΟΡΩΦ...)
FLOOR_PATTERNS = [
    (r"ΕΞΑ[ΩΟ]ΡΟΦ", 6),
    (r"ΠΕΝΤΑ[ΩΟ]ΡΟΦ", 5),
    (r"ΤΕΤΡΑ[ΩΟ]ΡΟΦ|ΤΕΤΡΑΟΡΩΦ", 4),
    (r"ΤΡΙ[ΩΟ]ΡΟΦ|ΤΡΙΟΡΩΦ", 3),
    (r"ΔΙ[ΩΟΥ][ΟΡ]?ΡΟΦ|ΔΙΟΡΩΦ|ΔΥΟΡΟΦ", 2),
    (r"ΜΟΝ[ΩΟ]ΡΟΦ|ΜΟΝΟΡΩΦ|ΙΣ[ΟΩ]ΓΕΙ", 1),
]
# «ΚΑΤΟΙΚΙΑ 2 ΟΡΟΦΩΝ», «ΔΥΟ ΟΡΟΦΟΙ» κ.λπ.
NUM_FLOORS_RE = re.compile(r"\b(\d{1,2})\s*ΟΡΟΦ")
WORD_FLOORS_RE = re.compile(r"\b(ΕΝΟΣ|ΔΥΟ|ΤΡΙΩΝ|ΤΕΣΣΑΡΩΝ|ΠΕΝΤΕ|ΕΞΙ)\s+ΟΡΟΦ")
WORD_NUM = {"ΕΝΟΣ": 1, "ΔΥΟ": 2, "ΤΡΙΩΝ": 3, "ΤΕΣΣΑΡΩΝ": 4,
            "ΠΕΝΤΕ": 5, "ΕΞΙ": 6}


def download_pdf(decision, cache_dir):
    """Επιστρέφει το path του PDF στην cache, ή None αν αποτύχει."""
    pdf_dir = Path(cache_dir) / "pdf"
    pdf_dir.mkdir(parents=True, exist_ok=True)
    path = pdf_dir / f"{decision['ada']}.pdf"
    if path.exists() and path.stat().st_size > 0:
        return path
    # το search API συχνά γυρίζει κενό documentUrl· το URL προκύπτει από το ΑΔΑ
    url = decision.get("documentUrl") or f"https://diavgeia.gov.gr/doc/{decision['ada']}"
    for attempt in range(3):
        try:
            r = session.get(url, timeout=120)
            r.raise_for_status()
            if not r.content.startswith(b"%PDF"):
                return None
            path.write_bytes(r.content)
            time.sleep(0.4)
            return path
        except Exception:
            if attempt == 2:
                return None
            time.sleep(2 ** attempt)


def pdf_text(path):
    out = subprocess.run(
        ["pdftotext", "-layout", str(path), "-"],
        capture_output=True, timeout=60,
    )
    if out.returncode != 0:
        return None
    return out.stdout.decode("utf-8", errors="replace")


def _clean(value):
    value = value.strip()
    return "" if value in ("-", "—", "–") else value


def parse_fields(text):
    """Εξάγει τα πεδία διεύθυνσης από το κείμενο της φόρμας."""
    fields = {k: "" for k in FIELD_LABELS}
    # κρατάμε μόνο την πρώτη σελίδα (τα ίδια labels επανεμφανίζονται σε
    # πίνακες επόμενων σελίδων με άλλη σημασία)
    page = text.split("\f")[0]
    for line in page.splitlines():
        for key, label in FIELD_LABELS.items():
            if fields[key]:
                continue
            m = re.match(rf"^\s*{re.escape(label)}\s\s+(\S.*)$", line)
            if m:
                fields[key] = _clean(m.group(1))
    return fields


COORDS_LINE_RE = re.compile(r"^\s*Συντεταγμένες\s\s+(\S.*)$", re.M)
COORDS_PAIR_RE = re.compile(r"(\d{5,6}(?:\.\d+)?)\s+(\d{7}(?:\.\d+)?)")


def extract_polygon(text):
    """Περίγραμμα κτίσματος από το πεδίο «Συντεταγμένες» (ΕΓΣΑ87).

    Επιστρέφει λίστα [lat, lon] (WGS84) ή None. Οι κορυφές συνεχίζονται
    σε επόμενες γραμμές με βαθιά εσοχή· σταματάμε στην πρώτη γραμμή που
    δεν είναι αριθμητική.
    """
    m = COORDS_LINE_RE.search(text)
    if not m:
        return None
    chunk = m.group(1)
    for line in text[m.end():].splitlines()[1:8]:
        if re.match(r"^\s{8,}[\d.,\s]+$", line) and re.search(r"\d{6,}", line):
            chunk += " " + line.strip()
        else:
            break
    points = []
    for xs, ys in COORDS_PAIR_RE.findall(chunk):
        w = egsa87_to_wgs84(float(xs), float(ys))
        if w:
            points.append([round(w[0], 7), round(w[1], 7)])
    return points or None


def detect_extent(description_norm):
    """«ολική» ή «τμηματική/μερική» κατεδάφιση, από την περιγραφή.

    Συναγωγή: όσες αναφέρουν ΤΜΗΜΑ/ΜΕΡΙΚΗ θεωρούνται τμηματικές, οι
    υπόλοιπες ολικές — η φόρμα δεν έχει ρητό πεδίο.
    """
    if re.search(r"ΤΜΗΜΑ|ΜΕΡΙΚ", description_norm):
        return "τμηματική/μερική"
    return "ολική"


def detect_floors(description_norm):
    """Μέγιστος αριθμός ορόφων που αναφέρεται στην (κανονικοποιημένη) περιγραφή."""
    floors = [n for pat, n in FLOOR_PATTERNS if re.search(pat, description_norm)]
    floors += [int(m.group(1)) for m in NUM_FLOORS_RE.finditer(description_norm)
               if 1 <= int(m.group(1)) <= 12]
    floors += [WORD_NUM[m.group(1)]
               for m in WORD_FLOORS_RE.finditer(description_norm)]
    return max(floors) if floors else None


def parse_decision(decision, cache_dir):
    """Metadata + PDF -> ενιαίο dict εγγραφής (χωρίς συντεταγμένες ακόμα)."""
    subject = decision.get("subject", "")
    description = subject.split(":", 1)[1].strip() if ":" in subject else subject
    row = {
        "ada": decision["ada"],
        "url": f"https://diavgeia.gov.gr/decision/view/{decision['ada']}",
        "perigrafi": description,
        "parse_ok": False,
        "odos": "", "ar_apo": "", "ar_eos": "", "poli": "",
        "dimos_pdf": "", "dim_enotita": "", "ot": "", "kaek": "",
    }
    path = download_pdf(decision, cache_dir)
    if path:
        text = pdf_text(path)
        if text:
            # το περίγραμμα του κτίσματος (ΕΓΣΑ87) είναι η ακριβέστερη
            # πηγή θέσης — όταν υπάρχει, δεν χρειάζεται γεωκωδικοποίηση
            poly = extract_polygon(text)
            if poly:
                row["poly"] = poly
                row["lat"] = round(sum(p[0] for p in poly) / len(poly), 7)
                row["lon"] = round(sum(p[1] for p in poly) / len(poly), 7)
                row["precision"] = "κτίσμα (PDF)"
            fields = parse_fields(text)
            # η περιγραφή του PDF μπορεί να κόβεται σε αναδίπλωση γραμμής·
            # προτιμάμε το (πλήρες) subject και κρατάμε το PDF ως fallback
            if not description and fields["perigrafi"]:
                row["perigrafi"] = fields["perigrafi"]
            for k in ("ar_apo", "ar_eos", "ot", "kaek"):
                row[k] = fields[k]
            # τα τοπωνύμια της φόρμας είναι ΚΕΦΑΛΑΙΑ ΑΤΟΝΑ
            for k in ("odos", "poli", "dimos_pdf", "dim_enotita"):
                row[k] = greek_title(fields[k])
            row["parse_ok"] = bool(fields["poli"] or fields["odos"])
    desc_norm = normalize(row["perigrafi"])
    row["orofoi"] = detect_floors(desc_norm)
    row["ektasi"] = detect_extent(desc_norm)
    return row
