"""照度上报批次更正：作废存证收据中实际插入的读数。

设计要点：
- 调用方提交规范档案（可用别名）、收据 ``receipt_id``、``request_key`` 与原因。
  服务只允许作废该收据中**实际插入**（``inserted``）且作废时**仍归属该规范
  档案、内容（ts/lux）与收据记录一致、尚未作废**的读数；重复请求内出现的
  插入 ID 按首次出现去重。任何一条不满足都视为前置状态已变化，整次 **409**、
  不产生任何作废。
- 以下前置变化均整次 409：
  * 收据链已变化——被更正收据不属于当前规范档案的链（例如归并后用来源别名
    更正来源链收据：链保留但读数已搬到目标档案），或整链校验失败（断链/
    内容被篡改）；
  * 归并状态变化——解析出的规范档案与收据写入链不一致（档案已被归并删除）；
  * 读数归属变化——读数已不存在于该档案（被归并去重/改名/覆盖/删除）、
    内容已变化（归并 keep_source 覆盖）或已被另一次更正作废。
  * 该收据此前已被另一条更正成功作废。
- 作废（readings 置 voided_at/voided_by）与更正记录在**同一个
  BEGIN IMMEDIATE 事务**内幂等落库；事务失败整体回滚。
- ``request_key`` 为幂等键：相同 request_key 重放首次结果
  （``already_committed=true``）；换目标（规范档案/收据/原因之一不同）→ 409。
- 收据行本身只增不改：收据内容、序号、prev/content/chain 哈希全部保持不变，
  作废标记只写在 readings 与 reading_corrections 中，查询收据时另行标出。
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone

from fastapi import HTTPException

from .merge import get_canonical_or_404, resolve_canonical
from .receipts import load_chain, verify_chain_rows
from .schemas import (
    ReadingCorrectionOut,
    ReadingCorrectionRequest,
    VoidedReadingOut,
)


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


class _CorrectionConflict(Exception):
    """前置状态已变化（收据链 / 归并状态 / 读数归属）或目标不一致，整次 409。"""


def _inserted_targets(payload_json: str) -> list[dict]:
    """从收据内容按请求顺序取出实际插入的读数（重复 ID 取首次出现）。"""
    content = json.loads(payload_json)
    inserted_ids = {r["id"] for r in content["outcome"]["results"] if r["status"] == "inserted"}
    targets: list[dict] = []
    seen: set[str] = set()
    for r in content["request"]["readings"]:
        if r["id"] in inserted_ids and r["id"] not in seen:
            seen.add(r["id"])
            # 旧收据没有 source_id 键：get 返回 None，对应未声明来源的读数
            targets.append({"id": r["id"], "ts": r["ts"], "lux": r["lux"],
                            "source_id": r.get("source_id")})
    return targets


def _load_replay(row: sqlite3.Row, already: bool) -> ReadingCorrectionOut:
    return ReadingCorrectionOut(
        **json.loads(row["result_json"]), already_committed=already
    )


def voids_for_rows(conn: sqlite3.Connection,
                   keys: list[tuple[str, int]]) -> dict[tuple[str, int], list[dict]]:
    """批量读取若干收据 (chain_id, seq) 的作废标记，供收据查询接口附加。"""
    if not keys:
        return {}
    where = " OR ".join(["(chain_id = ? AND seq = ?)"] * len(keys))
    params: list = [p for key in keys for p in key]
    rows = conn.execute(
        f"SELECT * FROM reading_corrections WHERE {where} ORDER BY created_at, request_key",
        params,
    ).fetchall()
    out: dict[tuple[str, int], list[dict]] = {}
    for r in rows:
        out.setdefault((r["chain_id"], r["seq"]), []).append({
            "request_key": r["request_key"],
            "reason": r["reason"],
            "voided_count": r["voided_count"],
            "voided_readings": json.loads(r["voided_json"]),
            "voided_at": r["created_at"],
        })
    return out


def correct_receipt(
    conn: sqlite3.Connection,
    requested_id: str,
    payload: ReadingCorrectionRequest,
) -> ReadingCorrectionOut:
    """在单个写事务内作废收据中实际插入且仍归属该档案的读数；幂等。"""
    # 幂等回放不依赖写锁之外的状态：先读一次（无行则进入写事务）。
    existing = conn.execute(
        "SELECT * FROM reading_corrections WHERE request_key = ?",
        (payload.request_key,),
    ).fetchone()
    if existing is not None:
        return _check_replay(conn, existing, requested_id, payload)

    conn.execute("BEGIN IMMEDIATE")
    try:
        # 锁内重读：并发的相同 request_key 可能已完成落库
        existing = conn.execute(
            "SELECT * FROM reading_corrections WHERE request_key = ?",
            (payload.request_key,),
        ).fetchone()
        if existing is not None:
            conn.commit()  # 尚未写入，COMMIT 仅释放 IMMEDIATE 锁
            return _check_replay(conn, existing, requested_id, payload)

        exhibit = get_canonical_or_404(conn, requested_id)
        canonical_id = exhibit["id"]

        receipt = conn.execute(
            "SELECT * FROM ingest_receipts WHERE receipt_id = ?",
            (payload.receipt_id,),
        ).fetchone()
        if receipt is None:
            raise HTTPException(
                status_code=404,
                detail=f"存证收据不存在: receipt_id={payload.receipt_id}",
            )

        # 归并状态 / 收据链变化：收据必须仍在当前规范档案自己的链上。
        # 归并后来源链原样保留但来源档案已删除、读数已搬到目标档案，
        # 用来源（或其别名）更正来源链收据时 canonical_id 为目标 ID，
        # 与 chain_id 不一致 → 整次 409。
        if receipt["chain_id"] != canonical_id:
            raise _CorrectionConflict(
                f"收据 {payload.receipt_id} 属于链 {receipt['chain_id']}，"
                f"不属于规范档案 {canonical_id} 的当前收据链（档案可能已归并），"
                "不能更正：收据链或归并状态已变化"
            )

        # 收据链变化（断链 / 序号 / 内容 / 哈希被篡改）→ 整次 409。
        verdict = verify_chain_rows(load_chain(conn, canonical_id))
        if not verdict.ok:
            failure = verdict.first_failure or {}
            raise _CorrectionConflict(
                f"规范档案 {canonical_id} 的存证收据链校验失败："
                f"第 {failure.get('seq')} 张（{failure.get('reason')}），更正被拒绝"
            )

        # 一张收据至多被成功更正一次。
        prior = conn.execute(
            "SELECT request_key FROM reading_corrections WHERE chain_id = ? AND seq = ?",
            (receipt["chain_id"], receipt["seq"]),
        ).fetchone()
        if prior is not None:
            raise _CorrectionConflict(
                f"收据 {payload.receipt_id}（链 {receipt['chain_id']} 第 "
                f"{receipt['seq']} 张）已被更正 request_key={prior['request_key']} 作废，"
                "不能再次更正"
            )

        targets = _inserted_targets(receipt["payload_json"])
        problems: list[str] = []
        voided: list[VoidedReadingOut] = []
        for t in targets:
            row = conn.execute(
                "SELECT ts, lux, source_id, voided_at FROM readings "
                "WHERE exhibit_id = ? AND reading_id = ?",
                (canonical_id, t["id"]),
            ).fetchone()
            # 读数归属变化：已不在该档案（归并去重/删除）、内容被覆盖
            # （归并 keep_source）或已被作废 —— 全部整次 409。
            if row is None:
                problems.append(
                    f"{t['id']}：读数已不在规范档案 {canonical_id} 下（读数归属已变化）"
                )
                continue
            if row["voided_at"] is not None:
                problems.append(f"{t['id']}：读数已被作废")
                continue
            # 来源标识同样是读数内容：收据记录的来源与当前不一致即视为归属已变化
            # （旧收据没有 source_id 键，_inserted_targets 以 None 兜底）。
            cols = row.keys()
            row_source = row["source_id"] if "source_id" in cols else None
            if (row["ts"] != _parse_iso_epoch(t["ts"]) or row["lux"] != t["lux"]
                    or row_source != t["source_id"]):
                problems.append(f"{t['id']}：读数内容与收据记录不一致（读数归属已变化）")
                continue
            voided.append(VoidedReadingOut(id=t["id"], ts=t["ts"], lux=t["lux"]))

        if problems:
            raise _CorrectionConflict(
                "收据中存在不能作废的读数，整次更正被拒绝（不做部分作废）："
                + "；".join(problems)
            )

        now_iso = _utc_now_iso()
        # 作废标记与更正记录同生共死：任一失败整体回滚。
        for v in voided:
            conn.execute(
                "UPDATE readings SET voided_at = ?, voided_by = ? "
                "WHERE exhibit_id = ? AND reading_id = ? AND voided_at IS NULL",
                (now_iso, payload.request_key, canonical_id, v.id),
            )
        result = ReadingCorrectionOut(
            requested_id=requested_id,
            canonical_id=canonical_id,
            receipt_id=payload.receipt_id,
            chain_id=receipt["chain_id"],
            seq=receipt["seq"],
            request_key=payload.request_key,
            reason=payload.reason,
            voided_count=len(voided),
            voided_readings=voided,
            voided_at=now_iso,
            already_committed=False,
        )
        result_data = result.model_dump(exclude={"already_committed"})
        conn.execute(
            """INSERT INTO reading_corrections
               (request_key, canonical_id, requested_id, receipt_id, chain_id, seq,
                reason, voided_count, voided_json, result_json, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                payload.request_key, canonical_id, requested_id, payload.receipt_id,
                receipt["chain_id"], receipt["seq"], payload.reason, len(voided),
                json.dumps([v.model_dump() for v in voided], ensure_ascii=False),
                json.dumps(result_data, ensure_ascii=False), now_iso,
            ),
        )
        conn.commit()
        return result

    except HTTPException:
        conn.rollback()
        raise
    except _CorrectionConflict as e:
        conn.rollback()
        raise HTTPException(status_code=409, detail=str(e))
    except Exception:
        conn.rollback()
        raise


def _check_replay(
    conn: sqlite3.Connection,
    row: sqlite3.Row,
    requested_id: str,
    payload: ReadingCorrectionRequest,
) -> ReadingCorrectionOut:
    """相同 request_key 重放：目标（规范档案/收据/原因）一致则回放，否则 409。"""
    # 请求 ID（可能是别名）必须仍解析到首次更正时的规范档案；档案不存在 → 404。
    canonical = resolve_canonical(conn, requested_id)
    if canonical is None:
        raise HTTPException(status_code=404, detail=f"展品不存在: {requested_id}")
    same_target = (
        canonical == row["canonical_id"]
        and row["receipt_id"] == payload.receipt_id
        and row["reason"] == payload.reason
    )
    if not same_target:
        raise HTTPException(
            status_code=409,
            detail=f"request_key {payload.request_key} 已绑定另一次更正"
                   "（收据/档案/原因不同），不能换目标重复提交",
        )
    return _load_replay(row, True)


def _parse_iso_epoch(ts: str) -> float:
    return datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()
