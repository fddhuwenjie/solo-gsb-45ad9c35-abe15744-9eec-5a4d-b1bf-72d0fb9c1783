"""展厅开放批次合规核验：单事务核验、合规凭证固化与 request_key 幂等。

设计要点：
- **核验**：调用方提交 ``request_key``、核验时间窗、盘点 ``round_id``、
  展品 ID 清单与诊断阈值。服务在**一个 SQLite 写事务**（``BEGIN IMMEDIATE``）
  内完成：展品 ID 别名解析、读取该轮次**固化**的盘点结果、逐件读取展陈日历
  并重算**截至窗口末的 calendar=display 剂量**（与剂量接口同口径）、在
  **完整未作废权威时间线**上形成诊断事件（与诊断接口同口径）、以及该规范
  档案的**未结处置单**状态；
- **固化**：只有所有展品都在盘点中定位到预期区域、窗口内无
  gap / jump / stuck、开放剂量未越限且没有未结处置单时，才把包含各项证据
  摘要的**合规凭证**落库并返回 ``digest``；任一项不满足即返回逐件原因
  （``status=non_compliant``），**不写入任何记录**，``request_key`` 不占用；
- **幂等**：``request_key`` 与（规范化输入摘要 + 当时证据摘要）绑定——
  相同 ``request_key`` 仅在规范化输入（时间窗 / 轮次 / 展品规范 ID 集合 /
  诊断阈值）与按当前库内状态重算的证据摘要**都一致**时重放首次凭证
  （``already_committed=true``），任一不同 → **409**；
- **凭证不可变**：落库后绝不 UPDATE；核验不写照度读数、展陈日历、盘点
  轮次、处置单与存证收据链，本模块唯一的写入点是
  ``compliance_credentials`` 表。
"""
from __future__ import annotations

import json
import sqlite3
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone

from fastapi import HTTPException

from .arbitration import (
    arbitrate,
    diagnosis_breaks,
    diagnosis_feed,
    load_priority_versions,
)
from .calendar import load_calendar, open_intervals
from .diagnose import DiagnosisEvent, build_events, intersect_window, sort_events
from .dose import compute_dose
from .inventory import load_round
from .merge import resolve_canonical
from .receipts import content_hash_of
from .schemas import (
    ComplianceCheckOut,
    ComplianceCheckRequest,
    ComplianceDoseOut,
    ComplianceExhibitOut,
    ComplianceLocationOut,
    ComplianceReasonOut,
    DiagnosisEventOut,
    ensure_aware,
)

# 阻断合规的诊断事件类型（timestamp_conflict 只记入证据，不阻断）
_BLOCKING_EVENT_TYPES = ("gap", "jump", "stuck")


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _to_iso(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, tz=timezone.utc).isoformat().replace("+00:00", "Z")


def _round6(x: float) -> float:
    return round(x, 6)


# ---------- 单件展品的证据采集（首次核验与重放核对共用同一口径） ----------

@dataclass
class _ExhibitCheck:
    """一件展品的核验结果：证据摘要（参与摘要哈希） + 逐条不合规原因。"""

    requested_id: str
    canonical_id: str
    location: dict                    # status / expected_area / actual_area
    dose: dict | None                 # calendar=display 剂量证据；未配置日历为 None
    events: list[dict] = field(default_factory=list)
    open_ticket_ids: list[str] = field(default_factory=list)
    reasons: list[dict] = field(default_factory=list)  # code / detail

    @property
    def evidence(self) -> dict:
        """参与凭证摘要哈希的证据部分（规范形式：只含规范 ID 与证据内容）。"""
        return {
            "canonical_id": self.canonical_id,
            "location": self.location,
            "dose": self.dose,
            "events": self.events,
            "open_ticket_ids": self.open_ticket_ids,
        }


