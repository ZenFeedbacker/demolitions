"""Αναζήτηση αδειών κατεδάφισης στη Διαύγεια (opendata API).

Όλες οι οικοδομικές πράξεις του e-Άδειες δημοσιεύονται από το ΤΕΕ
(organizationUid 99201077, decisionType 2.4.6.1). Το API δέχεται παράθυρο
issueDate έως 6 μήνες, οπότε τεμαχίζουμε το ζητούμενο διάστημα. Το πεδίο
extraFieldValues.municipality (κωδικός Καλλικράτη) δεν είναι αναζητήσιμο
server-side, οπότε το φιλτράρουμε client-side.
"""

import json
import re
import time
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

from .areas import normalize

SEARCH_URL = "https://diavgeia.gov.gr/opendata/search/advanced"
TEE_ORG = "99201077"
PAGE_SIZE = 500
SEARCH_CACHE_VERSION = "v2"

KIND_KATEDAFISI = "κατεδάφιση"
KIND_OIKODOMIKI = "οικοδομική με κατεδάφιση"
DEMOLITION_RE = re.compile(r"\bΚΑΤΕΔΑΦΙΣ(?:Η|ΗΣ)\b")
GREECE_TZ = ZoneInfo("Europe/Athens")


def permit_kind(subject):
    """Είδος τελικής άδειας που αφορά κατεδάφιση, αλλιώς None.

    «Άδεια Κατεδάφισης…» = αυτοτελής άδεια· «Οικοδομική Άδεια…» που
    αναφέρει κατεδάφιση στην περιγραφή = ενιαία άδεια (συνήθως
    κατεδάφιση-και-ανέγερση). Προεγκρίσεις/αναθεωρήσεις/ενημερώσεις
    αποκλείονται και στις δύο περιπτώσεις (το startswith τις κόβει).
    """
    subj = normalize(subject)
    description = subj.split(":", 1)[1].strip() if ":" in subj else subj
    if subj.startswith("ΑΔΕΙΑ ΚΑΤΕΔΑΦΙΣΗΣ"):
        return KIND_KATEDAFISI
    if subj.startswith("ΟΙΚΟΔΟΜΙΚΗ ΑΔΕΙΑ") and DEMOLITION_RE.search(description):
        return KIND_OIKODOMIKI
    return None

session = requests.Session()
session.headers["User-Agent"] = "demolitions-research/1.0 (ffeizidis@grnet.gr)"


def _windows(start, end):
    """Σπάει το [start, end] σε διαστήματα <= ~6 μηνών (180 ημέρες)."""
    cur = start
    while cur <= end:
        win_end = min(cur + timedelta(days=179), end)
        yield cur, win_end
        cur = win_end + timedelta(days=1)


def _search_query(win_start, win_end):
    """Broad retrieval; precise permit filtering happens in permit_kind()."""
    return (
        f'organizationUid:"{TEE_ORG}" AND decisionTypeUid:"2.4.6.1" '
        f'AND subject:"Κατεδάφιση" '
        f"AND issueDate:[DT({win_start}T00:00:00) TO DT({win_end}T23:59:59)]"
    )


def _fetch_window(win_start, win_end, cache_dir):
    """Όλες οι σελίδες ενός παραθύρου, με cache στο δίσκο."""
    cache = Path(cache_dir) / "search"
    cache.mkdir(parents=True, exist_ok=True)
    decisions = []
    page = 0
    while True:
        cache_file = cache / (
            f"{SEARCH_CACHE_VERSION}_{win_start}_{win_end}_p{page}.json"
        )
        if cache_file.exists():
            data = json.loads(cache_file.read_text(encoding="utf-8"))
        else:
            q = _search_query(win_start, win_end)
            for attempt in range(4):
                try:
                    r = session.get(
                        SEARCH_URL,
                        params={"q": q, "page": page, "size": PAGE_SIZE},
                        timeout=60,
                    )
                    r.raise_for_status()
                    data = r.json()
                    if "info" not in data:
                        raise ValueError(f"API error: {str(data)[:300]}")
                    break
                except (requests.RequestException, ValueError):
                    if attempt == 3:
                        raise
                    time.sleep(2 ** attempt)
            cache_file.write_text(
                json.dumps(data, ensure_ascii=False), encoding="utf-8"
            )
            time.sleep(0.3)
        decisions.extend(data["decisions"])
        if data["info"]["actualSize"] < PAGE_SIZE:
            break
        page += 1
    return decisions


def search_permits(from_date, to_date, muni_codes, cache_dir, progress=print):
    """Τελικές «Άδειες Κατεδάφισης» για τους δοθέντες κωδικούς δήμων.

    Επιστρέφει λίστα από metadata dicts της Διαύγειας, χωρίς διπλότυπα ΑΔΑ,
    ταξινομημένα κατά ημερομηνία έκδοσης.
    """
    seen = {}
    for win_start, win_end in _windows(from_date, to_date):
        decisions = _fetch_window(win_start, win_end, cache_dir)
        kept = 0
        for d in decisions:
            # το stemming επιστρέφει και Προεγκρίσεις/Αναθεωρήσεις/Ενημερώσεις
            if not permit_kind(d.get("subject", "")):
                continue
            muni = (d.get("extraFieldValues") or {}).get("municipality")
            if muni not in muni_codes:
                continue
            if d["ada"] not in seen:
                seen[d["ada"]] = d
                kept += 1
        progress(
            f"  {win_start:%d/%m/%Y} – {win_end:%d/%m/%Y}: {len(decisions)} "
            f"πράξεις στη χώρα, {kept} άδειες κατεδάφισης στην περιοχή"
        )
    return sorted(seen.values(), key=lambda d: d["issueDate"])


def issue_date(decision):
    """epoch ms -> datetime.date"""
    return datetime.fromtimestamp(decision["issueDate"] / 1000, GREECE_TZ).date()
