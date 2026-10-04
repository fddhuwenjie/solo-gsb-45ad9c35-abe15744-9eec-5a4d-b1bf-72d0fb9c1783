"""照度异常处置单：创建时固化快照，结案时重算核对。

设计要点：
- **创建**：提交展品 ID（可用归并别名）、处置时间窗、诊断阈值与
  ``request_key``。服务在**一个 SQLite 事务**（``BEGIN IMMEDIATE``）内按
  **未作废权威时间线**重算诊断事件（与 ``POST /readings/diagnose`` 同口径）
  与 ``calendar=display`` 剂量（与 ``GET /dose?calendar=display`` 同口径），
  连同**收据链末端哈希**、未作废读数指纹、来源优先级配置指纹与日历指纹
  一起固化快照，并以稳定规范序列化的 SHA-256 作为 ``digest`` 返回；
  展品未配置日历时无法核算 ``calendar=display`` 剂量，创建直接 409。
- **快照不可变**：``snapshot_json`` 与 ``digest`` 落库后绝不 UPDATE；
  相同 ``request_key`` 且目标（规范档案 / 时间窗 / 诊断阈值）一致时重放
  首次创建的原件（``already_committed=true``），换目标 → 409。
- **结案**：处置单只能从 ``open`` 结案为 ``resolved`` 或 ``accepted``。
  结案提交 ``expected_digest``、决定与说明；服务在写事务内**重算当前摘要**，
  读数、作废、来源优先级、日历或收据链的任何变化都会使摘要不同 →
  **409 且不写入**（客户端应先 GET 取回 ``current_digest`` 再结案）。
- **resolved 举证**：须给出涉及的存证收据（``receipt_ids``）或批次更正
  （``correction_request_keys``）至少一项（须属于本规范档案，否则 422），
  并证明重算后**原诊断事件全部消失**且 ``calendar=display`` 剂量**未越限**，
  否则 409；``accepted`` 保留事件，只在 ``note`` 中记录理由，不接受证据引用。
- 已结案的处置单不能再次结案（409）；结案只写结案列，创建快照保持不可变。
"""
from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import datetime, timezone

from fastapi import HTTPException

from .arbitration import (
    arbitrate,
    diagnosis_breaks,
    diagnosis_feed,
    load_priority_versions,
)
from .calendar import CalendarConfig, load_calendar, open_intervals
from .diagnose import DiagnosisEvent, build_events, intersect_window, sort_events
from .dose import compute_dose
from .merge import get_canonical_or_404, resolve_canonical
from .receipts import GENESIS_PREV_HASH, content_hash_of
from .schemas import (
    IlluminationTicketCloseOut,
    IlluminationTicketCloseRequest,
    IlluminationTicketCreate,
    IlluminationTicketOut,
    TicketEvidenceOut,
    TicketSnapshotOut,
    ensure_aware,
)


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _to_iso(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, tz=timezone.utc).isoformat().replace("+00:00", "Z")


def _round6(x: float) -> float:
    return round(x, 6)


# ---------- 快照重算（创建固化与结案核对共用同一口径） ----------

def _event_dict(e: DiagnosisEvent) -> dict:
    """诊断事件的快照序列化（与诊断接口输出字段一致，浮点保留 6 位）。"""
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


def _calendar_payload(cfg: CalendarConfig) -> dict:
    """展陈日历的稳定指纹载荷：时区 + 每周时段 + 例外日（全部按内容排序）。"""
    return {
        "timezone": cfg.timezone,
        "weekly": [
            {"weekday": wd, "start_sec": s, "end_sec": e}
            for wd in sorted(cfg.weekly)
            for s, e in sorted(cfg.weekly[wd])
        ],
        "exceptions": [
            {"date": day.isoformat(),
             "windows": [{"start_sec": s, "end_sec": e} for s, e in sorted(wins)]}
            for day, wins in sorted(cfg.exceptions.items())
        ],
    }


