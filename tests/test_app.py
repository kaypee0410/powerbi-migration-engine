import io
import time

import pytest


@pytest.fixture()
def client(tmp_path, monkeypatch):
    import app as webapp

    monkeypatch.setattr(webapp, "UPLOADS", tmp_path / "uploads")
    monkeypatch.setattr(webapp, "OUTPUTS", tmp_path / "outputs")
    webapp.app.config["TESTING"] = True
    return webapp.app.test_client()


def test_page_and_health(client):
    r = client.get("/")
    assert r.status_code == 200 and b"Power BI Extraction Insights" in r.data
    assert client.get("/healthz").json["ok"] is True


def test_rejects_missing_and_wrong_files(client):
    assert client.post("/api/jobs", data={}).status_code == 400
    r = client.post("/api/jobs", data={"file": (io.BytesIO(b"x"), "notes.txt")})
    assert r.status_code == 400 and "not a Power BI file" in r.json["error"]


def test_job_runs_and_downloads(client, pbit):
    with open(pbit, "rb") as fh:
        r = client.post("/api/jobs", data={"file": (fh, "synthetic.pbit"), "dialect": "auto"},
                        content_type="multipart/form-data")
    assert r.status_code == 202
    job_id = r.json["id"]
    for _ in range(200):
        job = client.get(f"/api/jobs/{job_id}").json
        if job["status"] in ("done", "error"):
            break
        time.sleep(0.05)
    assert job["status"] == "done", job.get("error")
    assert len(job["preview"]["insights"]) == 6
    x = client.get(job["downloads"]["xlsx"])
    assert x.status_code == 200 and x.data[:2] == b"PK"
    assert client.get(f"/api/jobs/{job_id}/download/exe").status_code == 404
    assert client.get("/api/jobs/not-a-job/download/xlsx").status_code == 404
