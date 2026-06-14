#!/usr/bin/env python3
"""Web UI για το εργαλείο demolitions.

Τοπικά:  python3 webui.py   (ανοίγει μόνο του τον browser)
Hosted:  gunicorn -w 1 --threads 8 --timeout 0 -b 0.0.0.0:$PORT webui:app

Ένα run τη φορά, σε background thread· ο browser ρωτά την πρόοδο με polling
(/api/status). Τα run αποθηκεύονται μέσω του storage backend (τοπικός δίσκος
ή Cloudflare R2 — βλ. demolitions/storage.py).
"""

import json
import os
import socket
import threading
import time
import traceback
import webbrowser
from datetime import date, datetime
from pathlib import Path
from urllib.parse import quote

from flask import Flask, Response, abort, jsonify, render_template, request

from zipstream import ZipStream

from demolitions.areas import AreaError, list_areas, normalize, resolve_area
from demolitions.diavgeia import session as diavgeia_session
from demolitions.greek import pretty_area
from demolitions.pipeline import (CancelledRun, E_ADEIES_START, NoPermitsFound,
                                  enrich_geocode, run_pipeline)
from demolitions.storage import content_type, make_storage

BASE = Path(__file__).parent
CACHE_DIR = Path(os.environ.get("DEMOLITIONS_CACHE_DIR", BASE / "cache"))
CACHE_DIR.mkdir(parents=True, exist_ok=True)
GITHUB_URL = "https://github.com/ZenFeedbacker/demolitions"
START_TIME = time.time()
RATE_LIMIT_WINDOW_SECONDS = int(
    os.environ.get("DEMOLITIONS_RATE_LIMIT_WINDOW_SECONDS", "60")
)
RATE_LIMIT_MAX_REQUESTS = int(
    os.environ.get("DEMOLITIONS_RATE_LIMIT_MAX_REQUESTS", "12")
)

app = Flask(__name__)
store = make_storage()


class Job:
    def __init__(self):
        self.state = "idle"   # idle|running|geocoding|done|error|cancelled
        self.log = []
        self.phase = ""
        self.i = self.n = 0
        self.error = None
        self.result = None
        self.run_id = None
        self.meta = {}        # area/from/to για το ιστορικό όσο τρέχει
        self.cancel_event = threading.Event()

    def append(self, msg):
        self.log.append(msg)

    def step(self, phase, i, n):
        self.phase, self.i, self.n = phase, i, n


job = Job()
lock = threading.Lock()
rate_lock = threading.Lock()
rate_hits = {}


def _slug(label, from_date, to_date):
    name = normalize(label).lower().replace(" ", "-").replace(",", "")
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    return f"{name}_{from_date}_{to_date}_{stamp}"


def _result_payload(run_id):
    manifest = store.read_manifest(run_id)
    manifest.pop("_mtime", None)
    manifest["xlsx_url"] = f"/runs/{run_id}/{run_id}.xlsx"
    manifest["zip_url"] = f"/zip/{run_id}.zip"
    return manifest


def _client_ip():
    forwarded = request.headers.get("X-Forwarded-For", "")
    if forwarded:
        return forwarded.split(",", 1)[0].strip()
    return request.remote_addr or "unknown"


def _check_rate_limit(action):
    if RATE_LIMIT_MAX_REQUESTS <= 0 or RATE_LIMIT_WINDOW_SECONDS <= 0:
        return None
    now = time.time()
    cutoff = now - RATE_LIMIT_WINDOW_SECONDS
    key = (_client_ip(), action)
    with rate_lock:
        # καθάρισε εγγραφές που έληξαν εντελώς ώστε το dict να μη μεγαλώνει
        # απεριόριστα με IP που δεν ξαναεμφανίζονται
        for k in [k for k, ts in rate_hits.items() if not ts or ts[-1] <= cutoff]:
            if k != key:
                del rate_hits[k]
        hits = [t for t in rate_hits.get(key, []) if t > cutoff]
        if len(hits) >= RATE_LIMIT_MAX_REQUESTS:
            rate_hits[key] = hits
            retry_after = max(1, int(hits[0] + RATE_LIMIT_WINDOW_SECONDS - now))
            return retry_after
        hits.append(now)
        rate_hits[key] = hits
    return None