def _event_dict(e: DiagnosisEvent) -> dict:
    """诊断事件的证据序列化（与诊断接口输出字段一致，浮点保留 6 位）。"""
    d: dict = {
        "type": e.type,
        "start": _to_iso(e.start),
        "end": _to_iso(e.end),
        "reading_ids": list(e.reading_ids),
    }
    if e.interval_seconds is not None:
        d["interval_seconds"] = _round6(e.interval_seconds)
    if e.rate_lux_per_second is not None:
        d["rate_lux_per_second"] = _round6(e.rate_lux_per_second)
    if e.duration_seconds is not None:
        d["duration_seconds"] = _round6(e.duration_seconds)
    if e.lux_span is not None:
        d["lux_span"] = _round6(e.lux_span)
    return d


def _location_from_round(round_out, canonical_id: str) -> dict:
    """从盘点轮次固化结果中取一件展品的定位结论。"""
    for m in round_out.misplaced:
        if m.canonical_id == canonical_id:
            return {"status": "misplaced",
                    "expected_area": m.expected_area, "actual_area": m.actual_area}
    for m in round_out.missing:
        if m.canonical_id == canonical_id:
            return {"status": "missing",
                    "expected_area": m.expected_area, "actual_area": None}
    for u in round_out.unknown_position:
        if u.canonical_id == canonical_id:
            return {"status": "unknown_position",
                    "expected_area": u.expected_area, "actual_area": None}
    for u in round_out.unexpected:
        if u.canonical_id == canonical_id:
            return {"status": "unexpected",
                    "expected_area": None, "actual_area": u.actual_area}
    for e in round_out.expectations:
        if e.canonical_id == canonical_id:
            return {"status": "located",
                    "expected_area": e.expected_area, "actual_area": e.expected_area}
    return {"status": "not_covered", "expected_area": None, "actual_area": None}


_LOCATION_REASONS = {
    "missing": ("inventory_missing",
                "盘点轮次中定位为缺失：预期区域 {expected_area} 全程无有效读头事件"),
    "misplaced": ("inventory_misplaced",
                  "盘点轮次中定位为错位：预期区域 {expected_area}，实际定位 {actual_area}"),
    "unknown_position": ("inventory_unknown_position",
                         "盘点轮次中位置不明：最后一个有事件窗口多区域信号差不足阈值"),
    "unexpected": ("inventory_unexpected",
                   "不在盘点轮次预期清单中（意外出现于 {actual_area}），无法确认预期区域"),
    "not_covered": ("inventory_not_covered",
                    "盘点轮次中无该展品的预期与观测记录，无法确认位置"),
}