def compute_snapshot(
    conn: sqlite3.Connection,
    canonical_id: str,
    start_s: float,
    end_s: float,
    *,
    max_rate_lux_per_s: float,
    stuck_tol_lux: float,
    min_stuck_s: float,
) -> dict:
    """按当前库内状态重算处置单快照（创建固化与结案核对共用同一口径）。

    - 诊断事件：在规范档案的**完整未作废时间线**上形成（已配置来源优先级
      版本时先仲裁为权威时间线，保持冲突的时刻作为断点），再筛选与窗口
      相交的事件——与诊断接口一致；
    - 剂量：``calendar=display`` 口径，与剂量接口一致；未配置日历 → 409；
    - 另含收据链末端哈希、未作废读数指纹、来源优先级配置指纹与日历指纹，
      使读数、作废、来源优先级、日历或收据链的任何变化都改变快照摘要。
    """
    exhibit = conn.execute(
        "SELECT * FROM exhibits WHERE id = ?", (canonical_id,)
    ).fetchone()
    if exhibit is None:
        raise HTTPException(
            status_code=409,
            detail=f"规范档案 {canonical_id} 已不存在，无法重算处置单快照",
        )

    rows = conn.execute(
        "SELECT reading_id, ts, lux, source_id FROM readings "
        "WHERE exhibit_id = ? AND voided_at IS NULL ORDER BY ts, reading_id",
        (canonical_id,),
    ).fetchall()

    # 与诊断/剂量接口一致：配置了来源优先级版本时先逐时刻仲裁，
    # 保持冲突的时刻作为断点（不进入诊断输入与剂量积分）。
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

    cfg = load_calendar(conn, canonical_id)
    if cfg is None:
        raise HTTPException(
            status_code=409,
            detail=f"展品未配置展陈开放日历，无法按 calendar=display 核算: {canonical_id}",
        )
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

    tail = conn.execute(
        "SELECT seq, chain_hash FROM ingest_receipts WHERE chain_id = ? "
        "ORDER BY seq DESC LIMIT 1",
        (canonical_id,),
    ).fetchone()

    return {
        "canonical_id": canonical_id,
        "window": {"start": _to_iso(start_s), "end": _to_iso(end_s)},
        "max_interval_seconds": exhibit["max_interval_seconds"],
        "thresholds": {
            "max_rate_lux_per_second": _round6(max_rate_lux_per_s),
            "stuck_tolerance_lux": _round6(stuck_tol_lux),
            "min_stuck_seconds": _round6(min_stuck_s),
        },
        "events": [_event_dict(e) for e in events],
        "dose": {
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
        },
        "receipt_chain": {
            "chain_id": canonical_id,
            "seq": tail["seq"] if tail is not None else 0,
            "chain_hash": tail["chain_hash"] if tail is not None else GENESIS_PREV_HASH,
        },
        "readings_fingerprint": content_hash_of({
            "readings": [
                [r["reading_id"], _to_iso(r["ts"]), r["lux"], r["source_id"]]
                for r in rows
            ],
        }),
        "source_priority_fingerprint": content_hash_of({
            "versions": [
                {"version": v.version, "effective_at": v.effective_at,
                 "priorities": list(v.priorities)}
                for v in versions
            ],
        }),
        "calendar_fingerprint": content_hash_of(_calendar_payload(cfg)),
    }


def snapshot_digest(snapshot: dict) -> str:
    """快照摘要：稳定规范序列化（键排序、紧凑分隔符、浮点 float.hex）的 SHA-256。"""
    return content_hash_of(snapshot)


# ---------- 输出组装 ----------