@app.before_request
def limit_expensive_routes():
    action = {
        "api_run": "run",
        "api_geocode": "geocode",
        "serve_zip": "zip",
    }.get(request.endpoint)
    if not action:
        return None
    retry_after = _check_rate_limit(action)
    if retry_after is None:
        return None
    headers = {"Retry-After": str(retry_after)}
    if request.endpoint == "serve_zip":
        return Response("Rate limit exceeded.", status=429, headers=headers)
    return jsonify({
        "error": "Πάρα πολλά αιτήματα από την ίδια διεύθυνση. Δοκιμάστε ξανά σε λίγο."
    }), 429, headers


def _run_worker(j, area, from_date, to_date, run_id):
    try:
        staging = store.staging_dir(run_id)
        run_pipeline(area, from_date, to_date, staging, cache_dir=CACHE_DIR,
                     log=j.append, step=j.step, cancel=j.cancel_event.is_set)
        j.append("Αποθήκευση αρχείων…")
        store.save_run(run_id, progress=lambda i, n: j.step("upload", i, n))
        store.free_local_pdfs(run_id)
        j.result = _result_payload(run_id)
        j.state = "geocoding"
        _geocode_worker(j, run_id, staging)
    except CancelledRun:
        j.append("Ακυρώθηκε.")
        j.state = "cancelled"
        store.cleanup(run_id)
    except (AreaError, NoPermitsFound) as e:
        j.error = str(e)
        j.state = "error"
        store.cleanup(run_id)
    except Exception as e:
        j.append(traceback.format_exc())
        j.error = f"Σφάλμα: {e}"
        j.state = "error"
        store.cleanup(run_id)


def _geocode_worker(j, run_id, staging=None):
    try:
        if staging is None:                       # κρύα εκκίνηση (παλιό run)
            staging = store.prepare_staging(run_id)
        enrich_geocode(staging, cache_dir=CACHE_DIR, log=j.append,
                       step=j.step, cancel=j.cancel_event.is_set)
        store.save_meta(run_id)   # μόνο json/xlsx — τα PDF ανέβηκαν ήδη
        store.enforce_pdf_cap(log=j.append)
        j.result = _result_payload(run_id)
        j.state = "done"
    except CancelledRun:
        store.save_meta(run_id)   # τα μερικά αποτελέσματα σώζονται (geocoded=false)
        j.append("Η γεωκωδικοποίηση ακυρώθηκε — οι μερικές συντεταγμένες σώθηκαν.")
        j.result = _result_payload(run_id)
        j.state = "done"
    except Exception as e:
        j.append(traceback.format_exc())
        j.error = f"Σφάλμα γεωκωδικοποίησης: {e}"
        j.state = "error"
    finally:
        store.cleanup(run_id)


def _start(target, *args, state="running", result=None, run_id=None, meta=None):
    """Ξεκινά νέο job αν δεν τρέχει ήδη κάποιο. Επιστρέφει True/False.

    Ο worker δουλεύει πάνω στο instance `j` που του περνιέται (όχι στο global
    `job`), ώστε ένα μεταγενέστερο run να μην μπορεί ποτέ να αλλοιώσει την
    κατάσταση ενός άλλου."""
    global job
    with lock:
        if job.state in ("running", "geocoding"):
            return False
        job = Job()
        job.state = state
        job.result = result
        job.run_id = run_id
        job.meta = meta or {}
        threading.Thread(target=target, args=(job,) + args, daemon=True).start()
        return True


@app.get("/healthz")
def healthz():
    return "ok", 200


@app.get("/")
def index():
    return render_template("index.html", today=date.today().isoformat(),
                           start=E_ADEIES_START.isoformat())


@app.get("/api/about")
def api_about():
    runs = store.list_runs()
    u = store.usage()
    return jsonify({
        "backend": store.kind,
        "n_runs": len(runs),
        "n_permits": sum(m.get("n_rows") or 0 for m in runs),
        "storage_bytes": u["storage_bytes"],
        "pdf_bytes": u["pdf_bytes"],
        "pdf_cap": store.pdf_cap,
        "uptime_seconds": int(time.time() - START_TIME),
        "version": (os.environ.get("RENDER_GIT_COMMIT") or "")[:7],
        "github": GITHUB_URL,
        "data_since": E_ADEIES_START.isoformat(),
    })


