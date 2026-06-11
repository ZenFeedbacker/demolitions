"""Επίλυση περιοχής (δήμος / νομός-ΠΕ / περιφέρεια) σε κωδικούς Καλλικράτη.

Οι κωδικοί δήμων στο λεξικό ADMIN_STRUCTURE_KALLIKRATIS της Διαύγειας είναι
4ψήφιοι· τα δύο πρώτα ψηφία αντιστοιχούν σε περιφερειακή ενότητα (νομό).
"""

import json
import unicodedata
from pathlib import Path

import requests

DICTIONARY_URL = (
    "https://diavgeia.gov.gr/opendata/dictionaries/ADMIN_STRUCTURE_KALLIKRATIS.json"
)

# 2ψήφιο πρόθεμα κωδικού -> περιφερειακή ενότητα
PREFIX_PE = {
    "01": "ΡΟΔΟΠΗΣ", "02": "ΔΡΑΜΑΣ", "03": "ΕΒΡΟΥ", "04": "ΘΑΣΟΥ",
    "05": "ΚΑΒΑΛΑΣ", "06": "ΞΑΝΘΗΣ", "07": "ΘΕΣΣΑΛΟΝΙΚΗΣ", "08": "ΗΜΑΘΙΑΣ",
    "09": "ΚΙΛΚΙΣ", "10": "ΠΕΛΛΑΣ", "11": "ΠΙΕΡΙΑΣ", "12": "ΣΕΡΡΩΝ",
    "13": "ΧΑΛΚΙΔΙΚΗΣ", "14": "ΚΟΖΑΝΗΣ", "15": "ΓΡΕΒΕΝΩΝ", "16": "ΚΑΣΤΟΡΙΑΣ",
    "17": "ΦΛΩΡΙΝΑΣ", "18": "ΙΩΑΝΝΙΝΩΝ", "19": "ΑΡΤΑΣ", "20": "ΘΕΣΠΡΩΤΙΑΣ",
    "21": "ΠΡΕΒΕΖΑΣ", "22": "ΛΑΡΙΣΑΣ", "23": "ΚΑΡΔΙΤΣΑΣ", "24": "ΜΑΓΝΗΣΙΑΣ",
    "25": "ΣΠΟΡΑΔΩΝ", "26": "ΤΡΙΚΑΛΩΝ", "27": "ΦΘΙΩΤΙΔΑΣ", "28": "ΒΟΙΩΤΙΑΣ",
    "29": "ΕΥΒΟΙΑΣ", "30": "ΕΥΡΥΤΑΝΙΑΣ", "31": "ΦΩΚΙΔΑΣ", "32": "ΚΕΡΚΥΡΑΣ",
    "33": "ΖΑΚΥΝΘΟΥ", "34": "ΙΘΑΚΗΣ", "35": "ΚΕΦΑΛΛΗΝΙΑΣ", "36": "ΛΕΥΚΑΔΑΣ",
    "37": "ΑΧΑΪΑΣ", "38": "ΑΙΤΩΛΟΑΚΑΡΝΑΝΙΑΣ", "39": "ΗΛΕΙΑΣ", "40": "ΑΡΚΑΔΙΑΣ",
    "41": "ΑΡΓΟΛΙΔΑΣ", "42": "ΚΟΡΙΝΘΙΑΣ", "43": "ΛΑΚΩΝΙΑΣ", "44": "ΜΕΣΣΗΝΙΑΣ",
    "45": "ΚΕΝΤΡΙΚΟΥ ΤΟΜΕΑ ΑΘΗΝΩΝ", "46": "ΒΟΡΕΙΟΥ ΤΟΜΕΑ ΑΘΗΝΩΝ",
    "47": "ΔΥΤΙΚΟΥ ΤΟΜΕΑ ΑΘΗΝΩΝ", "48": "ΝΟΤΙΟΥ ΤΟΜΕΑ ΑΘΗΝΩΝ",
    "49": "ΑΝΑΤΟΛΙΚΗΣ ΑΤΤΙΚΗΣ", "50": "ΔΥΤΙΚΗΣ ΑΤΤΙΚΗΣ", "51": "ΠΕΙΡΑΙΩΣ",
    "52": "ΝΗΣΩΝ ΑΤΤΙΚΗΣ", "53": "ΛΕΣΒΟΥ", "54": "ΙΚΑΡΙΑΣ", "55": "ΛΗΜΝΟΥ",
    "56": "ΣΑΜΟΥ", "57": "ΧΙΟΥ", "58": "ΣΥΡΟΥ", "59": "ΑΝΔΡΟΥ", "60": "ΘΗΡΑΣ",
    "61": "ΚΑΛΥΜΝΟΥ", "62": "ΚΑΡΠΑΘΟΥ", "63": "ΚΕΑΣ - ΚΥΘΝΟΥ", "64": "ΚΩ",
    "65": "ΜΗΛΟΥ", "66": "ΜΥΚΟΝΟΥ", "67": "ΝΑΞΟΥ", "68": "ΠΑΡΟΥ", "69": "ΡΟΔΟΥ",
    "70": "ΤΗΝΟΥ", "71": "ΗΡΑΚΛΕΙΟΥ", "72": "ΛΑΣΙΘΙΟΥ", "73": "ΡΕΘΥΜΝΟΥ",
    "74": "ΧΑΝΙΩΝ", "99": "ΑΓΙΟΥ ΟΡΟΥΣ",
}

