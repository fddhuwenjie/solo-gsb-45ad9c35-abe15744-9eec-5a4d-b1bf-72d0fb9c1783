"""跨盘点轮次的馆藏区域交接：证据核验、只增链续接与单事务固化。

语义（与需求逐条对应）：

- 提交展品 ID（可用归并别名）、来源与接收 ``round_id``、两端区域；
- 两轮都必须已固化对账，且**起始时间严格递增**（接收轮晚于来源轮）；
- 展品必须同时列在两轮的**预期清单**中，并在各轮**明确定位到所报区域**：
  缺失（无事件）、错位（定位区域 ≠ 预期/所报区域）、位置不明（信号歧义）
  或未列入预期，均不得交接（409）；
- **首次交接**以来源轮建链（链内序号从 1 起）；此后只能从上一笔交接的
  **接收轮与接收区域**续接——新一笔的来源轮/来源区域必须与上一笔的
  接收轮/接收区域一致；更早的旧轮次不能回填、不能覆盖当前去向；
- 证据核验（别名解析、两轮固化结果读取与逐轮定位核对）与写入在**同一个
  SQLite 写事务**（``BEGIN IMMEDIATE``）内完成：并发交接最多一笔生效，
  任何失败整体回滚，不留半笔记录；
- 固化每笔交接的两轮摘要（``request_digest``），提供当前区域查询与
  按轮次时间（= 链内序号）排序的交接历史；
- 交接记录只 INSERT、绝不 UPDATE。归并单侧有链时由 merge 模块把整链
  改挂到目标规范 ID（``rebind_handover_chain``），两侧都有链时归并被拒绝。

本模块只读写 ``area_handovers`` 与（经 inventory 模块只读）
``inventory_rounds``，不写照度读数、日历、处置单、合规凭证与收据链。
"""
from __future__ import annotations

import sqlite3

from fastapi import HTTPException

from .inventory import load_round
from .merge import resolve_canonical
from .schemas import (
    AreaHandoverChainOut,
    AreaHandoverOut,
    AreaHandoverRequest,
    AreaHandoverResponse,
)


# ---------- 轮次证据核验 ----------

def _load_rounds(
    conn: sqlite3.Connection, source_round_id: str, target_round_id: str
):
    """在当前事务内读取两轮固化结果；任一未固化 → 404/409。"""
    if source_round_id == target_round_id:
        raise HTTPException(
            status_code=422,
            detail="来源轮与接收轮不能是同一轮，跨轮交接要求两轮起始时间严格递增",
        )
    source = load_round(conn, source_round_id)
    target = load_round(conn, target_round_id)
    missing = [
        rid for rid, r in ((source_round_id, source), (target_round_id, target))
        if r is None
    ]
    if missing:
        raise HTTPException(
            status_code=404,
            detail=f"盘点轮次不存在或尚未对账，无法作为交接证据: {missing}",
        )
    return source, target


def _located_area(round_out, canonical_id: str) -> str | None:
    """展品在一轮固化结果中明确定位到的区域。

    只有列入预期且唯一明确定位在**预期区域**（即未出现在 missing /
    misplaced / unknown_position 任一组）才返回该区域；意外出现
    （不在预期清单）同样返回 None。返回 None 即不得交接。
    """
    bad = {m.canonical_id for m in round_out.missing}
    bad |= {m.canonical_id for m in round_out.misplaced}
    bad |= {u.canonical_id for u in round_out.unknown_position}
    bad |= {u.canonical_id for u in round_out.unexpected}
    for e in round_out.expectations:
        if e.canonical_id == canonical_id and canonical_id not in bad:
            return e.expected_area
    return None