@app.get("/api/areas")
def api_areas():
    return jsonify(list_areas(CACHE_DIR))


@app.post("/api/run")
def api_run():
    data = request.get_json(force=True)
    try:
        from_date = date.fromisoformat(data.get("from", ""))
        to_date = date.fromisoformat(data.get("to", ""))
    except ValueError:
        return jsonify({"error": "Μη έγκυρη ημερομηνία."}), 400
    if from_date > to_date:
        return jsonify({"error": "Η αρχή του διαστήματος είναι μετά το τέλος του."}), 400
    area = (data.get("area") or "").strip()
    try:
        label, _ = resolve_area(area, CACHE_DIR)
    except AreaError as e:
        return jsonify({"error": str(e)}), 400
    run_id = _slug(label, from_date, to_date)
    meta = {"area": pretty_area(label),
            "from": from_date.isoformat(), "to": to_date.isoformat()}
    if not _start(_run_worker, area, from_date, to_date, run_id,
                  run_id=run_id, meta=meta):
        return jsonify({"error": "Εκτελείται ήδη αναζήτηση."}), 409
    return jsonify({"ok": True, "run_id": run_id})


@app.post("/api/geocode/<run_id>")
def api_geocode(run_id):
    if not store.exists(run_id):
        abort(404)
    if not _start(_geocode_worker, run_id, state="geocoding",
                  result=_result_payload(run_id), run_id=run_id):
        return jsonify({"error": "Εκτελείται ήδη αναζήτηση."}), 409
    return jsonify({"ok": True})


@app.get("/api/status")
def api_status():
    since = request.args.get("since", 0, type=int)
    return jsonify({
        "state": job.state,
        "log": job.log[since:],
        "cursor": len(job.log),
        "phase": job.phase, "i": job.i, "n": job.n,
        "error": job.error,
        "result": job.result,
    })


@app.post("/api/cancel")
def api_cancel():
    with lock:
        j = job
    j.cancel_event.set()
    return jsonify({"ok": True})


@app.get("/api/runs")
def api_runs():
    runs = store.list_runs()
    sizes = store.sizes_by_run()
    for m in runs:
        m.pop("_mtime", None)
        s = sizes.get(m["run_id"], {})
        m["total_bytes"] = s.get("total_bytes", 0)
        m["pdf_bytes"] = s.get("pdf_bytes", 0)
    active_id = job.run_id if job.state in ("running", "geocoding") else None
    if active_id:
        for m in runs:
            if m["run_id"] == active_id:
                m["active"] = job.state
                break
        else:  # δεν έχει γραφτεί ακόμη run.json (φάση αναζήτησης/PDF)
            runs.insert(0, {"run_id": active_id, **job.meta, "n_rows": None,
                            "geocoded": False, "active": job.state,
                            "created": date.today().isoformat()})
        runs.sort(key=lambda m: 0 if m.get("active") else 1)
    return jsonify(runs)


@app.delete("/api/runs")
def api_delete_all():
    """Διαγράφει όλα τα run (εκτός από αυτό που τυχόν εκτελείται τώρα)."""
    with lock:
        active = job.run_id if job.state in ("running", "geocoding") else None
    deleted = 0
    for m in store.list_runs():
        if m["run_id"] == active:
            continue
        store.delete_run(m["run_id"])
        deleted += 1
    return jsonify({"ok": True, "deleted": deleted})


@app.delete("/api/pdfs")
def api_delete_all_pdfs():
    """Καθαρίζει τα PDF όλων των run (κρατά μεταδεδομένα/xlsx) — ελευθερώνει χώρο."""
    with lock:
        active = job.run_id if job.state in ("running", "geocoding") else None
    cleared = 0
    for m in store.list_runs():
        if m["run_id"] == active or not m.get("has_pdfs"):
            continue
        store.delete_pdfs(m["run_id"])
        m.pop("_mtime", None)
        m["has_pdfs"] = False
        store.write_manifest(m["run_id"], m)
        cleared += 1
    return jsonify({"ok": True, "cleared": cleared})