def check_exhibit(
    conn: sqlite3.Connection,
    requested_id: str,
    canonical_id: str,
    round_out,
    start_s: float,
    end_s: float,
    *,
    max_rate_lux_per_s: float,
    stuck_tol_lux: float,
    min_stuck_s: float,
) -> _ExhibitCheck:
    """在当前事务内采集一件展品的全部证据并给出逐条不合规原因。

    剂量与诊断事件的口径分别与 ``GET /dose?calendar=display`` 和
    ``POST /readings/diagnose`` 一致（完整未作废权威时间线 + 窗口相交筛选）。
    """
    exhibit = conn.execute(
        "SELECT * FROM exhibits WHERE id = ?", (canonical_id,)
    ).fetchone()
    if exhibit is None:
        raise HTTPException(
            status_code=409,
            detail=f"规范档案 {canonical_id} 已不存在，无法完成合规核验",
        )

    check = _ExhibitCheck(
        requested_id=requested_id,
        canonical_id=canonical_id,
        location=_location_from_round(round_out, canonical_id),
        dose=None,
    )

    # 1) 盘点定位：唯一定位到预期区域才合规
    loc = check.location
    if loc["status"] != "located":
        code, template = _LOCATION_REASONS[loc["status"]]
        check.reasons.append({"code": code, "detail": template.format(**loc)})

    # 2) 诊断事件：完整未作废权威时间线上形成，再筛选与窗口相交的部分
    rows = conn.execute(
        "SELECT reading_id, ts, lux, source_id FROM readings "
        "WHERE exhibit_id = ? AND voided_at IS NULL ORDER BY ts, reading_id",
        (canonical_id,),
    ).fetchall()
    versions = load_priority_versions(conn, canonical_id)
    breaks: list[float] = []
    dose_breaks: list[float] = []
    if versions:
        points, arb_conflicts = arbitrate(
            [(r["reading_id"], r["ts"], r["lux"], r["source_id"]) for r in rows],
            versions,
        )
        feed = diagnosis_feed(points)
        breaks = diagnosis_breaks(arb_conflicts)
        dose_readings = [(p.ts, p.lux) for p in points]
        dose_breaks = [c.ts for c in arb_conflicts]
    else:
        feed = [(r["reading_id"], r["ts"], r["lux"]) for r in rows]
        # 与剂量接口一致：截至窗口末的全部读数 + 窗口后最近一条（参与末端裁剪）
        dose_readings = [(r["ts"], r["lux"]) for r in rows if r["ts"] <= end_s]
        after = [r for r in rows if r["ts"] > end_s]
        if after:
            dose_readings.append((after[0]["ts"], after[0]["lux"]))

    events = build_events(
        feed,
        max_interval_s=exhibit["max_interval_seconds"],
        max_rate_lux_per_s=max_rate_lux_per_s,
        stuck_tol_lux=stuck_tol_lux,
        min_stuck_s=min_stuck_s,
        breaks=breaks,
    )
    events = sort_events(intersect_window(events, start_s, end_s))
    check.events = [_event_dict(e) for e in events]
    blocking = [e for e in events if e.type in _BLOCKING_EVENT_TYPES]
    if blocking:
        counts = {t: sum(1 for e in blocking if e.type == t)
                  for t in _BLOCKING_EVENT_TYPES}
        check.reasons.append({
            "code": "diagnosis_events",
            "detail": f"窗口内存在诊断事件：gap {counts['gap']} 个、"
                      f"jump {counts['jump']} 个、stuck {counts['stuck']} 个",
        })

    # 3) 开放剂量：calendar=display 口径，截至窗口末的开放历史不得越限
    cfg = load_calendar(conn, canonical_id)
    if cfg is None:
        check.reasons.append({
            "code": "calendar_missing",
            "detail": "展品未配置展陈开放日历，无法按 calendar=display 核算开放剂量",
        })
    else:
        # 开放区间须覆盖截至窗口末的开放历史（与剂量接口同口径）
        first_ts = dose_readings[0][0] if dose_readings else start_s
        intervals = open_intervals(cfg, min(start_s, first_ts), end_s)
        dose = compute_dose(
            readings=dose_readings,
            start_s=start_s,
            end_s=end_s,
            max_interval_s=exhibit["max_interval_seconds"],
            dose_limit_luxh=exhibit["dose_limit_luxh"],
            open_intervals=intervals,
            break_ts=dose_breaks,
        )
        check.dose = {
            "dose_limit_luxh": exhibit["dose_limit_luxh"],
            "known_dose_luxh": _round6(dose.known_dose_luxh),
            "remaining_luxh": _round6(dose.remaining_luxh),
            "exceeded": dose.exceeded,
            "first_exceeded_at": (
                _to_iso(dose.first_exceeded_at)
                if dose.first_exceeded_at is not None else None
            ),
            "unknown_seconds": _round6(dose.unknown_seconds),
            "covered_seconds": _round6(dose.covered_seconds),
            "closed_seconds": (
                _round6(dose.closed_seconds) if dose.closed_seconds is not None else 0.0
            ),
            "reading_count": dose.reading_count,
        }
        if dose.exceeded:
            check.reasons.append({
                "code": "dose_exceeded",
                "detail": "截至窗口末的开放剂量已越限"
                          + (f"（首次越线 {check.dose['first_exceeded_at']}）"
                             if check.dose["first_exceeded_at"] else ""),
            })

    # 4) 未结处置单：该规范档案当前不得有 open 状态的照度异常处置单
    ticket_rows = conn.execute(
        "SELECT ticket_id FROM illumination_tickets "
        "WHERE canonical_id = ? AND status = 'open' ORDER BY created_at, ticket_id",
        (canonical_id,),
    ).fetchall()
    check.open_ticket_ids = [r["ticket_id"] for r in ticket_rows]
    if check.open_ticket_ids:
        check.reasons.append({
            "code": "open_tickets",
            "detail": f"存在 {len(check.open_ticket_ids)} 张未结处置单: "
                      + ", ".join(check.open_ticket_ids),
        })

    return check