def _ticket_out(
    row: sqlite3.Row,
    *,
    current_digest: str | None = None,
    already: bool = False,
) -> IlluminationTicketOut:
    snapshot = json.loads(row["snapshot_json"])
    evidence = json.loads(row["evidence_json"]) if row["evidence_json"] else None
    return IlluminationTicketOut(
        ticket_id=row["ticket_id"],
        request_key=row["request_key"],
        requested_id=row["requested_id"],
        canonical_id=row["canonical_id"],
        start=snapshot["window"]["start"],
        end=snapshot["window"]["end"],
        max_rate_lux_per_second=row["max_rate_lux_per_second"],
        stuck_tolerance_lux=row["stuck_tolerance_lux"],
        min_stuck_seconds=row["min_stuck_seconds"],
        status=row["status"],
        digest=row["digest"],
        snapshot=TicketSnapshotOut(**snapshot),
        created_at=row["created_at"],
        decision=row["decision"],
        closed_at=row["closed_at"],
        close_note=row["close_note"],
        evidence=TicketEvidenceOut(**evidence) if evidence is not None else None,
        closed_digest=row["closed_digest"],
        current_digest=current_digest,
        already_committed=already,
    )


# ---------- 创建 ----------

def create_ticket(
    conn: sqlite3.Connection,
    requested_id: str,
    payload: IlluminationTicketCreate,
) -> IlluminationTicketOut:
    """在单个写事务内重算并固化快照，创建处置单；request_key 幂等。"""
    start_s = ensure_aware(payload.start).timestamp()
    end_s = ensure_aware(payload.end).timestamp()
    if end_s <= start_s:
        raise HTTPException(status_code=422, detail="end 必须晚于 start")

    # 幂等回放不依赖写锁之外的状态：先读一次（无行则进入写事务）。
    existing = conn.execute(
        "SELECT * FROM illumination_tickets WHERE request_key = ?",
        (payload.request_key,),
    ).fetchone()
    if existing is not None:
        return _replay_create(conn, existing, requested_id, payload, start_s, end_s)

    conn.execute("BEGIN IMMEDIATE")
    try:
        # 锁内重读：并发的相同 request_key 可能已完成落库
        existing = conn.execute(
            "SELECT * FROM illumination_tickets WHERE request_key = ?",
            (payload.request_key,),
        ).fetchone()
        if existing is not None:
            conn.commit()  # 尚未写入，COMMIT 仅释放 IMMEDIATE 锁
            return _replay_create(conn, existing, requested_id, payload, start_s, end_s)

        exhibit = get_canonical_or_404(conn, requested_id)
        canonical_id = exhibit["id"]

        # 重算诊断事件与 calendar=display 剂量并固化快照——全部在同一事务内。
        snapshot = compute_snapshot(
            conn, canonical_id, start_s, end_s,
            max_rate_lux_per_s=payload.max_rate_lux_per_second,
            stuck_tol_lux=payload.stuck_tolerance_lux,
            min_stuck_s=payload.min_stuck_seconds,
        )
        digest = snapshot_digest(snapshot)
        ticket_id = uuid.uuid4().hex
        now_iso = _utc_now_iso()
        conn.execute(
            """INSERT INTO illumination_tickets
               (ticket_id, request_key, canonical_id, requested_id,
                start_s, end_s, max_rate_lux_per_second, stuck_tolerance_lux,
                min_stuck_seconds, digest, snapshot_json, status, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'open', ?)""",
            (
                ticket_id, payload.request_key, canonical_id, requested_id,
                start_s, end_s, payload.max_rate_lux_per_second,
                payload.stuck_tolerance_lux, payload.min_stuck_seconds,
                digest, json.dumps(snapshot, ensure_ascii=False, sort_keys=True),
                now_iso,
            ),
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    row = conn.execute(
        "SELECT * FROM illumination_tickets WHERE ticket_id = ?", (ticket_id,)
    ).fetchone()
    return _ticket_out(row)


def _replay_create(
    conn: sqlite3.Connection,
    row: sqlite3.Row,
    requested_id: str,
    payload: IlluminationTicketCreate,
    start_s: float,
    end_s: float,
) -> IlluminationTicketOut:
    """相同 request_key 重放：目标（规范档案/时间窗/诊断阈值）一致回放原件，否则 409。"""
    # 请求 ID（可能是别名）必须仍解析到首次创建时的规范档案；档案不存在 → 404。
    canonical = resolve_canonical(conn, requested_id)
    if canonical is None:
        raise HTTPException(status_code=404, detail=f"展品不存在: {requested_id}")
    same_target = (
        canonical == row["canonical_id"]
        and row["start_s"] == start_s
        and row["end_s"] == end_s
        and row["max_rate_lux_per_second"] == payload.max_rate_lux_per_second
        and row["stuck_tolerance_lux"] == payload.stuck_tolerance_lux
        and row["min_stuck_seconds"] == payload.min_stuck_seconds
    )
    if not same_target:
        raise HTTPException(
            status_code=409,
            detail=f"request_key {payload.request_key} 已绑定另一张处置单"
                   "（规范档案/时间窗/诊断阈值不同），不能换目标重复提交",
        )
    return _ticket_out(row, already=True)


# ---------- 结案 ----------

def close_ticket(
    conn: sqlite3.Connection,
    ticket_id: str,
    payload: IlluminationTicketCloseRequest,
) -> IlluminationTicketCloseOut:
    """把 open 处置单结案为 resolved 或 accepted；摘要核对失败 409 不写入。"""
    events_cleared: bool | None = None
    dose_exceeded: bool | None = None

    conn.execute("BEGIN IMMEDIATE")
    try:
        row = conn.execute(
            "SELECT * FROM illumination_tickets WHERE ticket_id = ?", (ticket_id,)
        ).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail=f"处置单不存在: {ticket_id}")
        if row["status"] != "open":
            raise HTTPException(
                status_code=409,
                detail=f"处置单 {ticket_id} 已结案（{row['status']}），"
                       "只能从 open 结案，不能重复结案",
            )

        has_evidence = bool(payload.receipt_ids or payload.correction_request_keys)
        if payload.decision == "resolved" and not has_evidence:
            raise HTTPException(
                status_code=422,
                detail="resolved 结案须给出涉及的存证收据 receipt_ids 或批次更正 "
                       "correction_request_keys 至少一项",
            )
        if payload.decision == "accepted" and has_evidence:
            raise HTTPException(
                status_code=422,
                detail="accepted 结案保留事件、只记录理由，不接受收据/更正引用",
            )

        # 档案可能已归并：沿别名解析到当前规范档案重算；归并本身会改变
        # 链标识/读数集合/日历归属，摘要随之不同而 409。
        canonical = resolve_canonical(conn, row["canonical_id"])
        if canonical is None:
            raise HTTPException(
                status_code=409,
                detail=f"规范档案 {row['canonical_id']} 已不存在，无法重算核对，结案被拒绝",
            )
        snapshot = compute_snapshot(
            conn, canonical, row["start_s"], row["end_s"],
            max_rate_lux_per_s=row["max_rate_lux_per_second"],
            stuck_tol_lux=row["stuck_tolerance_lux"],
            min_stuck_s=row["min_stuck_seconds"],
        )
        current_digest = snapshot_digest(snapshot)
        if current_digest != payload.expected_digest:
            raise HTTPException(
                status_code=409,
                detail="处置单摘要核对失败：读数、作废、来源优先级、日历或收据链已变化"
                       f"（期望 {payload.expected_digest}，当前重算 {current_digest}），"
                       "结案未写入；请重新 GET 取回 current_digest 后重试",
            )

        if payload.decision == "resolved":
            # 证据引用须属于本规范档案（收据在本链上 / 更正属于本档案）。
            for rid in payload.receipt_ids:
                rc = conn.execute(
                    "SELECT 1 FROM ingest_receipts WHERE receipt_id = ? AND chain_id = ?",
                    (rid, canonical),
                ).fetchone()
                if rc is None:
                    raise HTTPException(
                        status_code=422,
                        detail=f"存证收据 {rid} 不存在或不属于规范档案 {canonical} 的收据链",
                    )
            for key in payload.correction_request_keys:
                cc = conn.execute(
                    "SELECT 1 FROM reading_corrections WHERE request_key = ? AND canonical_id = ?",
                    (key, canonical),
                ).fetchone()
                if cc is None:
                    raise HTTPException(
                        status_code=422,
                        detail=f"批次更正 request_key {key} 不存在或不属于规范档案 {canonical}",
                    )
            # 举证：重算后原诊断事件全部消失，且 calendar=display 剂量未越限。
            original_events = json.loads(row["snapshot_json"])["events"]
            current_events = snapshot["events"]
            remaining = [e for e in original_events if e in current_events]
            events_cleared = not remaining
            dose_exceeded = bool(snapshot["dose"]["exceeded"])
            if not events_cleared:
                raise HTTPException(
                    status_code=409,
                    detail=f"resolved 结案证明失败：重算后仍有 {len(remaining)} 个"
                           "原诊断事件未消失，不能结案为 resolved",
                )
            if dose_exceeded:
                raise HTTPException(
                    status_code=409,
                    detail="resolved 结案证明失败：重算后 calendar=display 剂量仍越限，"
                           "不能结案为 resolved",
                )

        now_iso = _utc_now_iso()
        evidence = (
            {
                "receipt_ids": list(payload.receipt_ids),
                "correction_request_keys": list(payload.correction_request_keys),
            }
            if payload.decision == "resolved" else None
        )
        # 结案只写结案列：创建时固化的 snapshot_json 与 digest 保持不可变。
        conn.execute(
            """UPDATE illumination_tickets
               SET status = ?, closed_at = ?, decision = ?, close_note = ?,
                   evidence_json = ?, closed_digest = ?
               WHERE ticket_id = ?""",
            (
                payload.decision, now_iso, payload.decision, payload.note,
                json.dumps(evidence, ensure_ascii=False) if evidence is not None else None,
                current_digest, ticket_id,
            ),
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise

    row = conn.execute(
        "SELECT * FROM illumination_tickets WHERE ticket_id = ?", (ticket_id,)
    ).fetchone()
    return IlluminationTicketCloseOut(
        **_ticket_out(row).model_dump(exclude={"already_committed"}),
        events_cleared=events_cleared,
        dose_exceeded=dose_exceeded,
    )


# ---------- 查询 ----------

def get_ticket(conn: sqlite3.Connection, ticket_id: str) -> IlluminationTicketOut | None:
    """按 ticket_id 读取处置单；open 状态附带实时重算的 current_digest。"""
    row = conn.execute(
        "SELECT * FROM illumination_tickets WHERE ticket_id = ?", (ticket_id,)
    ).fetchone()
    if row is None:
        return None
    current_digest: str | None = None
    if row["status"] == "open":
        canonical = resolve_canonical(conn, row["canonical_id"])
        if canonical is not None:
            try:
                snap = compute_snapshot(
                    conn, canonical, row["start_s"], row["end_s"],
                    max_rate_lux_per_s=row["max_rate_lux_per_second"],
                    stuck_tol_lux=row["stuck_tolerance_lux"],
                    min_stuck_s=row["min_stuck_seconds"],
                )
                current_digest = snapshot_digest(snap)
            except HTTPException:
                # 例如日历已删除：无法重算，以 null 明示（结案同样会 409）
                current_digest = None
    return _ticket_out(row, current_digest=current_digest)


def list_tickets(
    conn: sqlite3.Connection, requested_id: str
) -> tuple[sqlite3.Row, list[IlluminationTicketOut]]:
    """列出规范档案的全部处置单（别名自动解析），按创建时刻与 ID 稳定排序。"""
    exhibit = get_canonical_or_404(conn, requested_id)
    rows = conn.execute(
        "SELECT * FROM illumination_tickets WHERE canonical_id = ? "
        "ORDER BY created_at, ticket_id",
        (exhibit["id"],),
    ).fetchall()
    return exhibit, [_ticket_out(r) for r in rows]
