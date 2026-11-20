"""批次修订谱系：同一样例的新证据形成可辨认的新修订，旧修订只读保留。

修订号按样例单调递增；`supersedes` 指向上一修订的批次号，`lineage_root` 指向首版。
修订明细只描述与上一修订的差异：证书增删改、迟到证书所影响的已出结论，以及
轮载和磅单差额的重算结果，便于稽核员回查每个数字的来源。
"""

from app.evidence import wire

ROLE_NAMES = {
    "triggers": "触发流水",
    "tickets": "静态磅单",
    "certificates": "标定证书",
    "passes": "车次档案",
}


def certificate_index(results):
    return {
        item["id"]: {
            "id": item["id"], "side": item["side"], "start": item["start"], "end": item["end"],
            "gain": item["gain"], "bias_kg": item["bias_kg"], "ref": item["source"]["ref"],
        }
        for item in results.get("calibration", {}).get("certificates", [])
    }


def evaluated_span(results):
    times = [
        item["trigger"]["time"]
        for item in results.get("calibration", {}).get("wheels", [])
    ]
    if not times:
        return None
    return min(times), max(times)


def certificate_delta(base, current):
    before, after = certificate_index(base), certificate_index(current)
    added = [after[key] for key in sorted(after.keys() - before.keys())]
    removed = [before[key] for key in sorted(before.keys() - after.keys())]
    changed = [
        {"id": key, "before": before[key], "after": after[key]}
        for key in sorted(before.keys() & after.keys())
        if before[key] != after[key]
    ]
    return {"added": added, "removed": removed, "changed": changed}


def late_certificates(delta, span):
    """在上一修订已评估过的时段内生效的增改证书，视为迟到记录。"""
    if span is None:
        return []
    start, end = span
    late = []
    for item in delta["added"]:
        if item["start"] < end and start < item["end"]:
            late.append({"id": item["id"], "ref": item["ref"], "effect": "ADDED"})
    for item in delta["changed"]:
        after = item["after"]
        if after["start"] < end and start < after["end"]:
            late.append({"id": after["id"], "ref": after["ref"], "effect": "CHANGED"})
    return late


def wheel_delta(base, current):
    before = {
        (item["trigger"]["id"], item["trigger"]["source"]["ref"]): item
        for item in base.get("calibration", {}).get("wheels", [])
    }
    changes = []
    for item in current.get("calibration", {}).get("wheels", []):
        key = (item["trigger"]["id"], item["trigger"]["source"]["ref"])
        previous = before.pop(key, None)
        if previous is None:
            changes.append({
                "trigger": item["trigger"]["id"], "pass_id": item["trigger"]["pass_id"],
                "source_ref": item["trigger"]["source"]["ref"], "cert": item["cert"],
                "before_kg": None, "after_kg": item["kg"], "before_cert": None,
            })
        elif previous["kg"] != item["kg"] or previous["cert"] != item["cert"]:
            changes.append({
                "trigger": item["trigger"]["id"], "pass_id": item["trigger"]["pass_id"],
                "source_ref": item["trigger"]["source"]["ref"], "cert": item["cert"],
                "before_kg": previous["kg"], "after_kg": item["kg"], "before_cert": previous["cert"],
            })
    for item in before.values():
        changes.append({
            "trigger": item["trigger"]["id"], "pass_id": item["trigger"]["pass_id"],
            "source_ref": item["trigger"]["source"]["ref"], "cert": None,
            "before_kg": item["kg"], "after_kg": None, "before_cert": item["cert"],
        })
    return changes


def difference_delta(base, current):
    before = {row["pass_id"]: row for row in base.get("reconcile", {}).get("rows", [])}
    changes = []
    for row in current.get("reconcile", {}).get("rows", []):
        previous = before.get(row["pass_id"])
        if previous is None:
            continue
        if previous["difference_kg"] != row["difference_kg"] or previous["wheel_kg"] != row["wheel_kg"]:
            changes.append({
                "pass_id": row["pass_id"],
                "before_wheel_kg": previous["wheel_kg"], "after_wheel_kg": row["wheel_kg"],
                "before_difference_kg": previous["difference_kg"],
                "after_difference_kg": row["difference_kg"],
                "before_status": previous["status"], "after_status": row["status"],
            })
    return changes


def source_delta(base_manifest, manifest):
    roles = []
    for role in sorted(set(base_manifest.get("files", {})) | set(manifest.get("files", {}))):
        before = base_manifest.get("files", {}).get(role, {}).get("sha256")
        after = manifest.get("files", {}).get(role, {}).get("sha256")
        if before != after:
            roles.append(ROLE_NAMES.get(role, role))
    return roles


def describe(base_manifest, base_results, manifest, results):
    """返回 (修订原因, 修订明细)。"""
    certificates = certificate_delta(base_results, results)
    late = late_certificates(certificates, evaluated_span(base_results))
    wheels = wheel_delta(base_results, results)
    differences = difference_delta(base_results, results)
    roles = source_delta(base_manifest, manifest)
    if certificates["added"] or certificates["removed"] or certificates["changed"]:
        reason = "CERTIFICATE_LATE" if late else "CERTIFICATE_UPDATE"
    else:
        reason = "SOURCE_UPDATE"
    detail = {
        "base_run_id": base_manifest["run_id"],
        "base_revision": base_manifest.get("revision", 1),
        "changed_roles": roles,
        "certificates": certificates,
        "late_certificates": late,
        "recalculated_wheels": wheels,
        "recalculated_differences": differences,
        "wheel_kg_before": base_results.get("reconcile", {}).get("ledger", {}).get("wheel_kg"),
        "wheel_kg_after": results.get("reconcile", {}).get("ledger", {}).get("wheel_kg"),
        "review_before": base_results.get("reconcile", {}).get("ledger", {}).get("review"),
        "review_after": results.get("reconcile", {}).get("ledger", {}).get("review"),
    }
    return reason, wire(detail)