def _require_located(round_out, canonical_id: str, reported_area: str,
                     role: str, round_id: str) -> None:
    """核验展品在该轮预期清单中且明确定位到所报区域，否则 409 拒绝交接。"""
    actual = _located_area(round_out, canonical_id)
    if actual is None:
        raise HTTPException(
            status_code=409,
            detail=f"展品 {canonical_id} 在{role}轮 {round_id} 中缺失、错位、位置不明"
                   "或未列入预期清单，不得交接",
        )
    if actual != reported_area:
        raise HTTPException(
            status_code=409,
            detail=f"展品 {canonical_id} 在{role}轮 {round_id} 中明确定位为 "
                   f"{actual}，与所报区域 {reported_area} 不一致，不得交接",
        )


# ---------- 固化交接 ----------

def _handover_out(row: sqlite3.Row, starts: dict[str, str]) -> AreaHandoverOut:
    return AreaHandoverOut(
        seq=row["seq"],
        source_round_id=row["source_round_id"],
        target_round_id=row["target_round_id"],
        source_area=row["source_area"],
        target_area=row["target_area"],
        source_round_start_at=starts[row["source_round_id"]],
        target_round_start_at=starts[row["target_round_id"]],
        source_round_digest=row["source_round_digest"],
        target_round_digest=row["target_round_digest"],
        created_at=row["created_at"],
    )


def create_handover(
    conn: sqlite3.Connection, payload: AreaHandoverRequest
) -> AreaHandoverResponse:
    """核验证据并在同一写事务内固化一笔跨轮区域交接。

    全部检查在 ``BEGIN IMMEDIATE`` 写锁内完成：并发交接最多一笔生效，
    任一失败整体回滚、不留半笔记录。
    """
    conn.execute("BEGIN IMMEDIATE")
    try:
        canonical = resolve_canonical(conn, payload.exhibit_id)
        if canonical is None:
            raise HTTPException(
                status_code=404, detail=f"展品不存在: {payload.exhibit_id}"
            )

        source, target = _load_rounds(
            conn, payload.source_round_id, payload.target_round_id
        )
        # 起始时间严格递增：接收轮必须晚于来源轮
        if target.start_at <= source.start_at:
            raise HTTPException(
                status_code=422,
                detail=f"接收轮 {target.round_id} 起始时间 {target.start_at} 不晚于 "
                       f"来源轮 {source.round_id} 起始时间 {source.start_at}，"
                       "跨轮交接要求两轮起始时间严格递增",
            )

        # 展品须列入两轮预期清单且在各轮明确定位于所报区域
        _require_located(source, canonical, payload.source_area, "来源", source.round_id)
        _require_located(target, canonical, payload.target_area, "接收", target.round_id)

        # 续接校验：首次交接以来源轮建链；此后只能从上一笔的接收轮/区域续接
        last = conn.execute(
            "SELECT * FROM area_handovers WHERE canonical_id = ? "
            "ORDER BY seq DESC LIMIT 1",
            (canonical,),
        ).fetchone()
        if last is not None:
            if last["target_round_id"] != payload.source_round_id:
                raise HTTPException(
                    status_code=409,
                    detail=f"交接链只能从上一笔的接收轮 {last['target_round_id']} 续接，"
                           f"本次来源轮 {payload.source_round_id} 不是当前去向所在轮；"
                           "旧轮次不能回填覆盖当前去向",
                )
            if last["target_area"] != payload.source_area:
                raise HTTPException(
                    status_code=409,
                    detail=f"交接链当前去向为轮 {last['target_round_id']} 的区域 "
                           f"{last['target_area']}，本次所报来源区域 "
                           f"{payload.source_area} 与之不符，不能续接",
                )
            # 接收轮也必须严格晚于当前接收轮（时间不回头）；
            # target 轮 start 晚于 source 轮 start，而 source 轮 == 上一笔
            # target 轮，故该条件已由上面的递增检查覆盖，这里不再重复。

        seq = (last["seq"] + 1) if last is not None else 1
        try:
            conn.execute(
                "INSERT INTO area_handovers "
                "(canonical_id, seq, source_round_id, target_round_id, "
                " source_area, target_area, source_round_digest, target_round_digest) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (canonical, seq, source.round_id, target.round_id,
                 payload.source_area, payload.target_area,
                 source.request_digest, target.request_digest),
            )
            conn.commit()
        except sqlite3.IntegrityError:
            # 并发交接抢占了同一续接位置（同一轮在链中被重复占用）或序号竞争：
            # 只有一笔能生效，本笔整体回滚不留记录。
            conn.rollback()
            raise HTTPException(
                status_code=409,
                detail="并发交接竞争：同一展品的交接链已有并发一笔生效，"
                       "本次交接未写入任何记录，请读取当前去向后重试",
            )

        row = conn.execute(
            "SELECT * FROM area_handovers WHERE canonical_id = ? AND seq = ?",
            (canonical, seq),
        ).fetchone()
        assert row is not None
        out = _handover_out(row, {source.round_id: source.start_at,
                                  target.round_id: target.start_at})
        return AreaHandoverResponse(
            requested_id=payload.exhibit_id, canonical_id=canonical, **out.model_dump()
        )
    except HTTPException:
        conn.rollback()
        raise
    except Exception:
        conn.rollback()
        raise


