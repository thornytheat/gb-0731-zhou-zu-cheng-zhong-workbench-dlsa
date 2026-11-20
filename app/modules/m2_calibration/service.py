from dataclasses import dataclass
from decimal import Decimal

from app.config import MAX_WHEEL_KG
from app.evidence import Issue
from app.modules.m1_intake.parse import Trigger
from app.modules.m2_calibration.certificates import certificates
from app.modules.m2_calibration.crossing import crossings
from app.values import rounded

# 交接叠期的采信依据：生效起点较晚的证书优先（后装表更接近现场真值），
# 生效起点早的证书在该起点之前仍连续有效；起点相同且参数一致时视为等效。
BASIS_LATE_START = "LATE_START"
BASIS_EQUIVALENT = "EQUIVALENT"
BASIS_NO_ORDER = "NO_ORDER"


@dataclass(frozen=True)
class Wheel:
    trigger: Trigger
    kg: Decimal
    cert: str
    cert_ref: str
    cert_candidates: tuple[str, ...] = ()
    cert_basis: str = "SINGLE"


def handover_choice(eligible):
    """按统一依据从叠期证书中选出一张；返回 (证书, 依据) 或 (None, NO_ORDER)。

    依据按顺序比较：生效起点较晚 → 失效终点较晚。仍并列时，若参数完全一致
    视为等效（取证书号最小者）；参数不同则无法判定先后，交现场复核。
    """
    latest = max(item.start for item in eligible)
    newest = [item for item in eligible if item.start == latest]
    if len(newest) > 1:
        last_end = max(item.end for item in newest)
        newest = [item for item in newest if item.end == last_end]
    if len(newest) == 1:
        return newest[0], BASIS_LATE_START
    parameters = {(item.gain, item.bias_kg) for item in newest}
    if len(parameters) == 1:
        return min(newest, key=lambda item: item.id), BASIS_EQUIVALENT
    return None, BASIS_NO_ORDER


def calibrate(events, sources):
    catalog = certificates(sources["certificates"])
    passes = crossings(sources["passes"])
    by_side = {side: [item for item in catalog if item.side == side] for side in ("L", "R")}
    wheels, issues, handovers = [], [], []
    for event in events:
        crossing = passes.get(event.pass_id)
        if crossing is None:
            issues.append(Issue("PASS_UNKNOWN", event.source, event.pass_id))
            continue
        if not crossing.start <= event.time < crossing.end:
            issues.append(Issue("OUTSIDE_PASS", event.source, event.pass_id, (crossing.source.ref,)))
            continue
        eligible = sorted(
            (item for item in by_side[event.side] if item.start <= event.time < item.end),
            key=lambda item: (item.start, item.id),
        )
        if not eligible:
            issues.append(Issue("CAL_MISSING", event.source, event.pass_id))
            continue
        chosen, basis = handover_choice(eligible)
        refs = tuple(item.source.ref for item in eligible)
        if chosen is None:
            issues.append(Issue("CAL_OVERLAP", event.source, event.pass_id, refs))
            continue
        if len(eligible) > 1:
            handovers.append({
                "wheel": event.id,
                "pass_id": event.pass_id,
                "side": event.side,
                "time": event.time,
                "chosen": chosen.id,
                "chosen_ref": chosen.source.ref,
                "superseded": tuple(item.source.ref for item in eligible if item.id != chosen.id),
                "basis": basis,
            })
        kg = rounded(event.raw_kg * chosen.gain + chosen.bias_kg)
        if not 0 < kg <= MAX_WHEEL_KG:
            issues.append(Issue("WHEEL_RANGE", event.source, event.pass_id, (chosen.source.ref,)))
            continue
        wheels.append(Wheel(
            event, kg, chosen.id, chosen.source.ref, refs,
            basis if len(eligible) > 1 else "SINGLE",
        ))
    return {
        "wheels": wheels,
        "issues": issues,
        "handovers": sorted(handovers, key=lambda item: (item["time"], item["wheel"])),
        "certificates": catalog,
        "passes": passes,
    }
