import csv
import hashlib
import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from io import BytesIO, StringIO
from zipfile import ZipFile

from fastapi.testclient import TestClient

from app.main import create_app


def run(client, sample="dirty"):
    return client.post("/api/batches", json={"sample": sample})


def read_csv(bundle, name, key):
    return {row[key]: row for row in csv.DictReader(StringIO(bundle.read(name).decode("utf-8-sig")))}


def test_full_flow_from_unarchived_preview_to_standalone_export(client, settings):
    page = client.get("/")
    assert page.status_code == 200
    assert all(word in page.text for word in ("运行样例", "重跑", "导出复核包", "暂停滚动", "左右轮载"))
    assert client.get("/api/workbench").json()["manifest"] is None
    response = run(client)
    assert response.status_code == 201
    manifest = response.json()["manifest"]
    run_id = manifest["run_id"]
    assert manifest["ledger"]["wheel_kg"] == "53623.03"
    assert manifest["revision"] == 1
    assert manifest["supersedes"] is None
    assert manifest["revision_reason"] == "INITIAL"
    assert manifest["files"]["triggers"]["sha256"] == hashlib.sha256(
        (settings.source_dir / "dirty-triggers.csv").read_bytes(),
    ).hexdigest()
    response = client.get(f"/api/batches/{run_id}/export")
    assert response.headers["Content-Type"] == "application/zip"
    assert response.headers["Content-Disposition"] == f'attachment; filename="axle-{run_id[:12]}.zip"'
    with ZipFile(BytesIO(response.content)) as bundle:
        assert set(bundle.namelist()) == {
            "运行清单.json", "触发复核.csv", "整车勾稽.csv", "轴组轮载.csv",
            "标定证书.csv", "交接采信.csv", "修订明细.csv",
        }
        assert json.loads(bundle.read("运行清单.json")) == manifest
        votes = read_csv(bundle, "整车勾稽.csv", "车次")
        assert votes["0007"]["差额kg"] == "50.00"
        assert votes["0012"]["差额kg"] == "-3033.02"
        assert votes["0023"]["状态"] == "待复秤"
        axes = read_csv(bundle, "轴组轮载.csv", "轴号")
        assert axes["0007/A03"]["偏载率%"] == "20.00"
        assert (axes["0012/A03"]["左kg"], axes["0012/A03"]["右kg"], axes["0012/A03"]["右标定证书"]) == (
            "2000.00", "2033.02", "R-借表",
        )
        handovers = read_csv(bundle, "交接采信.csv", "记录号")
        assert handovers["0012-4R"]["采信证书"] == "R-借表"
        assert handovers["0012-4R"]["被覆盖证书"] == "certificates.csv:4"
        certificates = read_csv(bundle, "标定证书.csv", "证书")
        assert certificates["R-借表"]["增益"] == "1.003"
        issues = list(csv.DictReader(StringIO(bundle.read("触发复核.csv").decode("utf-8-sig"))))
        assert not [row for row in issues if row["原因码"] == "CAL_OVERLAP"]
        assert len(issues) == 28


def test_idempotent_post_reuses_manifest_and_results(client):
    first = run(client).json()["manifest"]
    second = run(client)
    assert second.status_code == 200
    assert second.json() == {"manifest": first, "reused": True}
    assert client.get("/api/batches").json()["batches"] == [first]
    assert client.get(f"/api/batches/{first['run_id']}/lineage").json() == {"lineage": [first]}


def test_exact_replay_is_new_log_event_with_same_export_bytes(client):
    first = run(client).json()["manifest"]
    run_path = f"/api/batches/{first['run_id']}"
    before = client.get(f"{run_path}/export").content
    cursor = client.get("/api/batches/events").json()["cursor"]
    outcome = client.post(f"{run_path}/rerun")
    assert outcome.json() == {"manifest": first, "reused": True, "replayed": True}
    assert client.get(f"{run_path}/export").content == before
    assert client.get("/api/batches").json()["batches"] == [first]
    rows = client.get(f"/api/batches/events?after={cursor}").json()["events"]
    assert [(row["seq"], row["code"]) for row in rows] == [(cursor + 1, "REPLAYED")]


