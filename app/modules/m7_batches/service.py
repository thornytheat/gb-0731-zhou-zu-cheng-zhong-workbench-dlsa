from time import perf_counter_ns

from app.errors import DomainError
from app.manifest import canonical, clock, digest, identify
from app.modules.m7_batches.pipeline import run_pipeline
from app.modules.m7_batches.revision import describe
from app.sources import read_sources
from app.store import add_events, add_run, find, latest


def commit_sample(store, source_dir, sample, base_run_id=None):
    sources = read_sources(source_dir, sample)
    identity = identify(sample, sources)
    run_id = identity["run_id"]
    with store.transaction() as db:
        existing = find(db, run_id)
        if existing:
            add_events(db, run_id, [(clock(), "info", "REUSED", "相同来源已归档", None)])
            return existing[0], True
        base = None
        if base_run_id is not None:
            base = find(db, base_run_id)
            if base is None or base[0].get("sample") != sample:
                raise DomainError("REVISION_BASE", 404)
            current_latest = latest(db, sample)
            if current_latest is not None and current_latest[0]["run_id"] != base_run_id:
                raise DomainError("REVISION_CONFLICT", 409)
        else:
            base = latest(db, sample)
        started = perf_counter_ns()
        events = [(clock(), "info", "START", "开始读取称重源文件", None)]
        results = run_pipeline(sources, events=events)
        if base is None:
            revision, supersedes, root = 1, None, None
            reason, detail = "INITIAL", {}
        else:
            base_manifest, base_results = base
            if base_manifest.get("sample") != sample:
                revision, supersedes, root = 1, None, None
                reason, detail = "INITIAL", {}
            else:
                revision = base_manifest.get("revision", 1) + 1
                supersedes = base_manifest["run_id"]
                root = base_manifest.get("lineage_root") or base_manifest["run_id"]
                reason, detail = describe(base_manifest, base_results, identity, results)
        manifest = {
            **identity,
            "created_at": clock(),
            "elapsed_ms": (perf_counter_ns() - started) // 1_000_000,
            "result_sha256": digest(canonical(results).encode("utf-8")),
            "ledger": results["reconcile"]["ledger"],
            "revision": revision,
            "supersedes": supersedes,
            "lineage_root": root,
            "revision_reason": reason,
            "revision_detail": detail,
        }
        if base is not None:
            events.append((clock(), "info", "REVISED", _revision_message(revision, reason, detail), None))
        add_run(db, manifest, results)
        add_events(db, run_id, events + [(manifest["created_at"], "ok", "COMMITTED", "批次已归档", None)])
        return manifest, False


def _revision_message(revision, reason, detail):
    labels = {
        "INITIAL": "首版",
        "CERTIFICATE_LATE": "迟到标定",
        "CERTIFICATE_UPDATE": "标定更新",
        "SOURCE_UPDATE": "来源更新",
    }
    late = len(detail.get("late_certificates", ())) if isinstance(detail, dict) else 0
    suffix = f" · 迟到证书 {late} 张" if late else ""
    return f"修订 {revision} · {labels.get(reason, reason)}{suffix}"


def replay_sample(store, source_dir, run_id):
    manifest, original = store.read(run_id)
    sample = manifest["sample"]
    current = read_sources(source_dir, sample)
    if identify(sample, current)["run_id"] != run_id:
        raise DomainError("SOURCE_CHANGED", 409)
    recomputed = run_pipeline(current)
    if canonical(recomputed) != canonical(original):
        raise DomainError("REPLAY_MISMATCH", 409)
    with store.transaction() as db:
        add_events(db, run_id, [(clock(), "ok", "REPLAYED", "重算核验一致 · 沿用归档", None)])
    return manifest