# Παλιοί νομοί που καλύπτουν περισσότερες από μία ΠΕ. Το σκέτο όνομα
# ερμηνεύεται ως νομός (ευρύτερο)· για τη στενή ΠΕ γράψτε «ΠΕ ΚΑΒΑΛΑΣ».
NOMOS_PREFIXES = {
    "ΑΤΤΙΚΗΣ": ["45", "46", "47", "48", "49", "50", "51", "52"],
    "ΚΑΒΑΛΑΣ": ["04", "05"],
    "ΜΑΓΝΗΣΙΑΣ": ["24", "25"],
    "ΚΕΦΑΛΛΗΝΙΑΣ": ["34", "35"],
    "ΛΕΣΒΟΥ": ["53", "55"],
    "ΣΑΜΟΥ": ["54", "56"],
    "ΚΥΚΛΑΔΩΝ": ["58", "59", "60", "63", "65", "66", "67", "68", "70"],
    "ΔΩΔΕΚΑΝΗΣΟΥ": ["61", "62", "64", "69"],
}


def normalize(s):
    """Πεζά/κεφαλαία και τόνοι αδιάφορα: 'Δήμος Δράμας' -> 'ΔΗΜΟΣ ΔΡΑΜΑΣ'."""
    s = unicodedata.normalize("NFD", s)
    s = "".join(c for c in s if not unicodedata.combining(c))
    return " ".join(s.upper().replace("-", " ").split())


def load_kallikratis(cache_dir):
    path = Path(cache_dir) / "kallikratis.json"
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        r = requests.get(DICTIONARY_URL, timeout=30)
        r.raise_for_status()
        path.write_text(r.text, encoding="utf-8")
    items = json.loads(path.read_text(encoding="utf-8"))["items"]
    regions = {i["uid"]: i["label"] for i in items if i["parent"] is None}
    munis = {i["uid"]: i["label"] for i in items if i["parent"] is not None}
    muni_region = {i["uid"]: i["parent"] for i in items if i["parent"] is not None}
    return regions, munis, muni_region


def resolve_area(area, cache_dir):
    """Επιστρέφει (περιγραφή, {κωδικός: όνομα δήμου}) για το ζητούμενο --area.

    Δεκτά: όνομα δήμου («Δήμος Δράμας» ή σκέτο «Δράμας»), νομός/ΠΕ
    («Νομός Δράμας», «ΠΕ Καβάλας»), περιφέρεια («Περιφέρεια Κρήτης»),
    «Ελλάδα»/«ΟΛΗ» για όλη τη χώρα. Πολλές περιοχές χωρισμένες με κόμμα.
    """
    regions, munis, muni_region = load_kallikratis(cache_dir)
    selected = {}
    labels = []

    for part in area.split(","):
        token = normalize(part)
        if not token:
            continue
        if token in ("ΕΛΛΑΔΑ", "ΟΛΗ", "ALL"):
            selected.update(munis)
            labels.append("Ελλάδα")
            continue

        # Περιφέρεια
        bare = token
        for pre in ("ΠΕΡΙΦΕΡΕΙΑ ", "ΠΕΡΙΦΕΡΕΙΑΣ "):
            if bare.startswith(pre):
                bare = bare[len(pre):]
        hit = None
        for uid, label in regions.items():
            nl = normalize(label)
            if nl == token or nl == "ΠΕΡΙΦΕΡΕΙΑ " + bare or nl.endswith(" " + bare) or nl == bare:
                hit = uid
                break
        if hit:
            sub = {u: l for u, l in munis.items() if muni_region[u] == hit}
            selected.update(sub)
            labels.append(regions[hit])
            continue

        # Δήμος
        bare = token
        if bare.startswith("ΔΗΜΟΣ "):
            bare = bare[len("ΔΗΜΟΣ "):]
        muni_hits = [u for u, l in munis.items()
                     if normalize(l) in ("ΔΗΜΟΣ " + bare, bare)]
        if len(muni_hits) == 1:
            selected[muni_hits[0]] = munis[muni_hits[0]]
            labels.append(munis[muni_hits[0]])
            continue
        if len(muni_hits) > 1:
            raise SystemExit(f"Διφορούμενη περιοχή «{part.strip()}»: "
                             + ", ".join(munis[u] for u in muni_hits))

        # Νομός / περιφερειακή ενότητα
        bare = token
        narrow = False
        for pre in ("ΝΟΜΟΣ ", "ΝΟΜΟΥ ", "Ν. "):
            if bare.startswith(pre):
                bare = bare[len(pre):]
        for pre in ("ΠΕ ", "Π.Ε. ", "ΠΕΡΙΦΕΡΕΙΑΚΗ ΕΝΟΤΗΤΑ ", "ΠΕΡΙΦΕΡΕΙΑΚΗΣ ΕΝΟΤΗΤΑΣ "):
            if bare.startswith(pre):
                bare = bare[len(pre):]
                narrow = True
        prefixes = None
        if not narrow and bare in NOMOS_PREFIXES:
            prefixes = NOMOS_PREFIXES[bare]
        else:
            pe = [p for p, name in PREFIX_PE.items() if normalize(name) == bare]
            if pe:
                prefixes = pe
        if prefixes:
            sub = {u: l for u, l in munis.items() if u[:2] in prefixes}
            selected.update(sub)
            labels.append(("ΠΕ " if narrow else "Νομός ") + bare.title())
            continue

        raise SystemExit(
            f"Άγνωστη περιοχή «{part.strip()}». Δεκτά: όνομα δήμου, νομού/ΠΕ, "
            f"περιφέρειας, ή «Ελλάδα»."
        )

    if not selected:
        raise SystemExit("Δεν δόθηκε περιοχή.")
    return ", ".join(labels), selected