def test_changed_source_rejects_rerun_without_rewriting_old_snapshot(client, settings):
    original = run(client).json()["manifest"]
    run_id = original["run_id"]
    source = settings.source_dir / "dirty-triggers.csv"
    source.write_bytes(source.read_bytes() + b"\r\n")
    reply = client.post(f"/api/batches/{run_id}/rerun")
    assert (reply.status_code, reply.json()["error"]["code"]) == (409, "SOURCE_CHANGED")
    assert client.get("/api/batches").json()["batches"] == [original]
    new = run(client).json()["manifest"]
    assert new["run_id"] != run_id
    assert new["ledger"] == original["ledger"]
    assert (new["revision"], new["supersedes"], new["lineage_root"]) == (
        2, run_id, run_id,
    )
    assert new["revision_reason"] == "SOURCE_UPDATE"
    assert client.get(f"/api/batches/{run_id}").json()["manifest"] == original
    assert client.get(f"/api/batches/{run_id}/export").content == client.get(
        f"/api/batches/{run_id}/export",
    ).content


def test_fatal_source_rolls_back_everything_and_can_recover(client, settings):
    path = settings.source_dir / "dirty-tickets.csv"
    original = path.read_bytes()
    path.write_bytes(original + b'"cut off\n')
    broken = run(client)
    assert (broken.status_code, broken.json()["error"]["code"]) == (422, "CSV_SYNTAX")
    assert client.get("/api/batches").json()["batches"] == []
    assert client.get("/api/batches/events").json() == {"events": [], "cursor": 0}
    path.write_bytes(original)
    assert run(client).status_code == 201


def test_database_error_after_snapshot_insert_rolls_back_the_entire_batch(client, settings):
    with closing(sqlite3.connect(settings.db_file)) as db:
        db.executescript("""
            CREATE TRIGGER deny_log BEFORE INSERT ON events
            BEGIN SELECT RAISE(ABORT, 'receipt write failed'); END;
        """)
    with TestClient(client.app, raise_server_exceptions=False) as tolerant:
        assert run(tolerant).status_code == 500
    assert client.get("/api/batches").json()["batches"] == []
    assert client.get("/api/batches/events").json()["events"] == []


def test_concurrent_runs_commit_once(client):
    with ThreadPoolExecutor(max_workers=4) as workers:
        results = list(workers.map(lambda _: run(client), range(4)))
    assert sorted(result.status_code for result in results) == [200, 200, 200, 201]
    ids = {result.json()["manifest"]["run_id"] for result in results}
    assert len(ids) == 1
    codes = [event["code"] for event in client.get("/api/batches/events").json()["events"]]
    assert (codes.count("COMMITTED"), codes.count("REUSED")) == (1, 3)


def test_events_use_cursor_without_reordering_or_duplicates(client):
    manifest = run(client).json()["manifest"]
    base = "/api/batches/events"
    first = client.get(f"{base}?limit=5").json()
    second = client.get(f"{base}?after={first['cursor']}&limit=5").json()
    assert [event["seq"] for event in first["events"] + second["events"]] == list(range(1, 11))
    assert first["events"][0]["code"] == "START"
    axle = next(event for event in second["events"] if event["code"] == "AXLE")
    assert axle["source_time"].startswith("2026-11-20T12:00:")
    assert axle["run_id"] == manifest["run_id"]


def test_archive_survives_app_restart_and_supports_export_without_sources(client, settings):
    manifest = run(client).json()["manifest"]
    run_id = manifest["run_id"]
    export = client.get(f"/api/batches/{run_id}/export").content
    (settings.source_dir / "dirty-triggers.csv").unlink()
    with TestClient(create_app(settings)) as restarted:
        assert restarted.get(f"/api/batches/{run_id}").json()["manifest"] == manifest
        assert restarted.get(f"/api/batches/{run_id}/export").content == export
        assert restarted.get(f"/api/axles?run_id={run_id}").json()["data"]["groups"] == 9
        assert restarted.post(f"/api/batches/{run_id}/rerun").json()["error"]["code"] == "SOURCE_MISSING"


