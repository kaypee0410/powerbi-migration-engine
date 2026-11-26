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

    # the page's review data: every measure to review sits in one group
    p, s = job["preview"], job["summary"]
    assert sum(len(g["measures"]) for g in p["review"]) == s["measures_review"] > 0
    assert s["measures_translated"] + s["measures_review"] == s["measures"]
    assert p["target_views"] >= 1 and all("steps" in t for t in p["sources"])
    assert job["done"] == []

    # mark as done is saved per run and only for measures on the review list
    name = p["review"][0]["measures"][0]["name"]
    url = f"/api/jobs/{job_id}/done"
    assert client.post(url, json={"measure": name, "done": True}).json["done"] == [name]
    assert client.get(f"/api/jobs/{job_id}").json["done"] == [name]
    assert client.post(url, json={"measure": name, "done": False}).json["done"] == []
    assert client.post(url, json={"measure": "Not a measure", "done": True}).status_code == 400
    assert client.post(url, json={"measure": name}).status_code == 400
    assert client.post("/api/jobs/000000000000/done", json={"measure": name, "done": True}).status_code == 404


def test_page_is_offline(client):
    html = client.get("/").data.decode()
    assert "http://" not in html.replace("http://www.w3.org", "") and "https://" not in html
    css = client.get("/static/css/tokens.css").data.decode()
    assert "googleapis" not in css and "woff2" in css
    assert client.get("/static/fonts/source-sans-3-400.woff2").status_code == 200
