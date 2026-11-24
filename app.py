"""Power BI Extraction Insights - local web app.

Run:  python app.py      then open http://127.0.0.1:5000
Upload a .pbix (or .pbit / .bim); the server decodes it in a background thread and writes the migration
workbook (.xlsx) and a JSON export to outputs/<job id>/.
"""
from __future__ import annotations

import gc
import json
import logging
import os
import re
import shutil
import threading
import time
import traceback
import uuid
from pathlib import Path

from flask import Flask, abort, jsonify, render_template, request, send_file
from werkzeug.exceptions import RequestEntityTooLarge
from werkzeug.utils import secure_filename

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

from pbi_insights import __version__
from pbi_insights.extract.package import SUPPORTED_EXTENSIONS
from pbi_insights.pipeline import DIALECTS, STEPS, Options, run

BASE = Path(__file__).resolve().parent
UPLOADS = Path(os.getenv("PBI_UPLOAD_DIR", BASE / "uploads"))
OUTPUTS = Path(os.getenv("PBI_OUTPUT_DIR", BASE / "outputs"))
MAX_MB = int(os.getenv("PBI_MAX_UPLOAD_MB", "1024"))
KEEP_UPLOADS = os.getenv("PBI_KEEP_UPLOADS", "0") == "1"
WORKERS = threading.BoundedSemaphore(int(os.getenv("PBI_WORKERS", "2")))
JOB_ID = re.compile(r"^[a-f0-9]{12}$")

app = Flask(__name__, template_folder="templates", static_folder="static")
app.config["MAX_CONTENT_LENGTH"] = MAX_MB * 1024 * 1024
log = logging.getLogger("pbi_insights")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

JOBS: dict[str, dict] = {}
LOCK = threading.Lock()


# ------------------------------------------------------------------------------------------ helpers
def _job_dir(job_id: str) -> Path:
    if not JOB_ID.match(job_id or ""):
        abort(404)
    return OUTPUTS / job_id


def _update(job_id: str, **kw):
    with LOCK:
        JOBS[job_id].update(kw)


def _preview(data: dict) -> dict:
    """Small, JSON-safe slices of the workbook for the page's preview grid."""
    plans = data["plans"]
    runs = (data.get("checks") or {}).get("test_run") or {}
    views = {v["replaces"]: v["view"] for v in data["views"]}
    graph = data["graph"]

    def role(t):
        if t.get("auto_date"):
            return "Auto date (hidden)"
        if t.get("calculated"):
            return "Calculated table"
        if graph.is_fact(t["name"]):
            return "Fact"
        return "Dimension" if graph.is_dimension(t["name"]) else "Other"

    sources = []
    for t in data["model"].get("tables", []):
        if t.get("auto_date"):
            continue
        s = data["sources"].get(t["name"]) or {}
        m = s.get("m") or {}
        sources.append({"table": t["name"], "role": role(t), "rows": t.get("row_count"),
                        "connector": m.get("connector") or ("DAX" if t.get("calculated") else ""),
                        "objects": ", ".join(m.get("source_objects") or (s.get("sql") or {}).get("objects") or []),
                        "view": views.get(t["name"], "")})
    return {
        "findings": data["findings"],
        "decisions": [list(d) for d in data["decisions"]],
        "insights": [{"id": p["id"], "tab": p["page"], "title": p["title"] or "", "chart": p["generic_type"],
                      "approach": p["approach"], "question": p["question"], "complexity": p["complexity"]}
                     for p in plans],
        "measures": [{"name": m["name"], "scope": m["scope"], "pattern": m["pattern"],
                      "status": m["translation"].status, "used": ", ".join(m["used_directly_in"])}
                     for m in sorted(data["measures"], key=lambda m: ({"Used on report": 0, "Building block": 1}
                                                                      .get(m["scope"], 2), m["id"]))],
        "sources": sources,
        "sql": [{"id": p["id"], "title": p["title"] or "", "approach": p["approach"],
                 "run": (runs.get(p["id"]) or {}).get("status", "Not run"), "rows": (runs.get(p["id"]) or {}).get("rows"),
                 "sql": p["sql"]} for p in plans if p["approach"] != "Static"],
    }


def _worker(job_id: str, upload: Path, options: Options):
    out_dir = OUTPUTS / job_id

    def progress(i, msg):
        with LOCK:
            job = JOBS[job_id]
            job["step"] = max(job["step"], i)
            job["message"] = msg
            job["log"] = (job["log"] + [msg])[-40:]

    with WORKERS:
        _update(job_id, status="running", started=time.time())
        try:
            res = run(upload, out_dir, options, progress)
            meta = {"id": job_id, "file": JOBS[job_id]["file"], "created": JOBS[job_id]["created"],
                    "summary": res.summary, "workbook": res.workbook.name, "json": res.json_path.name,
                    "preview": _preview(res.data)}
            (out_dir / "meta.json").write_text(json.dumps(meta, default=str), encoding="utf-8")
            _update(job_id, status="done", step=len(STEPS), message="Workbook ready", summary=res.summary,
                    preview=meta["preview"], workbook=res.workbook.name, json=res.json_path.name, finished=time.time())
            log.info("job %s done: %s", job_id, res.workbook)
        except Exception as ex:  # noqa: BLE001 - report every failure to the page
            log.error("job %s failed\n%s", job_id, traceback.format_exc())
            _update(job_id, status="error", error=str(ex) or ex.__class__.__name__, finished=time.time())
        finally:
            gc.collect()                      # release the model's file handles before deleting (Windows)
            if not KEEP_UPLOADS:
                shutil.rmtree(upload.parent, ignore_errors=True)


