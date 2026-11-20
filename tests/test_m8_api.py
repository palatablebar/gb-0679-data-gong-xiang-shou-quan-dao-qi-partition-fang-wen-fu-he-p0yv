import csv
import io


def test_admission_preview_and_ingest(client):
    preview = client.get("/api/admissions")
    assert preview.status_code == 200
    assert len(preview.get_json()["queries"]) == 4

    resp = client.post("/api/admissions", json={"sample": "admissions.csv"})
    assert resp.status_code == 200
    run = resp.get_json()
    assert run["review"] == "待复核" and run["version"] == 1


def test_retry_is_idempotent_over_http(client):
    first = client.post("/api/admissions", json={"sample": "admissions.csv"}).get_json()
    second = client.post("/api/admissions", json={"sample": "admissions.csv"}).get_json()
    assert first["run_id"] == second["run_id"]
    history = client.get("/api/admissions/runs").get_json()["runs"]
    assert len(history) == 1 and history[0]["version"] == 1


def test_review_stale_keeps_old_conclusion(client):
    run = client.post("/api/admissions", json={"sample": "admissions.csv"}).get_json()
    url = f"/api/admissions/runs/{run['run_id']}/review"
    assert client.post(url, json={"version": 99, "decision": "封存"}).status_code == 409
    kept = client.get(f"/api/admissions/runs/{run['run_id']}").get_json()
    assert kept["version"] == 1 and kept["review"] == "待复核"
    journal = client.get(f"/api/admissions/runs/{run['run_id']}/journal").get_json()["journal"]
    assert any(e["event"] == "SWITCH_FAILED" for e in journal)


def test_seal_gate_and_evidence_share_basis(client):
    run = client.post("/api/admissions", json={"sample": "admissions.csv"}).get_json()
    review = client.post(
        f"/api/admissions/runs/{run['run_id']}/review",
        json={"version": 1, "decision": "封存"},
    )
    assert review.status_code == 200 and review.get_json()["sealed"] is True

    evidence = client.get(f"/api/admissions/runs/{run['run_id']}/evidence")
    assert evidence.status_code == 200 and evidence.mimetype == "text/csv"
    rows = list(csv.reader(io.StringIO(evidence.data.decode("utf-8-sig"))))
    header = rows[0]
    i_qid, i_basis = header.index("query_id"), header.index("basis_sha256")
    basis = {q["query_id"]: q["basis_sha256"] for q in run["queries"]}
    exported = {r[i_qid]: r[i_basis] for r in rows[1:] if r[i_qid] in basis and r[i_basis]}
    assert exported == basis  # 证据导出引用裁决同一组依据


def test_full_reject_run_cannot_be_sealed(client):
    payload = {
        "csv": "query_id,principal,resource,partition,submitted_at,rows,note\n"
               "Q-9,guest,orders,2026-09,2026-11-16T00:00:00Z,5,无授权\n"
    }
    run = client.post("/api/admissions", json=payload).get_json()
    assert run["metrics"]["sealable"] is False
    resp = client.post(
        f"/api/admissions/runs/{run['run_id']}/review",
        json={"version": 1, "decision": "封存"},
    )
    assert resp.status_code == 422 and resp.get_json()["code"] == "NOT_SEALABLE"


def test_get_preview_does_not_persist(client):
    assert client.get("/api/admissions").status_code == 200
    assert client.get("/api/admissions/runs").get_json()["runs"] == []


def test_evidence_available_before_seal_but_seal_gated(client):
    payload = {
        "csv": "query_id,principal,resource,partition,submitted_at,rows,note\n"
               "Q-9,guest,orders,2026-09,2026-11-16T00:00:00Z,5,无授权\n"
    }
    run = client.post("/api/admissions", json=payload).get_json()
    # 未封存也能导出证据，但封存动作被闸门拒绝
    assert client.get(f"/api/admissions/runs/{run['run_id']}/evidence").status_code == 200
    resp = client.post(
        f"/api/admissions/runs/{run['run_id']}/review",
        json={"version": 1, "decision": "封存"},
    )
    assert resp.status_code == 422 and resp.get_json()["code"] == "NOT_SEALABLE"
    kept = client.get(f"/api/admissions/runs/{run['run_id']}").get_json()
    assert kept["review"] == "待复核" and kept["sealed"] is False


def test_query_detail_and_missing_404(client):
    run = client.post("/api/admissions", json={"sample": "admissions.csv"}).get_json()
    key = run["queries"][0]["query_key"]
    ok = client.get(f"/api/admissions/queries/{key}")
    assert ok.status_code == 200 and ok.get_json()["query_id"] == run["queries"][0]["query_id"]
    assert client.get("/api/admissions/queries/nope").status_code == 404
    assert client.get("/api/admissions/runs/nope").status_code == 404


def test_admission_page_and_navigation(client):
    page = client.get("/admissions")
    assert page.status_code == 200
    assert "查询准入裁决" in page.data.decode()
    # 原有 7 个模块页面不受影响
    for slug in ["accesses", "resources", "grants", "conflicts", "partitions", "audit", "seals"]:
        assert client.get("/" + slug).status_code == 200


def test_upload_multipart_query_csv(client):
    data = {
        "file": (
            io.BytesIO(
                b"query_id,principal,resource,partition,submitted_at,rows,note\n"
                b"Q-7,auditor,customers,2026-05,2026-11-16T00:00:00Z,7,\n"
            ),
            "q.csv",
        )
    }
    resp = client.post("/api/admissions/upload", data=data, content_type="multipart/form-data")
    assert resp.status_code == 200
    assert resp.get_json()["queries"][0]["conclusion"] == "整单放行"
