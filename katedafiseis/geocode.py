"""Γεωκωδικοποίηση διευθύνσεων με Nominatim (OpenStreetMap).

Κλιμακωτή ακρίβεια: οδός+αριθμός -> οδός -> οικισμός -> δήμος. Όλα τα
αποτελέσματα (και οι αποτυχίες) κρατιούνται σε cache ώστε επαναλήψεις να
μην ξαναχτυπούν το API (όριο ~1 αίτημα/δευτερόλεπτο).
"""

import json
import math
import re
import time
from pathlib import Path

import requests

NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"
USER_AGENT = "katedafiseis-research/1.0 (ffeizidis@grnet.gr)"
# λογικά όρια Ελλάδας για απόρριψη άσχετων αποτελεσμάτων
LAT_RANGE = (34.5, 42.0)
LON_RANGE = (19.0, 30.1)

session = requests.Session()
session.headers["User-Agent"] = USER_AGENT


def _strip_dimos(label):
    """«Δήμος Δράμας»/«ΔΗΜΟΣ ΔΡΑΜΑΣ» -> «Δράμας»."""
    return re.sub(r"^(ΔΗΜΟΣ|Δήμος)\s+", "", label).title()


class Geocoder:
    def __init__(self, cache_dir):
        self.cache_path = Path(cache_dir) / "geocode.json"
        self.cache = {}
        if self.cache_path.exists():
            self.cache = json.loads(self.cache_path.read_text(encoding="utf-8"))
        self._dirty = 0

    def _save(self):
        self.cache_path.write_text(
            json.dumps(self.cache, ensure_ascii=False), encoding="utf-8"
        )
        self._dirty = 0

    def _query(self, q):
        if q in self.cache:
            return self.cache[q]
        result = None
        try:
            r = session.get(
                NOMINATIM_URL,
                params={"q": q, "format": "jsonv2", "limit": 1,
                        "countrycodes": "gr"},
                timeout=30,
            )
            time.sleep(1.1)
            if r.ok:
                hits = r.json()
                if hits:
                    lat, lon = float(hits[0]["lat"]), float(hits[0]["lon"])
                    if LAT_RANGE[0] <= lat <= LAT_RANGE[1] and \
                       LON_RANGE[0] <= lon <= LON_RANGE[1]:
                        result = [lat, lon]
        except requests.RequestException:
            pass
        self.cache[q] = result
        self._dirty += 1
        if self._dirty >= 20:
            self._save()
        return result

    def geocode_row(self, row, dimos_label):
        """Επιστρέφει (lat, lon, precision) ή (None, None, '')."""
        # «Δήμος Δράμας» -> «Δράμας» για πιο φυσικά queries
        dimos = row.get("dimos_pdf") or _strip_dimos(dimos_label)
        odos, ar, poli = row.get("odos"), row.get("ar_apo"), row.get("poli")
        # «ΔΡΑΜΑ/ΠΡΟΑΣΤΕΙΟ» -> δοκίμασε και σκέτο «ΔΡΑΜΑ»
        polis = [p.strip() for p in dict.fromkeys([poli, poli.split("/")[0]]) if p.strip()]

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
        dimos = _strip_dimos(dimos_label)
        hit = self._query(f"Δήμος {dimos}, Ελλάδα")
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
