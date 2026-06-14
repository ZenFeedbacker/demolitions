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
        la1, lo1, la2, lo2 = map(math.radians,
                                 (row["lat"], row["lon"], hit[0], hit[1]))
        h = (math.sin((la2 - la1) / 2) ** 2
             + math.cos(la1) * math.cos(la2) * math.sin((lo2 - lo1) / 2) ** 2)
        return 6371 * 2 * math.asin(math.sqrt(h))

    def close(self):
        if self._dirty:
            self._save()
