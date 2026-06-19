"""Γεωκωδικοποίηση διευθύνσεων με Nominatim (OpenStreetMap).

Κλιμακωτή ακρίβεια: οδός+αριθμός -> οδός -> οικισμός -> δήμος. Τα θετικά
αποτελέσματα και τα οριστικά «δεν βρέθηκε» κρατιούνται σε cache ώστε
επαναλήψεις να μην ξαναχτυπούν το API (όριο ~1 αίτημα/δευτερόλεπτο).
"""

import json
import math
import re
import time
from pathlib import Path

import requests

from .areas import normalize

NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"
USER_AGENT = "demolitions-research/1.0 (ffeizidis@grnet.gr)"
# λογικά όρια Ελλάδας για απόρριψη άσχετων αποτελεσμάτων
LAT_RANGE = (34.5, 42.0)
LON_RANGE = (19.0, 30.1)

session = requests.Session()
session.headers["User-Agent"] = USER_AGENT

# ακρίβεια geocoding αρκετή για γεωγραφικό έλεγχο θέσης
_HIGH_PREC = frozenset(("κτίσμα (PDF)", "οδός+αριθμός", "οδός", "οικισμός"))


def _haversine(la1, lo1, la2, lo2):
    la1, lo1, la2, lo2 = map(math.radians, (la1, lo1, la2, lo2))
    h = (math.sin((la2 - la1) / 2) ** 2
         + math.cos(la1) * math.cos(la2) * math.sin((lo2 - lo1) / 2) ** 2)
    return 6371 * 2 * math.asin(math.sqrt(h))


def _strip_dimos(label):
    """«Δήμος Δράμας»/«ΔΗΜΟΣ ΔΡΑΜΑΣ» -> «Δράμας»."""
    label = re.sub(r"^(ΔΗΜΟΣ|Δήμος)\s+", "", label)
    label = re.sub(r"\s*\([^)]+\)\s*$", "", label)
    return label.title()


def _poli_variants(poli, dimos):
    """Παραλλαγές του «Πόλη/Οικισμός» για τα queries.

    Το πεδίο συχνά γράφεται περιγραφικά («Οικισμός Ποταμιας Θάσου»,
    «ΔΡΑΜΑ/ΠΡΟΑΣΤΕΙΟ») και δεν ταιριάζει με το OSM· δοκιμάζουμε πρώτα
    καθαρισμένες μορφές (χωρίς «Οικισμός», χωρίς το όνομα του δήμου στο
    τέλος) — το σκέτο όνομα οικισμού είναι ό,τι ξέρει το OSM, ενώ η
    περιγραφική μορφή πιάνει εύκολα λάθος αποτέλεσμα (π.χ. δρόμο).
    """
    out = []
    for part in [poli] + poli.split("/"):
        part = part.strip()
        if not part:
            continue
        variants = [part]
        words = part.split()
        if words and normalize(words[0]) in ("ΟΙΚΙΣΜΟΣ", "ΣΥΝΟΙΚΙΣΜΟΣ",
                                             "ΟΙΚ.", "ΠΕΡΙΟΧΗ", "ΘΕΣΗ"):
            words = words[1:]
            variants.append(" ".join(words))
        if len(words) > 1 and normalize(words[-1]) == normalize(dimos):
            variants.append(" ".join(words[:-1]))
        out.extend(v for v in reversed(variants) if v and v not in out)
    return out


