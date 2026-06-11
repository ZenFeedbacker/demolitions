#!/usr/bin/env python3
"""Τοπικό web UI για το εργαλείο κατεδαφίσεων.

Εκκίνηση:  python3 webui.py   (ανοίγει μόνο του τον browser)

Ένα run τη φορά, σε background thread· ο browser ρωτά την πρόοδο με
polling (/api/status). Κάθε run αποθηκεύεται στο runs/<id>/ μαζί με
run.json (μεταδεδομένα) και rows.json (για τον πίνακα και το ιστορικό).
"""

import json
import shutil
import socket
import threading
import traceback
import webbrowser
from datetime import date, datetime
from pathlib import Path

from flask import Flask, abort, jsonify, render_template, request, send_from_directory

from katedafiseis.areas import AreaError, list_areas, normalize, resolve_area
from katedafiseis.greek import pretty_area
from katedafiseis.pipeline import (CancelledRun, NoPermitsFound,
                                   enrich_geocode, run_pipeline)

BASE = Path(__file__).parent
RUNS_DIR = BASE / "runs"
CACHE_DIR = BASE / "cache"

app = Flask(__name__)


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


def _slug(label, from_date, to_date):
    name = normalize(label).lower().replace(" ", "-").replace(",", "")
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    return f"{name}_{from_date}_{to_date}_{stamp}"


def _zip_run(run_id):
    job.append("Δημιουργία zip…")
    shutil.make_archive(str(RUNS_DIR / run_id), "zip",
                        root_dir=RUNS_DIR, base_dir=run_id)


def _result_payload(run_id):
    manifest = json.loads(
        (RUNS_DIR / run_id / "run.json").read_text(encoding="utf-8"))
    manifest["xlsx_url"] = f"/runs/{run_id}/{run_id}.xlsx"
    manifest["zip_url"] = f"/zip/{run_id}.zip"
    return manifest


def _run_worker(area, from_date, to_date, run_id):
    try:
        run_pipeline(area, from_date, to_date, RUNS_DIR / run_id,
                     cache_dir=CACHE_DIR, log=job.append, step=job.step,
                     cancel=job.cancel_event.is_set)
        _zip_run(run_id)
        job.result = _result_payload(run_id)
        job.state = "geocoding"
        _geocode_worker(run_id)
    except CancelledRun:
        job.append("Ακυρώθηκε.")
        job.state = "cancelled"
    except (AreaError, NoPermitsFound) as e:
        job.error = str(e)
        job.state = "error"
    except Exception as e:
        job.append(traceback.format_exc())
        job.error = f"Σφάλμα: {e}"
        job.state = "error"


def _geocode_worker(run_id):
    try:
        enrich_geocode(RUNS_DIR / run_id, cache_dir=CACHE_DIR,
                       log=job.append, step=job.step,
                       cancel=job.cancel_event.is_set)
        _zip_run(run_id)
        job.result = _result_payload(run_id)
        job.state = "done"
    except CancelledRun:
        # τα μερικά αποτελέσματα έχουν σωθεί (geocoded=false)
        job.append("Η γεωκωδικοποίηση ακυρώθηκε — οι μερικές συντεταγμένες σώθηκαν.")
        _zip_run(run_id)
        job.result = _result_payload(run_id)
        job.state = "done"
    except Exception as e:
        job.append(traceback.format_exc())
        job.error = f"Σφάλμα γεωκωδικοποίησης: {e}"
        job.state = "error"


def _start(target, *args, state="running", result=None, run_id=None, meta=None):
    """Ξεκινά νέο job αν δεν τρέχει ήδη κάποιο. Επιστρέφει True/False."""
    global job
    with lock:
        if job.state in ("running", "geocoding"):
            return False
        job = Job()
        job.state = state
        job.result = result
        job.run_id = run_id
        job.meta = meta or {}
        threading.Thread(target=target, args=args, daemon=True).start()
        return True


@app.get("/")
def index():
    return render_template("index.html", today=date.today().isoformat())


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
    _safe_run_dir(run_id)
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
    job.cancel_event.set()
    return jsonify({"ok": True})


@app.get("/api/runs")
def api_runs():
    runs = []
    if RUNS_DIR.exists():
        for manifest_path in RUNS_DIR.glob("*/run.json"):
            m = _result_payload(manifest_path.parent.name)
            m["mtime"] = manifest_path.stat().st_mtime
            runs.append(m)
    runs.sort(key=lambda m: m["mtime"], reverse=True)
    # τρέχον job: σήμανση στην υπάρχουσα εγγραφή ή συνθετική αν δεν έχει
    # γραφτεί ακόμη run.json (φάση αναζήτησης/PDF)
    active_id = job.run_id if job.state in ("running", "geocoding") else None
    if active_id:
        for m in runs:
            if m["run_id"] == active_id:
                m["active"] = job.state
                break
        else:
            runs.insert(0, {"run_id": active_id, **job.meta, "n_rows": None,
                            "geocoded": False, "active": job.state,
                            "created": date.today().isoformat()})
        runs.sort(key=lambda m: 0 if m.get("active") else 1)
    return jsonify(runs)


@app.delete("/api/runs/<run_id>")
def api_delete_run(run_id):
    run_dir = _safe_run_dir(run_id)
    with lock:
        if job.state in ("running", "geocoding") and job.run_id == run_id:
            return jsonify({"error": "Το run εκτελείται αυτή τη στιγμή."}), 409
        shutil.rmtree(run_dir)
        (RUNS_DIR / f"{run_id}.zip").unlink(missing_ok=True)
    return jsonify({"ok": True})


@app.get("/api/runs/<run_id>/rows")
def api_rows(run_id):
    return send_from_directory(_safe_run_dir(run_id), "rows.json")


@app.get("/runs/<run_id>/<path:filename>")
def serve_run_file(run_id, filename):
    return send_from_directory(_safe_run_dir(run_id), filename)


@app.get("/zip/<run_id>.zip")
def serve_zip(run_id):
    _safe_run_dir(run_id)
    return send_from_directory(RUNS_DIR, f"{run_id}.zip")


def _safe_run_dir(run_id):
    run_dir = (RUNS_DIR / run_id).resolve()
    if not (run_dir.is_relative_to(RUNS_DIR.resolve())
            and (run_dir / "run.json").exists()):
        abort(404)
    return run_dir


def main():
    RUNS_DIR.mkdir(exist_ok=True)
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
    print(f"Κατεδαφίσεις web UI: {url}  (Ctrl-C για τερματισμό)")
    threading.Timer(1.0, webbrowser.open, [url]).start()
    app.run(host="127.0.0.1", port=port, threaded=True, debug=False)


if __name__ == "__main__":
    main()