def test_independent_module_entrypoints_return_sample_evidence(client):
    expected = {
        "intake": ("raw", 50), "calibration": ("certificates", 4), "debounce": ("duplicates", 3),
        "axles": ("groups", 9), "balance": ("rows", 12), "reconcile": ("rows", 7),
    }
    for stage, (key, count) in expected.items():
        response = client.get(f"/api/{stage}?sample=dirty")
        assert response.status_code == 200
        actual = response.json()["data"][key]
        assert (len(actual) if isinstance(actual, list) else actual) == count
    assert client.get("/api/batches").json() == {"batches": []}


def test_early_stages_do_not_require_unused_sources(client, settings):
    (settings.source_dir / "dirty-tickets.csv").unlink()
    assert client.get("/api/balance").json()["data"]["counts"]["severe"] == 1
    assert client.get("/api/reconcile").json()["error"]["file"] == "dirty-tickets.csv"
    (settings.source_dir / "certificates.csv").unlink()
    assert client.get("/api/intake").json()["data"]["raw"] == 50
    assert client.get("/api/calibration").json()["error"]["file"] == "certificates.csv"


def test_invalid_sample_export_id_and_log_limits_fail_explicitly(client):
    reply = run(client, "absent")
    assert (reply.status_code, reply.json()["error"]["code"]) == (404, "SAMPLE_UNKNOWN")
    for path in ("/api/batches/absent/export", "/api/intake?run_id=absent"):
        response = client.get(path)
        assert (response.status_code, response.json()["error"]["code"]) == (404, "RUN_UNKNOWN")
    assert client.get("/api/batches/events?limit=0").status_code == 422


def test_late_certificate_forms_identifiable_revision_and_keeps_old_conclusion(client, settings):
    from conftest import append_late_certificate

    first = run(client).json()["manifest"]
    first_export = client.get(f"/api/batches/{first['run_id']}/export").content
    append_late_certificate(settings.source_dir)
    second = run(client).json()["manifest"]
    assert (second["revision"], second["supersedes"], second["lineage_root"]) == (
        2, first["run_id"], first["run_id"],
    )
    assert second["revision_reason"] == "CERTIFICATE_LATE"
    detail = second["revision_detail"]
    assert [item["id"] for item in detail["late_certificates"]] == ["R-补录"]
    assert detail["changed_roles"] == ["标定证书"]
    recalculated = {item["trigger"]: item for item in detail["recalculated_wheels"]}
    assert recalculated["0012-4R"]["before_cert"] == "R-借表"
    assert recalculated["0012-4R"]["cert"] == "R-补录"
    assert (recalculated["0012-4R"]["before_kg"], recalculated["0012-4R"]["after_kg"]) == (
        "2033.02", "2055.05",
    )
    differences = {item["pass_id"]: item for item in detail["recalculated_differences"]}
    assert (differences["0023"]["before_difference_kg"], differences["0023"]["after_difference_kg"]) == (
        "0.00", "-58.05",
    )
    assert second["ledger"]["wheel_kg"] != first["ledger"]["wheel_kg"]

    # 旧修订逐字保留：清单、结果与导出字节都不变。
    assert client.get(f"/api/batches/{first['run_id']}").json()["manifest"] == first
    assert client.get(f"/api/batches/{first['run_id']}/export").content == first_export
    lineage = client.get(f"/api/batches/{second['run_id']}/lineage").json()["lineage"]
    assert [(item["revision"], item["run_id"]) for item in lineage] == [
        (1, first["run_id"]), (2, second["run_id"]),
    ]

    # 修订明细可从复核包中辨认，并给出新证书的原始参数。
    with ZipFile(BytesIO(client.get(f"/api/batches/{second['run_id']}/export").content)) as bundle:
        revision = list(csv.DictReader(StringIO(bundle.read("修订明细.csv").decode("utf-8-sig"))))
        assert ("迟到证书", "R-补录", "ADDED", "", "", "", "certificates.csv:6") in [
            tuple(row.values()) for row in revision
        ]
        assert ("轮载重算", "0012-4R", "2033.02", "2055.05", "R-借表", "R-补录", "dirty-triggers.csv:23") in [
            tuple(row.values()) for row in revision
        ]
        assert ("差额重算", "0023", "4400.00", "4458.05", "0.00", "-58.05", "review") in [
            tuple(row.values()) for row in revision
        ]
        certificates = read_csv(bundle, "标定证书.csv", "证书")
        assert (certificates["R-补录"]["增益"], certificates["R-补录"]["偏置千克"]) == ("1.01", "30")
        axes = read_csv(bundle, "轴组轮载.csv", "轴号")
        assert axes["0012/A03"]["右标定证书"] == "R-补录"
        assert axes["0012/A03"]["右kg"] == "2055.05"