# ---------- 规范化输入与凭证摘要 ----------

def _normalized_request(
    payload: ComplianceCheckRequest,
    start_s: float,
    end_s: float,
    canonical_ids: list[str],
) -> dict:
    """规范化输入：时间统一 UTC、展品 ID 为解析后的规范 ID 并排序（顺序与
    别名写法不影响摘要）、诊断阈值按浮点稳定序列化参与哈希。"""
    return {
        "round_id": payload.round_id,
        "window": {"start": _to_iso(start_s), "end": _to_iso(end_s)},
        "exhibit_ids": sorted(canonical_ids),
        "thresholds": {
            "max_rate_lux_per_second": payload.max_rate_lux_per_second,
            "stuck_tolerance_lux": payload.stuck_tolerance_lux,
            "min_stuck_seconds": payload.min_stuck_seconds,
        },
    }


def _credential_digest(normalized: dict, round_digest: str, checks: list[_ExhibitCheck]) -> str:
    """凭证摘要：规范化输入 + 轮次固化摘要 + 逐件证据摘要的 SHA-256。

    证据按规范 ID 排序参与哈希：请求清单的排列顺序不影响摘要（与规范化
    输入中 ``exhibit_ids`` 排序的口径一致）；响应中的逐件顺序仍按请求顺序。
    """
    return content_hash_of({
        "request": normalized,
        "round_digest": round_digest,
        "exhibits": [c.evidence for c in sorted(checks, key=lambda c: c.canonical_id)],
    })


def _exhibit_out(c: _ExhibitCheck) -> ComplianceExhibitOut:
    return ComplianceExhibitOut(
        requested_id=c.requested_id,
        canonical_id=c.canonical_id,
        compliant=not c.reasons,
        reasons=[ComplianceReasonOut(**r) for r in c.reasons],
        location=ComplianceLocationOut(**c.location),
        dose=ComplianceDoseOut(**c.dose) if c.dose is not None else None,
        events=[DiagnosisEventOut(**e) for e in c.events],
        open_ticket_ids=list(c.open_ticket_ids),
    )


def _resolve_exhibits(
    conn: sqlite3.Connection, exhibit_ids: list[str]
) -> list[tuple[str, str]]:
    """展品 ID 清单解析为 (请求 ID, 规范 ID) 序列；不存在 → 404，解析后重复 → 422。"""
    resolved: list[tuple[str, str]] = []
    seen: dict[str, str] = {}
    for eid in exhibit_ids:
        canonical = resolve_canonical(conn, eid)
        if canonical is None:
            raise HTTPException(status_code=404, detail=f"展品不存在: {eid}")
        if canonical in seen:
            raise HTTPException(
                status_code=422,
                detail=f"展品清单重复：{eid} 与 {seen[canonical]} 解析到同一规范档案 "
                       f"{canonical}，每件展品只能出现一次",
            )
        seen[canonical] = eid
        resolved.append((eid, canonical))
    return resolved


def _run_checks(
    conn: sqlite3.Connection,
    payload: ComplianceCheckRequest,
    resolved: list[tuple[str, str]],
    start_s: float,
    end_s: float,
):
    """读取轮次固化结果并逐件采集证据；轮次未固化 → 404。"""
    round_out = load_round(conn, payload.round_id)
    if round_out is None:
        raise HTTPException(
            status_code=404,
            detail=f"盘点轮次不存在或尚未对账: {payload.round_id}",
        )
    checks = [
        check_exhibit(
            conn, requested, canonical, round_out, start_s, end_s,
            max_rate_lux_per_s=payload.max_rate_lux_per_second,
            stuck_tol_lux=payload.stuck_tolerance_lux,
            min_stuck_s=payload.min_stuck_seconds,
        )
        for requested, canonical in resolved
    ]
    return round_out, checks


# ---------- 核验与固化 ----------

