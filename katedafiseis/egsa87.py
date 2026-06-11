"""Μετατροπή συντεταγμένων ΕΓΣΑ87 (GGRS87 / EPSG:2100) σε WGS84.

Τα PDF του e-Άδειες δίνουν το περίγραμμα του κτίσματος σε ΕΓΣΑ87
(Εγκάρσια Μερκατορική: ελλειψοειδές GRS80, λ0=24°, k0=0.9996,
FE=500000). Αντιστροφή προβολής με τις σειρές του Snyder και μετάθεση
datum GGRS87->WGS84 κατά EPSG:1272 (-199.87, +74.79, +246.62 μέτρα
γεωκεντρικά)· ακρίβεια ~1 m, υπεραρκετή για χαρτογράφηση κτισμάτων.
"""

import math

A = 6378137.0                      # GRS80 = WGS84 ημιάξονας
F_GRS80 = 1 / 298.257222101
F_WGS84 = 1 / 298.257223563
K0 = 0.9996
LON0 = math.radians(24.0)
FE = 500000.0
DX, DY, DZ = -199.87, 74.79, 246.62   # EPSG:1272 (GGRS87 -> WGS84)

# λογικά όρια ΕΓΣΑ87 για την Ελλάδα (απόρριψη σκουπιδιών της ανάλυσης PDF)
X_RANGE = (94874, 857398)
Y_RANGE = (3804000, 4655000)


def _tm_inverse(x, y):
    """ΕΓΣΑ87 x/y -> (φ, λ) σε rad πάνω στο GRS80 (datum GGRS87)."""
    e2 = F_GRS80 * (2 - F_GRS80)
    ep2 = e2 / (1 - e2)
    m = y / K0
    mu = m / (A * (1 - e2 / 4 - 3 * e2 ** 2 / 64 - 5 * e2 ** 3 / 256))
    e1 = (1 - math.sqrt(1 - e2)) / (1 + math.sqrt(1 - e2))
    phi1 = (mu
            + (3 * e1 / 2 - 27 * e1 ** 3 / 32) * math.sin(2 * mu)
            + (21 * e1 ** 2 / 16 - 55 * e1 ** 4 / 32) * math.sin(4 * mu)
            + (151 * e1 ** 3 / 96) * math.sin(6 * mu)
            + (1097 * e1 ** 4 / 512) * math.sin(8 * mu))
    sin1, cos1, tan1 = math.sin(phi1), math.cos(phi1), math.tan(phi1)
    c1 = ep2 * cos1 ** 2
    t1 = tan1 ** 2
    n1 = A / math.sqrt(1 - e2 * sin1 ** 2)
    r1 = A * (1 - e2) / (1 - e2 * sin1 ** 2) ** 1.5
    d = (x - FE) / (n1 * K0)
    phi = phi1 - (n1 * tan1 / r1) * (
        d ** 2 / 2
        - (5 + 3 * t1 + 10 * c1 - 4 * c1 ** 2 - 9 * ep2) * d ** 4 / 24
        + (61 + 90 * t1 + 298 * c1 + 45 * t1 ** 2
           - 252 * ep2 - 3 * c1 ** 2) * d ** 6 / 720)
    lam = LON0 + (d
                  - (1 + 2 * t1 + c1) * d ** 3 / 6
                  + (5 - 2 * c1 + 28 * t1 - 3 * c1 ** 2
                     + 8 * ep2 + 24 * t1 ** 2) * d ** 5 / 120) / cos1
    return phi, lam


def _geodetic_to_ecef(phi, lam, f):
    e2 = f * (2 - f)
    n = A / math.sqrt(1 - e2 * math.sin(phi) ** 2)
    return (n * math.cos(phi) * math.cos(lam),
            n * math.cos(phi) * math.sin(lam),
            n * (1 - e2) * math.sin(phi))


def _ecef_to_geodetic(x, y, z, f):
    e2 = f * (2 - f)
    lam = math.atan2(y, x)
    p = math.hypot(x, y)
    phi = math.atan2(z, p * (1 - e2))
    for _ in range(6):
        n = A / math.sqrt(1 - e2 * math.sin(phi) ** 2)
        phi = math.atan2(z + e2 * n * math.sin(phi), p)
    return phi, lam


def egsa87_to_wgs84(x, y):
    """(x, y) ΕΓΣΑ87 -> (lat, lon) WGS84 σε μοίρες, ή None εκτός ορίων."""
    if not (X_RANGE[0] <= x <= X_RANGE[1] and Y_RANGE[0] <= y <= Y_RANGE[1]):
        return None
    phi, lam = _tm_inverse(x, y)
    ex, ey, ez = _geodetic_to_ecef(phi, lam, F_GRS80)
    phi, lam = _ecef_to_geodetic(ex + DX, ey + DY, ez + DZ, F_WGS84)
    return math.degrees(phi), math.degrees(lam)
