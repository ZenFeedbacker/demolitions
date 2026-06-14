"""Αποθήκευση των run (μεταδεδομένα + xlsx + PDF) — τοπικά ή σε Cloudflare R2.

Το `pipeline.run_pipeline` γράφει πάντα σε έναν τοπικό φάκελο (staging). Το
backend αναλαμβάνει τη μονιμότητα:

- `LocalStorage` — ο staging φάκελος *είναι* ο μόνιμος (runs/<id>/), όπως πριν.
- `R2Storage` — ανεβάζει τα αρχεία ως αντικείμενα `runs/<id>/<relpath>` σε bucket
  S3-συμβατό (Cloudflare R2). Ο δίσκος του host είναι εφήμερος, οπότε η μνήμη
  του ιστορικού ζει στο R2.

Επιλογή με env: `DEMOLITIONS_STORAGE=local|r2`. Όριο cache για PDF:
`DEMOLITIONS_PDF_CACHE_LIMIT` (bytes, default 1 GB) — όταν το συνολικό μέγεθος
των PDF ξεπεράσει το όριο, σβήνονται τα PDF των παλαιότερων run (τα
μεταδεδομένα μένουν· το zip τους ξαναφτιάχνεται κατ' απαίτηση από τη Διαύγεια).
"""

import json
import os
import shutil
import tempfile
from pathlib import Path

CONTENT_TYPES = {
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ".json": "application/json; charset=utf-8",
    ".pdf": "application/pdf",
    ".zip": "application/zip",
}
DEFAULT_PDF_CAP = 1_000_000_000  # 1 GB


def content_type(name):
    return CONTENT_TYPES.get(Path(name).suffix.lower(), "application/octet-stream")


def make_storage():
    """Backend από τα env vars (default: τοπικό runs/ δίπλα στο webui)."""
    kind = os.environ.get("DEMOLITIONS_STORAGE", "local").lower()
    cap = int(os.environ.get("DEMOLITIONS_PDF_CACHE_LIMIT", DEFAULT_PDF_CAP))
    if kind == "r2":
        return R2Storage(
            bucket=os.environ["R2_BUCKET"],
            account_id=os.environ["R2_ACCOUNT_ID"],
            access_key=os.environ["R2_ACCESS_KEY_ID"],
            secret_key=os.environ["R2_SECRET_ACCESS_KEY"],
            endpoint_url=os.environ.get("R2_ENDPOINT"),
            pdf_cap=cap,
        )
    runs_dir = Path(os.environ.get("DEMOLITIONS_RUNS_DIR",
                                   Path(__file__).parent.parent / "runs"))
    return LocalStorage(runs_dir, pdf_cap=cap)


# --------------------------------------------------------------------------- #