# ---------- 查询：当前区域与交接历史 ----------

def load_handover_chain(
    conn: sqlite3.Connection, exhibit_id: str
) -> AreaHandoverChainOut | None:
    """读取展品当前区域与按轮次时间排序的交接历史；展品不存在返回 None。

    交接历史按链内序号升序返回（链按盘点轮次起始时间严格递增续接，
    故序号序即轮次时间序）；轮次起始时刻从固化的盘点结果中补齐，
    交接行本身是交接链的权威存证。
    """
    canonical = resolve_canonical(conn, exhibit_id)
    if canonical is None:
        return None

    rows = conn.execute(
        "SELECT * FROM area_handovers WHERE canonical_id = ? ORDER BY seq ASC",
        (canonical,),
    ).fetchall()
    round_ids = sorted(
        {r["source_round_id"] for r in rows} | {r["target_round_id"] for r in rows}
    )
    # 盘点轮次固化后只增不删（见 inventory.py），交接行引用的轮次必然可取回；
    # 起始时刻从固化结果补齐，交接行本身才是交接链的权威存证。
    starts: dict[str, str] = {}
    for rid in round_ids:
        round_out = load_round(conn, rid)
        assert round_out is not None, f"交接证据引用的盘点轮次固化结果缺失: {rid}"
        starts[rid] = round_out.start_at

    history = [_handover_out(r, starts) for r in rows]
    if rows:
        last = rows[-1]
        current_round_id = last["target_round_id"]
        current_area = last["target_area"]
    else:
        current_round_id = None
        current_area = None
    return AreaHandoverChainOut(
        requested_id=exhibit_id,
        canonical_id=canonical,
        current_round_id=current_round_id,
        current_area=current_area,
        history=history,
    )


# ---------- 归并支持：单侧链改挂，双侧链拒绝 ----------

def handover_counts(conn: sqlite3.Connection, *canonical_ids: str) -> dict[str, int]:
    """统计各规范档案的交接笔数（供归并预演展示冲突）。"""
    counts: dict[str, int] = {}
    for cid in canonical_ids:
        counts[cid] = conn.execute(
            "SELECT COUNT(*) AS n FROM area_handovers WHERE canonical_id = ?", (cid,)
        ).fetchone()["n"]
    return counts


def rebind_handover_chain(
    conn: sqlite3.Connection, source_id: str, target_id: str
) -> int:
    """在归并写事务内把来源档案的交接链整链改挂到目标规范档案。

    仅允许在**目标没有交接链**时调用（双侧都有链由调用方提前拒绝归并）。
    改挂保持序号与全部证据行不变；归并后用目标 ID 或来源别名查询到的
    是同一条链。返回改挂行数。
    """
    cur = conn.execute(
        "UPDATE area_handovers SET canonical_id = ? WHERE canonical_id = ?",
        (target_id, source_id),
    )
    return cur.rowcount