def verify_batch(
    conn: sqlite3.Connection, payload: ComplianceCheckRequest
) -> ComplianceCheckOut:
    """执行一批展品的开放合规核验；全部通过才固化合规凭证。

    别名解析、轮次读取、证据采集与凭证落库全部在同一个 ``BEGIN IMMEDIATE``
    事务内完成：任一校验失败（404/422/409）整体回滚；核验不通过时不写入
    任何记录（``request_key`` 不占用，可在条件修复后用同键重新核验）。
    """
    start_s = ensure_aware(payload.start).timestamp()
    end_s = ensure_aware(payload.end).timestamp()
    if end_s <= start_s:
        raise HTTPException(status_code=422, detail="end 必须晚于 start")

    conn.execute("BEGIN IMMEDIATE")
    try:
        resolved = _resolve_exhibits(conn, list(payload.exhibit_ids))
        normalized = _normalized_request(
            payload, start_s, end_s, [cid for _, cid in resolved]
        )
        request_digest = content_hash_of(normalized)

        # 已固化的同键凭证：规范化输入与当时摘要都一致才允许重放
        existing = conn.execute(
            "SELECT request_digest, digest, result_json FROM compliance_credentials "
            "WHERE request_key = ?",
            (payload.request_key,),
        ).fetchone()
        if existing is not None:
            if existing["request_digest"] != request_digest:
                raise HTTPException(
                    status_code=409,
                    detail=f"request_key {payload.request_key} 已绑定另一份核验请求"
                           "（规范化输入不同），不能换目标重复提交",
                )
            round_out, checks = _run_checks(conn, payload, resolved, start_s, end_s)
            digest = _credential_digest(normalized, round_out.request_digest, checks)
            if digest != existing["digest"]:
                raise HTTPException(
                    status_code=409,
                    detail=f"request_key {payload.request_key} 的证据摘要已变化"
                           "（读数、日历、诊断事件或处置单状态与首次核验时不同），"
                           "不能重放首次凭证；请换用新的 request_key 重新核验",
                )
            conn.commit()  # 尚未写入，COMMIT 仅释放 IMMEDIATE 锁
            result = json.loads(existing["result_json"])
            result["already_committed"] = True
            return ComplianceCheckOut(**result)

        round_out, checks = _run_checks(conn, payload, resolved, start_s, end_s)

        if any(c.reasons for c in checks):
            # 任一件不合规：返回逐件原因，不写入任何记录
            conn.rollback()
            return ComplianceCheckOut(
                request_key=payload.request_key,
                round_id=payload.round_id,
                round_digest=round_out.request_digest,
                start=_to_iso(start_s),
                end=_to_iso(end_s),
                status="non_compliant",
                credential_id=None,
                digest=None,
                exhibits=[_exhibit_out(c) for c in checks],
                created_at=None,
                already_committed=False,
            )

        # 全部通过：固化包含各项证据摘要的合规凭证
        digest = _credential_digest(normalized, round_out.request_digest, checks)
        credential_id = uuid.uuid4().hex
        created_at = _utc_now_iso()
        result = ComplianceCheckOut(
            request_key=payload.request_key,
            round_id=payload.round_id,
            round_digest=round_out.request_digest,
            start=_to_iso(start_s),
            end=_to_iso(end_s),
            status="compliant",
            credential_id=credential_id,
            digest=digest,
            exhibits=[_exhibit_out(c) for c in checks],
            created_at=created_at,
            already_committed=False,
        )
        conn.execute(
            "INSERT INTO compliance_credentials "
            "(request_key, request_digest, digest, credential_id, result_json, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (
                payload.request_key, request_digest, digest, credential_id,
                json.dumps(result.model_dump(), ensure_ascii=False, sort_keys=True),
                created_at,
            ),
        )
        conn.commit()
        return result
    except HTTPException:
        conn.rollback()
        raise
    except Exception:
        conn.rollback()
        raise


def load_credential(
    conn: sqlite3.Connection, request_key: str
) -> ComplianceCheckOut | None:
    """按 request_key 取回已固化的合规凭证；不存在返回 None。"""
    row = conn.execute(
        "SELECT result_json FROM compliance_credentials WHERE request_key = ?",
        (request_key,),
    ).fetchone()
    if row is None:
        return None
    return ComplianceCheckOut(**json.loads(row["result_json"]))