@app.delete("/api/runs/<run_id>")
def api_delete_run(run_id):
    with lock:
        if job.state in ("running", "geocoding") and job.run_id == run_id:
            return jsonify({"error": "Το run εκτελείται αυτή τη στιγμή."}), 409
    if not store.exists(run_id):
        abort(404)
    store.delete_run(run_id)
    return jsonify({"ok": True})


@app.delete("/api/runs/<run_id>/pdfs")
def api_delete_pdfs(run_id):
    """Σβήνει μόνο τα PDF του run (κρατά μεταδεδομένα/xlsx) — ελευθερώνει χώρο."""
    with lock:
        if job.state in ("running", "geocoding") and job.run_id == run_id:
            return jsonify({"error": "Το run εκτελείται αυτή τη στιγμή."}), 409
    if not store.exists(run_id):
        abort(404)
    store.delete_pdfs(run_id)
    m = store.read_manifest(run_id)
    m.pop("_mtime", None)
    m["has_pdfs"] = False
    store.write_manifest(run_id, m)
    return jsonify({"ok": True})


@app.get("/api/runs/<run_id>/rows")
def api_rows(run_id):
    member = store.open_member(run_id, "rows.json")
    if not member:
        abort(404)
    gen, _ = member
    return Response(gen, mimetype="application/json")


@app.get("/runs/<run_id>/<path:filename>")
def serve_run_file(run_id, filename):
    member = store.open_member(run_id, filename)
    if not member:
        abort(404)
    gen, size = member
    headers = {"Content-Length": str(size)}
    if filename.endswith(".xlsx"):
        headers["Content-Disposition"] = _attachment(filename.rsplit("/", 1)[-1])
    return Response(gen, mimetype=content_type(filename), headers=headers)


def _attachment(name):
    """Content-Disposition με RFC 5987 (τα headers δέχονται μόνο latin-1)."""
    return "attachment; filename*=UTF-8''" + quote(name)


def _diavgeia_pdf(ada):
    """Streamάρει το PDF μιας πράξης από τη Διαύγεια (για zip χωρίς cache)."""
    url = f"https://diavgeia.gov.gr/doc/{ada}"
    try:
        with diavgeia_session.get(url, timeout=120, stream=True) as r:
            if not r.ok:
                return
            for chunk in r.iter_content(65536):
                yield chunk
    except Exception:
        return


@app.get("/zip/<run_id>.zip")
def serve_zip(run_id):
    if not store.exists(run_id):
        abort(404)
    manifest = store.read_manifest(run_id)
    rows_member = store.open_member(run_id, "rows.json")
    rows = json.loads(b"".join(rows_member[0])) if rows_member else []

    zs = ZipStream()
    xlsx = f"{run_id}.xlsx"
    xm = store.open_member(run_id, xlsx)
    if xm:
        zs.add(data=xm[0], arcname=xlsx)
    cached = manifest.get("has_pdfs")
    for r in rows:
        arc = r.get("pdf_path")
        if not arc:
            continue
        if cached:                                  # από την αποθήκη (γρήγορο)
            m = store.open_member(run_id, arc)
            if m:
                zs.add(data=m[0], arcname=arc)
        elif r.get("ada"):                           # κατ' απαίτηση από Διαύγεια
            zs.add(data=_diavgeia_pdf(r["ada"]), arcname=arc)
    return Response(zs, mimetype="application/zip",
                    headers={"Content-Disposition": _attachment(f"{run_id}.zip")})


def main():
    port = None
    for p in range(8741, 8761):
        with socket.socket() as s:
            try:
                s.bind(("127.0.0.1", p))
                port = p
                break
            except OSError:
                continue
    url = f"http://127.0.0.1:{port}/"
    print(f"demolitions web UI: {url}  (Ctrl-C για τερματισμό)")
    threading.Timer(1.0, webbrowser.open, [url]).start()
    app.run(host="127.0.0.1", port=port, threaded=True, debug=False)


if __name__ == "__main__":
    main()
