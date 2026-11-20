import csv
import json
from io import BytesIO, StringIO
from zipfile import ZIP_DEFLATED, ZipFile, ZipInfo

from app.reasons import REASONS


def safe_cell(value):
    if isinstance(value, str) and value.lstrip().startswith(("=", "+", "-", "@")):
        return "'" + value
    return value


def csv_bytes(header, rows):
    output = StringIO(newline="")
    writer = csv.writer(output, lineterminator="\r\n")
    writer.writerow(header)
    writer.writerows(rows)
    return output.getvalue().encode("utf-8-sig")


def issue_rows(results):
    families = (
        ("触发隔离", results["quarantine"]),
        ("触发重复", results["debounce"]["duplicates"]),
        ("磅单隔离", results["reconcile"]["ticket_issues"]),
    )
    for kind, issues in families:
        for issue in issues:
            source = issue["source"]
            yield (kind, safe_cell(issue["pass_id"]), issue["code"], issue["message"],
                   source["file"], source["line"], source["end_line"],
                   " / ".join(issue["related"]), safe_cell(issue["kept_id"]),
                   json.dumps(source["cells"], ensure_ascii=False))


def ticket_rows(results):
    for row in results["reconcile"]["rows"]:
        ticket = row["ticket"]
        yield (safe_cell(row["pass_id"]), safe_cell(row["plate"]), safe_cell(ticket["id"]) if ticket else "",
               ticket["revision"] if ticket else "", ticket["kg"] if ticket else "",
               row["wheel_kg"], row["difference_kg"], "勾稽通过" if row["status"] == "matched" else "待复秤",
               " / ".join(row["codes"]), " / ".join(REASONS[code] for code in row["codes"]),
               row["pass_ref"], " / ".join(row["ticket_refs"]), " / ".join(row["wheel_refs"]),
               " / ".join(row["review_refs"]))


def axle_rows(results):
    grades = {row["id"]: row for row in results["balance"]["rows"]}
    for axis in results["axles"]["axes"]:
        left, right = axis["left"], axis["right"]
        grade = grades[axis["id"]]
        level = {"pass": "正常", "warn": "偏载", "severe": "严重"}[grade["level"]]
        yield (safe_cell(axis["id"]), safe_cell(axis["group"]), axis["time"], axis["gap_m"], axis["skew_ms"],
               left["kg"], right["kg"], axis["total_kg"], grade["percent"], level,
               left["trigger"]["source"]["ref"], right["trigger"]["source"]["ref"],
               safe_cell(left["cert"]), safe_cell(right["cert"]), left["cert_ref"], right["cert_ref"])


def certificate_rows(results):
    for item in results["calibration"]["certificates"]:
        yield (safe_cell(item["id"]), item["side"], item["start"], item["end"], item["gain"], item["bias_kg"],
               item["source"]["file"], item["source"]["line"])


def handover_rows(results):
    for item in results["calibration"].get("handovers", []):
        yield (safe_cell(item["wheel"]), safe_cell(item["pass_id"]), item["side"], item["time"],
               safe_cell(item["chosen"]), item["chosen_ref"], " / ".join(item["superseded"]), item["basis"])


def revision_rows(manifest):
    detail = manifest.get("revision_detail") or {}
    if not detail:
        return
    if manifest.get("supersedes"):
        yield ("基线", manifest["supersedes"], "", "", "", "", "")
    for role in detail.get("changed_roles", ()):
        yield ("来源", role, "", "", "", "", "")
    for item in detail.get("certificates", {}).get("added", ()):
        yield ("证书新增", item["id"], item["start"], item["end"], item["gain"], item["bias_kg"], item["ref"])
    for item in detail.get("certificates", {}).get("removed", ()):
        yield ("证书移除", item["id"], item["start"], item["end"], item["gain"], item["bias_kg"], item["ref"])
    for item in detail.get("certificates", {}).get("changed", ()):
        yield ("证书变更", item["after"]["id"], item["before"]["gain"], item["after"]["gain"],
               item["before"]["bias_kg"], item["after"]["bias_kg"], item["after"]["ref"])
    for item in detail.get("late_certificates", ()):
        yield ("迟到证书", item["id"], item["effect"], "", "", "", item["ref"])
    for item in detail.get("recalculated_wheels", ()):
        yield ("轮载重算", item["trigger"], item["before_kg"], item["after_kg"],
               item["before_cert"], item["cert"], item["source_ref"])
    for item in detail.get("recalculated_differences", ()):
        yield ("差额重算", item["pass_id"], item["before_wheel_kg"], item["after_wheel_kg"],
               item["before_difference_kg"], item["after_difference_kg"], item["after_status"])
    yield ("汇总", "有效轮载kg", detail.get("wheel_kg_before"), detail.get("wheel_kg_after"), "", "", "")
    yield ("汇总", "待复秤车次", detail.get("review_before"), detail.get("review_after"), "", "", "")


def export_bundle(manifest, results):
    files = {
        "运行清单.json": json.dumps(manifest, indent=2, ensure_ascii=False, sort_keys=True).encode("utf-8"),
        "触发复核.csv": csv_bytes(
            ("类别", "车次", "原因码", "说明", "文件", "起始行", "结束行", "关联证据", "保留记录", "原始字段"),
            issue_rows(results),
        ),
        "整车勾稽.csv": csv_bytes(
            ("车次", "车牌", "磅单号", "版次", "静态总重kg", "有效轮载kg", "差额kg", "状态", "原因码", "说明",
             "车次档案", "磅单证据", "轮载证据", "复秤证据"), ticket_rows(results),
        ),
        "轴组轮载.csv": csv_bytes(
            ("轴号", "轴组", "轴时刻UTC", "轴距m", "右减左ms", "左kg", "右kg", "轴重kg", "偏载率%", "判级",
             "左轮证据", "右轮证据", "左标定证书", "右标定证书", "左证书行", "右证书行"), axle_rows(results),
        ),
        "标定证书.csv": csv_bytes(
            ("证书", "轮道", "生效UTC", "失效UTC", "增益", "偏置千克", "文件", "行号"), certificate_rows(results),
        ),
        "交接采信.csv": csv_bytes(
            ("记录号", "车次", "轮道", "采样时刻UTC", "采信证书", "证书行", "被覆盖证书", "采信依据"),
            handover_rows(results),
        ),
        "修订明细.csv": csv_bytes(
            ("类别", "对象", "前值", "后值", "补充前值", "补充后值", "证据"),
            revision_rows(manifest),
        ),
    }
    output = BytesIO()
    with ZipFile(output, "w") as archive:
        for name, data in files.items():
            # DOS 起始时刻使同一归档反复下载得到同一字节序列。
            archive.writestr(ZipInfo(name), data, compress_type=ZIP_DEFLATED)
    return output.getvalue()