class Geocoder:
    def __init__(self, cache_dir):
        self.cache_path = Path(cache_dir) / "geocode.json"
        self.cache = {}
        if self.cache_path.exists():
            raw = json.loads(self.cache_path.read_text(encoding="utf-8"))
            # παλιό format: q -> [lat, lon] / None. Τα legacy None μπορεί να
            # κρύβουν παροδικές αστοχίες, οπότε τα ξαναδοκιμάζουμε.
            for q, value in raw.items():
                if isinstance(value, list):
                    self.cache[q] = {"status": "hit", "result": value}
                elif isinstance(value, dict) and value.get("status") in ("hit", "miss"):
                    self.cache[q] = value
        self._dirty = 0

    def _save(self):
        self.cache_path.write_text(
            json.dumps(self.cache, ensure_ascii=False), encoding="utf-8"
        )
        self._dirty = 0

    def _cache_get(self, q):
        cached = self.cache.get(q)
        if not cached:
            return None, False
        return cached.get("result"), True

    def _cache_put(self, q, result, *, status):
        self.cache[q] = {"status": status, "result": result}
        self._dirty += 1
        if self._dirty >= 20:
            self._save()

    def _query(self, q):
        cached, found = self._cache_get(q)
        if found:
            return cached
        for attempt in range(4):
            try:
                r = session.get(
                    NOMINATIM_URL,
                    params={"q": q, "format": "jsonv2", "limit": 1,
                            "countrycodes": "gr"},
                    timeout=30,
                )
            except requests.RequestException:
                if attempt == 3:
                    return None
                time.sleep(2 ** attempt)
                continue
            time.sleep(1.1)
            if r.ok:
                hits = r.json()
                if hits:
                    lat, lon = float(hits[0]["lat"]), float(hits[0]["lon"])
                    if LAT_RANGE[0] <= lat <= LAT_RANGE[1] and \
                       LON_RANGE[0] <= lon <= LON_RANGE[1]:
                        result = [lat, lon]
                        self._cache_put(q, result, status="hit")
                        return result
                self._cache_put(q, None, status="miss")
                return None
            if r.status_code in (429, 500, 502, 503, 504):
                if attempt == 3:
                    return None
                time.sleep(2 ** attempt)
                continue
            return None
        return None

    def geocode_row(self, row, dimos_label):
        """Επιστρέφει (lat, lon, precision) ή (None, None, '')."""
        # «Δήμος Δράμας» -> «Δράμας» για πιο φυσικά queries
        dimos = row.get("dimos_pdf") or _strip_dimos(dimos_label)
        odos, ar, poli = row.get("odos"), row.get("ar_apo"), row.get("poli")
        # «Ο.Τ. 53» στο πεδίο οδού = οικοδομικό τετράγωνο, όχι οδός·
        # δεν υπάρχει στο OSM — πέφτουμε κατευθείαν στον οικισμό
        if odos and re.search(r"(^|\s)Ο\.?\s*Τ\.?(\s|\d|$)|ΟΙΚΟΔΟΜΙΚ",
                              normalize(odos)):
            odos = ""
        polis = _poli_variants(poli, dimos)

        tiers = []
        for p in polis:
            if odos and ar:
                tiers.append((f"{odos} {ar}, {p}, Ελλάδα", "οδός+αριθμός"))
        for p in polis:
            if odos:
                tiers.append((f"{odos}, {p}, Ελλάδα", "οδός"))
        for p in polis:
            tiers.append((f"{p}, Δήμος {dimos}, Ελλάδα", "οικισμός"))
            tiers.append((f"{p}, Ελλάδα", "οικισμός"))
        tiers.append((f"Δήμος {dimos}, Ελλάδα", "δήμος"))

        for q, precision in tiers:
            hit = self._query(q)
            if hit:
                return hit[0], hit[1], precision
        return None, None, ""

    def dimos_distance_km(self, row, dimos_label):
        """Απόσταση σημείου από το κέντρο του δήμου των μεταδεδομένων.

        Πιάνει λάθος χρεώσεις δήμου στο e-Άδειες (π.χ. άδειες του Ηρακλείου
        Κρήτης δηλωμένες στον ομώνυμο δήμο της Αττικής).
        """
        if not row.get("lat"):
            return None
        query = row.get("dimos_query") or f"Δήμος {_strip_dimos(dimos_label)}"
        hit = self._query(f"{query}, Ελλάδα")
        if not hit:
            return None
        return _haversine(row["lat"], row["lon"], hit[0], hit[1])

    def close(self):
        if self._dirty:
            self._save()


def rows_centroid_bbox(rows, margin=0.5):
    """Ευρωστο bbox (lat_min, lat_max, lon_min, lon_max) από γεωκωδικοποιημένες
    εγγραφές. Αποκλείει outliers με τον κανόνα 1.5×IQR Tukey: αν π.χ. υπάρχουν
    μερικές άδειες Κρήτης μέσα σε αναζήτηση Αττικής, η IQR τις αποκλείει και
    το bbox παραμένει αττικοκεντρικό. Απαιτεί ≥5 σημεία υψηλής ακρίβειας.

    Το αποτέλεσμα χρησιμοποιείται σε δεύτερο πέρασμα του enrich_geocode για να
    εντοπιστούν εγγραφές γεωγραφικά εκτός της ζητούμενης περιοχής — χωρίς
    επιπλέον κλήσεις Nominatim και ανεξάρτητα από ομωνυμίες δήμων."""
    pts = [(r["lat"], r["lon"]) for r in rows
           if r.get("lat") and r.get("precision") in _HIGH_PREC]
    if len(pts) < 5:
        return None
    lats = sorted(p[0] for p in pts)
    lons = sorted(p[1] for p in pts)
    n = len(lats)
    lat_q1, lat_q3 = lats[n // 4], lats[3 * n // 4]
    lon_q1, lon_q3 = lons[n // 4], lons[3 * n // 4]
    lat_iqr = max(lat_q3 - lat_q1, 0.01)
    lon_iqr = max(lon_q3 - lon_q1, 0.01)
    core = [(la, lo) for la, lo in pts
            if (lat_q1 - 1.5 * lat_iqr <= la <= lat_q3 + 1.5 * lat_iqr
                and lon_q1 - 1.5 * lon_iqr <= lo <= lon_q3 + 1.5 * lon_iqr)]
    if not core:
        return None
    lats = [p[0] for p in core]
    lons = [p[1] for p in core]
    return (min(lats) - margin, max(lats) + margin,
            min(lons) - margin, max(lons) + margin)


def area_flag(row, bbox):
    """Flag «εκτός περιοχής αναζήτησης» αν η εγγραφή βρίσκεται εκτός bbox.

    Επιστρέφει string ή None. Αγνοεί εγγραφές χωρίς αξιόπιστες συντεταγμένες
    (precision «δήμος» ή καθόλου geocoding) — μόνο «κτίσμα», «οδός» κ.λπ."""
    if bbox is None or not row.get("lat"):
        return None
    if row.get("precision") not in _HIGH_PREC:
        return None
    lat, lon = row["lat"], row["lon"]
    lat_min, lat_max, lon_min, lon_max = bbox
    if lat_min <= lat <= lat_max and lon_min <= lon <= lon_max:
        return None
    center_lat = (lat_min + lat_max) / 2
    center_lon = (lon_min + lon_max) / 2
    dist = _haversine(lat, lon, center_lat, center_lon)
    return f"~{dist:.0f}km εκτός περιοχής αναζήτησης"