def _public(job: dict) -> dict:
    keys = ("id", "file", "status", "step", "message", "log", "error", "summary", "preview", "created", "options")
    out = {k: job.get(k) for k in keys}
    out["steps"] = STEPS
    if job.get("status") == "done":
        out["downloads"] = {"xlsx": f"/api/jobs/{job['id']}/download/xlsx", "json": f"/api/jobs/{job['id']}/download/json"}
    return out


# ------------------------------------------------------------------------------------------ routes
@app.get("/")
def index():
    return render_template("index.html", dialects=DIALECTS, max_mb=MAX_MB,
                           version=__version__, steps=STEPS, extensions=", ".join(SUPPORTED_EXTENSIONS))


@app.get("/healthz")
def healthz():
    return {"ok": True, "version": __version__}


@app.post("/api/jobs")
def create_job():
    f = request.files.get("file")
    if f is None or not f.filename:
        return jsonify(error="Choose a .pbix, .pbit or .bim file first."), 400
    name = secure_filename(f.filename) or "upload.pbix"
    if Path(name).suffix.lower() not in SUPPORTED_EXTENSIONS:
        return jsonify(error=f"{f.filename} is not a Power BI file. Use a .pbix, .pbit or .bim file."), 400
    dialect = request.form.get("dialect", "auto")
    if dialect not in DIALECTS:
        return jsonify(error=f"Unknown SQL dialect: {dialect}"), 400
    options = Options(dialect=dialect, read_data=request.form.get("read_data") == "on")
    job_id = uuid.uuid4().hex[:12]
    up_dir = UPLOADS / job_id
    up_dir.mkdir(parents=True, exist_ok=True)
    upload = up_dir / name
    f.save(upload)
    with LOCK:
        JOBS[job_id] = {"id": job_id, "file": f.filename, "status": "queued", "step": 0, "message": "Waiting to start",
                        "log": [], "created": time.time(), "options": vars(options).copy()}
    threading.Thread(target=_worker, args=(job_id, upload, options), daemon=True, name=f"job-{job_id}").start()
    return jsonify(id=job_id), 202


@app.get("/api/jobs/<job_id>")
def job_status(job_id):
    with LOCK:
        job = JOBS.get(job_id)
        if job:
            return jsonify(_public(job))
    meta = _job_dir(job_id) / "meta.json"
    if meta.is_file():
        m = json.loads(meta.read_text(encoding="utf-8"))
        return jsonify(_public({**m, "status": "done", "step": len(STEPS), "message": "Workbook ready", "log": []}))
    abort(404)


@app.get("/api/jobs")
def recent_jobs():
    rows = []
    if OUTPUTS.is_dir():
        for meta in OUTPUTS.glob("*/meta.json"):
            try:
                m = json.loads(meta.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            rows.append({"id": m["id"], "file": m["file"], "created": m["created"], "summary": m["summary"],
                         "downloads": {"xlsx": f"/api/jobs/{m['id']}/download/xlsx",
                                       "json": f"/api/jobs/{m['id']}/download/json"}})
    rows.sort(key=lambda r: r["created"], reverse=True)
    return jsonify(rows[:15])


@app.get("/api/jobs/<job_id>/download/<kind>")
def download(job_id, kind):
    folder = _job_dir(job_id)
    pattern = {"xlsx": "*_migration_inventory.xlsx", "json": "*_inventory.json"}.get(kind)
    if pattern is None:
        abort(404)
    hits = sorted(folder.glob(pattern))
    if not hits:
        abort(404)
    return send_file(hits[0], as_attachment=True, download_name=hits[0].name)


@app.errorhandler(RequestEntityTooLarge)
def too_large(_):
    return jsonify(error=f"The file is larger than {MAX_MB} MB. Raise PBI_MAX_UPLOAD_MB in .env to allow it."), 413


if __name__ == "__main__":
    UPLOADS.mkdir(exist_ok=True)
    OUTPUTS.mkdir(exist_ok=True)
    host, port = os.getenv("PBI_HOST", "127.0.0.1"), int(os.getenv("PBI_PORT", "5000"))
    print(f"Power BI Extraction Insights v{__version__}: open http://{host}:{port}")
    app.run(host=host, port=port, debug=False, threaded=True)