def test_identical_evidence_does_not_double_book_revision(client):
    first = run(client).json()["manifest"]
    again = run(client)
    assert again.status_code == 200
    assert again.json()["manifest"] == first
    assert [item["revision"] for item in client.get("/api/batches").json()["batches"]] == [1]


def test_failed_revision_keeps_previous_version_and_is_retryable(client, settings):
    first = run(client).json()["manifest"]
    from conftest import append_late_certificate

    append_late_certificate(settings.source_dir)
    path = settings.source_dir / "dirty-tickets.csv"
    original = path.read_bytes()
    path.write_bytes(original + b'"cut off\n')
    broken = run(client)
    assert (broken.status_code, broken.json()["error"]["code"]) == (422, "CSV_SYNTAX")
    assert [item["run_id"] for item in client.get("/api/batches").json()["batches"]] == [first["run_id"]]
    assert client.get(f"/api/batches/{first['run_id']}").json()["manifest"] == first
    path.write_bytes(original)
    second = run(client).json()["manifest"]
    assert (second["revision"], second["supersedes"]) == (2, first["run_id"])


def test_revision_base_must_be_latest_and_known(client, settings):
    from conftest import append_late_certificate

    unknown = client.post("/api/batches", json={"sample": "dirty", "base_run_id": "absent"})
    assert (unknown.status_code, unknown.json()["error"]["code"]) == (404, "REVISION_BASE")
    first = run(client).json()["manifest"]
    cross_sample = client.post("/api/batches", json={"sample": "clean", "base_run_id": first["run_id"]})
    assert (cross_sample.status_code, cross_sample.json()["error"]["code"]) == (404, "REVISION_BASE")
    append_late_certificate(settings.source_dir)
    second = client.post("/api/batches", json={"sample": "dirty", "base_run_id": first["run_id"]}).json()["manifest"]
    assert second["supersedes"] == first["run_id"]
    triggers = settings.source_dir / "dirty-triggers.csv"
    triggers.write_bytes(triggers.read_bytes() + b"\r\n")
    stale = client.post("/api/batches", json={"sample": "dirty", "base_run_id": first["run_id"]})
    assert (stale.status_code, stale.json()["error"]["code"]) == (409, "REVISION_CONFLICT")
    third = run(client).json()["manifest"]
    assert (third["revision"], third["supersedes"]) == (3, second["run_id"])


def test_concurrent_late_evidence_commits_single_next_revision(client, settings):
    from conftest import append_late_certificate

    first = run(client).json()["manifest"]
    append_late_certificate(settings.source_dir)
    with ThreadPoolExecutor(max_workers=4) as workers:
        results = list(workers.map(lambda _: run(client), range(4)))
    assert sorted(result.status_code for result in results) == [200, 200, 200, 201]
    revisions = sorted(item["revision"] for item in client.get("/api/batches").json()["batches"])
    assert revisions == [1, 2]