class LocalStorage:
    """Μόνιμη αποθήκευση στον δίσκο (runs/<id>/). Συμπεριφορά όπως πριν."""

    kind = "local"

    def __init__(self, runs_dir, pdf_cap=DEFAULT_PDF_CAP):
        self.runs = Path(runs_dir)
        self.runs.mkdir(parents=True, exist_ok=True)
        self.pdf_cap = pdf_cap

    # staging == ο μόνιμος φάκελος
    def staging_dir(self, run_id):
        return self.runs / run_id

    def prepare_staging(self, run_id):
        return self.runs / run_id

    def save_run(self, run_id, progress=None):
        pass  # ήδη γραμμένο στη θέση του

    def save_meta(self, run_id):
        pass  # τα json/xlsx είναι ήδη στον δίσκο

    def free_local_pdfs(self, run_id):
        pass  # τοπικά τα PDF ΕΙΝΑΙ η αποθήκη — δεν τα σβήνουμε

    def cleanup(self, run_id):
        pass

    def _dir(self, run_id):
        d = (self.runs / run_id).resolve()
        if not d.is_relative_to(self.runs.resolve()):
            raise KeyError(run_id)
        return d

    def exists(self, run_id):
        try:
            return (self._dir(run_id) / "run.json").exists()
        except KeyError:
            return False

    def read_manifest(self, run_id):
        return json.loads((self._dir(run_id) / "run.json").read_text("utf-8"))

    def write_manifest(self, run_id, manifest):
        (self._dir(run_id) / "run.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=1), "utf-8")

    def open_member(self, run_id, relpath):
        """(iterable bytes, size) ή None — με προστασία traversal."""
        base = self._dir(run_id)
        p = (base / relpath).resolve()
        if not (p.is_relative_to(base) and p.is_file()):
            return None
        return _file_chunks(p), p.stat().st_size

    def list_runs(self):
        out = []
        for mf in self.runs.glob("*/run.json"):
            m = json.loads(mf.read_text("utf-8"))
            m["_mtime"] = mf.stat().st_mtime
            out.append(m)
        out.sort(key=lambda m: m["_mtime"], reverse=True)
        return out

    def iter_pdfs(self, run_id):
        pdf_root = self._dir(run_id) / "pdf"
        if not pdf_root.is_dir():
            return
        for p in pdf_root.rglob("*.pdf"):
            yield str(p.relative_to(self._dir(run_id))), p.stat().st_size

    def delete_pdfs(self, run_id):
        shutil.rmtree(self._dir(run_id) / "pdf", ignore_errors=True)

    def delete_run(self, run_id):
        shutil.rmtree(self._dir(run_id), ignore_errors=True)

    def enforce_pdf_cap(self, log=lambda m: None):
        pass  # τοπικά δεν περιορίζουμε (ο δίσκος είναι ο δίσκος του χρήστη)

    def usage(self):
        total = pdf = 0
        for p in self.runs.rglob("*"):
            if p.is_file():
                s = p.stat().st_size
                total += s
                if p.suffix == ".pdf":
                    pdf += s
        return {"storage_bytes": total, "pdf_bytes": pdf}


# --------------------------------------------------------------------------- #

class R2Storage:
    """Cloudflare R2 (S3-συμβατό) μέσω boto3. Κλειδιά: runs/<id>/<relpath>."""

    kind = "r2"

    def __init__(self, bucket, account_id, access_key, secret_key,
                 endpoint_url=None, pdf_cap=DEFAULT_PDF_CAP):
        import boto3  # lazy: μόνο το hosted χρειάζεται boto3
        from botocore.config import Config
        self.bucket = bucket
        self.pdf_cap = pdf_cap
        self.s3 = boto3.client(
            "s3",
            endpoint_url=endpoint_url
            or f"https://{account_id}.r2.cloudflarestorage.com",
            aws_access_key_id=access_key,
            aws_secret_access_key=secret_key,
            region_name="auto",
            config=Config(signature_version="s3v4"),
        )
        self._staging = Path(tempfile.gettempdir()) / "demolitions_staging"

    def _key(self, run_id, relpath):
        return f"runs/{run_id}/{relpath}"

    def staging_dir(self, run_id):
        d = self._staging / run_id
        d.mkdir(parents=True, exist_ok=True)
        return d

    def prepare_staging(self, run_id):
        """Κατεβάζει τα json (όχι τα PDF) σε staging για enrich_geocode."""
        d = self.staging_dir(run_id)
        for name in ("rows.json", "run.json"):
            self.s3.download_file(self.bucket, self._key(run_id, name),
                                  str(d / name))
        return d

    META_FILES = ("rows.json", "run.json")

    def _upload(self, run_id, rel):
        self.s3.upload_file(
            str(self.staging_dir(run_id) / rel), self.bucket,
            self._key(run_id, rel),
            ExtraArgs={"ContentType": content_type(rel)})

    def save_run(self, run_id, progress=None):
        """Ανεβάζει όλο τον staging φάκελο στο R2 (αρχικό ανέβασμα)."""
        d = self.staging_dir(run_id)
        files = [str(p.relative_to(d)) for p in d.rglob("*") if p.is_file()]
        for i, rel in enumerate(files, 1):
            self._upload(run_id, rel)
            if progress:
                progress(i, len(files))

    def save_meta(self, run_id):
        """Ανεβάζει μόνο τα μικρά αρχεία (μετά τη γεωκωδικοποίηση) — όχι ξανά
        τα PDF (αποφυγή διπλού ανεβάσματος δεκάδων MB)."""
        d = self.staging_dir(run_id)
        for rel in self.META_FILES + (run_id + ".xlsx",):
            if (d / rel).is_file():
                self._upload(run_id, rel)

    def free_local_pdfs(self, run_id):
        """Σβήνει τα τοπικά PDF του staging μετά το ανέβασμα (τα PDF είναι
        ασφαλή στο R2)· μειώνει τη χρήση του εφήμερου δίσκου κατά τη
        γεωκωδικοποίηση."""
        shutil.rmtree(self.staging_dir(run_id) / "pdf", ignore_errors=True)

    def cleanup(self, run_id):
        shutil.rmtree(self._staging / run_id, ignore_errors=True)

    def exists(self, run_id):
        try:
            self.s3.head_object(Bucket=self.bucket,
                                Key=self._key(run_id, "run.json"))
            return True
        except Exception:
            return False

    def read_manifest(self, run_id):
        obj = self.s3.get_object(Bucket=self.bucket,
                                 Key=self._key(run_id, "run.json"))
        return json.loads(obj["Body"].read())

    def write_manifest(self, run_id, manifest):
        self.s3.put_object(
            Bucket=self.bucket, Key=self._key(run_id, "run.json"),
            Body=json.dumps(manifest, ensure_ascii=False, indent=1).encode("utf-8"),
            ContentType=content_type("run.json"))

    def open_member(self, run_id, relpath):
        try:
            obj = self.s3.get_object(Bucket=self.bucket,
                                     Key=self._key(run_id, relpath))
        except Exception:
            return None
        body = obj["Body"]
        return _iter_body(body), obj["ContentLength"]

    def list_runs(self):
        out = []
        paginator = self.s3.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=self.bucket, Prefix="runs/"):
            for o in page.get("Contents", []):
                if o["Key"].endswith("/run.json"):
                    m = self.read_manifest(o["Key"].split("/")[1])
                    m["_mtime"] = o["LastModified"].timestamp()
                    out.append(m)
        out.sort(key=lambda m: m["_mtime"], reverse=True)
        return out

    def iter_pdfs(self, run_id):
        paginator = self.s3.get_paginator("list_objects_v2")
        prefix = self._key(run_id, "pdf/")
        for page in paginator.paginate(Bucket=self.bucket, Prefix=prefix):
            for o in page.get("Contents", []):
                rel = o["Key"][len(f"runs/{run_id}/"):]
                yield rel, o["Size"]

    def _delete_keys(self, keys):
        for i in range(0, len(keys), 1000):
            self.s3.delete_objects(
                Bucket=self.bucket,
                Delete={"Objects": [{"Key": k} for k in keys[i:i + 1000]]})

    def delete_pdfs(self, run_id):
        keys = [self._key(run_id, rel) for rel, _ in self.iter_pdfs(run_id)]
        if keys:
            self._delete_keys(keys)

    def delete_run(self, run_id):
        paginator = self.s3.get_paginator("list_objects_v2")
        keys = []
        for page in paginator.paginate(Bucket=self.bucket,
                                       Prefix=f"runs/{run_id}/"):
            keys += [o["Key"] for o in page.get("Contents", [])]
        if keys:
            self._delete_keys(keys)

    def enforce_pdf_cap(self, log=lambda m: None):
        total = 0
        for m in self.list_runs():           # newest -> oldest
            if not m.get("has_pdfs"):
                continue
            run_id = m["run_id"]
            size = sum(sz for _, sz in self.iter_pdfs(run_id))
            if total + size <= self.pdf_cap:
                total += size
            else:
                self.delete_pdfs(run_id)
                m.pop("_mtime", None)
                m["has_pdfs"] = False
                self.write_manifest(run_id, m)
                log(f"Εκκαθάριση PDF παλαιότερου run: {run_id}")

    def usage(self):
        total = pdf = 0
        paginator = self.s3.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=self.bucket, Prefix="runs/"):
            for o in page.get("Contents", []):
                total += o["Size"]
                if "/pdf/" in o["Key"]:
                    pdf += o["Size"]
        return {"storage_bytes": total, "pdf_bytes": pdf}


# --------------------------------------------------------------------------- #

def _file_chunks(path, chunk=65536):
    with open(path, "rb") as f:
        while True:
            b = f.read(chunk)
            if not b:
                break
            yield b


def _iter_body(body, chunk=65536):
    try:
        while True:
            b = body.read(chunk)
            if not b:
                break
            yield b
    finally:
        body.close()
