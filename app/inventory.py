"""RFID 馆藏盘点对账：固定窗口定位、四类差异、轮次摘要绑定与并发固化。

定位算法（纯函数，便于单测）：
- 从盘点开始时刻起按 ``window_seconds`` 划分固定窗口，窗口下标
  ``floor((ts - start_at) / window_seconds)``；早于开始时刻的事件忽略；
  分窗在**整数纳秒 epoch 与精确有理窗口长度**上进行（见 ``epoch_nanos``、
  ``window_index``），不经过浮点 epoch 相减、也不使用任何固定容差，
  因而半开区间语义严格成立：恰好落在边界的事件进入更高窗口，
  边界下方哪怕一纳秒的真实偏移仍留在低窗；
- 每件展品在**每个窗口、每个区域**只保留最高 RSSI（同值不影响结果）；
- 取该展品**最后一个有事件的窗口**判定位置：
  - 窗口内只有一个区域 → 直接定位到该区域；
  - 出现多个区域时，比较最强与次强 RSSI：差值**不足阈值（严格小于）**
    记为位置不明，恰好等于阈值仍可定位；同 RSSI 时按区域标识字典序决胜，
    保证结果确定。

对账分类（四组互斥，按规范 ID 稳定排序）：缺失 / 错位 / 意外出现 / 位置不明；
预期区域内且位置可判的正常项不出现在任何列表中。

固化语义：
- ``round_id`` 与**规范化请求摘要**（``request_digest``，SHA-256）一一绑定：
  时间统一为 UTC、展品 ID 经别名解析为规范档案、事件/预期排序去重后取摘要；
- 相同请求重放 → 回放首次结果（``already_committed=true``）；
  不同请求 → 409；不存在的轮次 GET → 404；
- 首次固化在 ``BEGIN IMMEDIATE`` 写事务内完成，``round_id`` 主键兜底，
  并发提交只有一份结果能落库，竞争失败方按摘要异同得到 409 或回放；
- 对账是**只写 inventory_rounds** 的操作：不改写照度读数、不生成也不改写
  存证收据链（见 receipts.py），仅读 exhibits / exhibit_aliases 做别名解析。
"""
from __future__ import annotations

import json
import math
import sqlite3
from datetime import datetime, timezone
from fractions import Fraction

from fastapi import HTTPException

from .merge import resolve_canonical
from .receipts import content_hash_of
from .schemas import (
    InventoryExpectationOut,
    InventoryMissingOut,
    InventoryMisplacedOut,
    InventoryRoundOut,
    InventoryUnexpectedOut,
    InventoryUnknownPositionOut,
    ReconcileRequest,
    ensure_aware,
)

# 纳秒/秒：时间在整数纳秒域参与分窗，datetime 微秒精度下纳秒恒为 1000 的倍数。
_NANOS_PER_SECOND = 1_000_000_000
_EPOCH_ORDINAL = datetime(1970, 1, 1, tzinfo=timezone.utc).toordinal()


def epoch_nanos(dt: datetime) -> int:
    """感知时区的 datetime → 自 Unix epoch 起的**整数纳秒**。

    先精确换到 UTC（由 ``astimezone`` 完成日历换算），再按
    ``(序数日差 × 86400 + 日内秒) × 1e9 + 微秒 × 1000`` 计算，
    全程不经过 float，避免 ``.timestamp()`` 在大 epoch 下引入
    亚微秒级舍入误差（该误差足以使 0.1 秒级窗口错分一窗）。
    """
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    dt = dt.astimezone(timezone.utc)
    ordinal_days = dt.toordinal() - _EPOCH_ORDINAL
    day_seconds = dt.hour * 3600 + dt.minute * 60 + dt.second
    return (ordinal_days * 86400 + day_seconds) * _NANOS_PER_SECOND + dt.microsecond * 1000


def window_length(window_s: float) -> Fraction | None:
    """请求中的窗口秒数（float）按其**十进制字面量**解释为精确有理数。

    JSON 请求体经 pydantic 得到二进制 float（如 0.1）；``repr`` 还原出
    最短的十进制写法，再由 ``Fraction`` 精确表示，使 ``window_seconds=0.1``
    真正按 1/10 秒、0.3 秒边界真正落在 3 倍窗长处参与分窗，而不是按
    浮点近似值 0.1000000000000000055… 计算。

    返回 None 表示非有限窗长（``Infinity`` 等，标准 JSON 客户端无法产生；
    仅宽容解析器可能送达）——此时所有事件退入第 0 窗，与历史行为一致。
    """
    if not math.isfinite(window_s):
        return None
    return Fraction(repr(window_s))


def to_iso(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, tz=timezone.utc).isoformat().replace("+00:00", "Z")


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


# ---------- 纯算法：窗口划分与定位 ----------

def window_index(elapsed_nanos: int, window_ns: Fraction | None) -> int:
    """事件所属窗口下标。

    半开区间 [k·w, (k+1)·w)：``elapsed_nanos`` 为事件相对盘点起点的整数
    纳秒偏移，``window_ns`` 为精确有理窗口长度（纳秒）。整数除法
    ``floor(p/q)`` 天然满足——**恰好落在边界进入高窗**（k·w 整除得 k），
    边界下方哪怕 1 纳秒仍留低窗；全程精确整数运算，不使用任何容差。
    ``window_ns`` 为 None（非有限窗长）时所有事件退入第 0 窗。
    """
    if window_ns is None:
        return 0
    return (elapsed_nanos * window_ns.denominator) // window_ns.numerator


def locate_tags(
    events: list[tuple[str, str, int, float]],
    start_ns: int,
    window_ns: Fraction | None,
    delta_threshold: float,
) -> tuple[dict[str, str | None], int, int]:
    """按窗口定位每件展品。

    ``events`` 元素为 (规范展品 ID, 区域, 事件时刻整数纳秒 epoch, RSSI)。
    返回 (定位结果, 使用事件数, 早于开始时刻被忽略的事件数)；
    定位结果为 None 表示该展品位置不明。
    """
    # tag -> 窗口下标 -> 区域 -> 窗口内最高 RSSI
    windows: dict[str, dict[int, dict[str, float]]] = {}
    used = 0
    before = 0
    for tag_id, area, ts_ns, rssi in events:
        if ts_ns < start_ns:
            before += 1
            continue
        used += 1
        idx = window_index(ts_ns - start_ns, window_ns)
        area_best = windows.setdefault(tag_id, {}).setdefault(idx, {})
        if area not in area_best or rssi > area_best[area]:
            area_best[area] = rssi

    positions: dict[str, str | None] = {}
    for tag_id, wins in windows.items():
        last_idx = max(wins)
        # 最强在前；同 RSSI 按区域标识字典序决胜，保证确定性
        ranked = sorted(wins[last_idx].items(), key=lambda kv: (-kv[1], kv[0]))
        if len(ranked) == 1:
            positions[tag_id] = ranked[0][0]
        else:
            best_area, best_rssi = ranked[0]
            second_rssi = ranked[1][1]
            # 差值严格小于阈值才判不明；恰好达到阈值仍定位
            positions[tag_id] = (
                None if best_rssi - second_rssi < delta_threshold else best_area
            )
    return positions, used, before


# ---------- 规范化请求摘要 ----------

def request_digest(
    start_s: float,
    window_s: float,
    delta_threshold: float,
    expectations: list[tuple[str, str]],
    events: list[tuple[str, str, float, float]],
) -> str:
    """计算规范化请求摘要（SHA-256）。

    规范化口径：开始时刻统一为 UTC ``Z`` 结尾 ISO 8601；展品 ID 一律为
    别名解析后的规范档案 ID；预期与事件按内容排序并去重（顺序、重复项、
    同一档案的不同别名写法都不影响摘要）；浮点经 receipts 的稳定序列化
    （``float.hex``）参与哈希。
    """
    expected_norm = [list(t) for t in sorted({(cid, area) for cid, area in expectations})]
    events_norm = [
        list(t)
        for t in sorted({(cid, area, to_iso(ts_s), rssi) for cid, area, ts_s, rssi in events})
    ]
    payload = {
        "start_at": to_iso(start_s),
        "window_seconds": window_s,
        "rssi_delta_threshold": delta_threshold,
        "expected": expected_norm,
        "events": events_norm,
    }
    return content_hash_of(payload)


# ---------- 别名解析 ----------

def _resolve_or_404(conn: sqlite3.Connection, requested_id: str) -> str:
    canonical = resolve_canonical(conn, requested_id)
    if canonical is None:
        raise HTTPException(status_code=404, detail=f"展品不存在: {requested_id}")
    return canonical


# ---------- 对账与固化 ----------

def reconcile_round(
    conn: sqlite3.Connection,
    round_id: str,
    payload: ReconcileRequest,
) -> tuple[InventoryRoundOut, bool]:
    """执行一轮盘点对账并固化；返回 (结果, 是否为重放)。

    别名解析、摘要计算、定位、分类与落库全部在同一个 ``BEGIN IMMEDIATE``
    事务内完成：任何校验失败（404/422）整体回滚、不留轮次行；并发提交由
    round_id 唯一主键与写锁保证只固化一份结果。
    """
    conn.execute("BEGIN IMMEDIATE")
    try:
        # 已存在的轮次：摘要相同则回放，不同则 409（锁内重读，无竞态）
        existing = conn.execute(
            "SELECT request_digest, result_json FROM inventory_rounds WHERE round_id = ?",
            (round_id,),
        ).fetchone()

        # 预期区域：别名解析为规范档案；解析后重复配置 → 422
        expectations: list[tuple[str, str, str]] = []  # (requested, canonical, area)
        expected_area: dict[str, str] = {}
        for item in payload.expected:
            canonical = _resolve_or_404(conn, item.exhibit_id)
            if canonical in expected_area:
                raise HTTPException(
                    status_code=422,
                    detail=f"展品 {item.exhibit_id}（规范档案 {canonical}）"
                           f"在预期清单中重复配置，每件展品每轮只能配置一个预期区域",
                )
            expected_area[canonical] = item.area
            expectations.append((item.exhibit_id, canonical, item.area))

        # 读头事件：展品 ID 同样解析为规范档案；保留精确时刻（datetime），
        # 摘要用 ISO 字符串、定位用整数纳秒 epoch，都不经过浮点相减
        events: list[tuple[str, str, datetime, float]] = []
        for ev in payload.events:
            canonical = _resolve_or_404(conn, ev.exhibit_id)
            events.append((canonical, ev.area, ensure_aware(ev.ts), ev.rssi))

        start_dt = ensure_aware(payload.start_at)
        start_s = start_dt.timestamp()
        digest = request_digest(
            start_s,
            payload.window_seconds,
            payload.rssi_delta_threshold,
            [(cid, area) for _, cid, area in expectations],
            [(cid, area, ts.timestamp(), rssi) for cid, area, ts, rssi in events],
        )

        if existing is not None:
            conn.commit()  # 尚未写入，COMMIT 仅释放 IMMEDIATE 锁
            if existing["request_digest"] != digest:
                raise HTTPException(
                    status_code=409,
                    detail=f"盘点轮次 {round_id} 已绑定另一份对账请求，"
                           f"不能用不同请求重复提交",
                )
            return InventoryRoundOut(**json.loads(existing["result_json"])), True

        # 分窗在整数纳秒 epoch + 精确有理窗长上进行，杜绝浮点边界误差
        start_ns = epoch_nanos(start_dt)
        window_frac = window_length(payload.window_seconds)
        window_ns = None if window_frac is None else window_frac * _NANOS_PER_SECOND
        locate_events = [
            (cid, area, epoch_nanos(ts), rssi) for cid, area, ts, rssi in events
        ]
        positions, used, before = locate_tags(
            locate_events, start_ns, window_ns, payload.rssi_delta_threshold
        )

        missing: list[InventoryMissingOut] = []
        misplaced: list[InventoryMisplacedOut] = []
        unknown_position: list[InventoryUnknownPositionOut] = []
        for canonical in sorted(expected_area):
            area = expected_area[canonical]
            if canonical not in positions:
                missing.append(InventoryMissingOut(
                    canonical_id=canonical, expected_area=area,
                ))
                continue
            actual = positions[canonical]
            if actual is None:
                # 有事件但信号歧义：位置不明优先于错位/正常判定
                unknown_position.append(InventoryUnknownPositionOut(
                    canonical_id=canonical, expected_area=area,
                ))
            elif actual != area:
                misplaced.append(InventoryMisplacedOut(
                    canonical_id=canonical, expected_area=area, actual_area=actual,
                ))

        unexpected: list[InventoryUnexpectedOut] = []
        for canonical in sorted(positions):
            if canonical in expected_area:
                continue
            actual = positions[canonical]
            if actual is None:
                # 未在预期清单中但读到、且多区域信号难分：同样记为位置不明
                unknown_position.append(InventoryUnknownPositionOut(
                    canonical_id=canonical, expected_area=None,
                ))
            else:
                unexpected.append(InventoryUnexpectedOut(
                    canonical_id=canonical, actual_area=actual,
                ))
        unknown_position.sort(key=lambda x: x.canonical_id)

        result = InventoryRoundOut(
            round_id=round_id,
            request_digest=digest,
            start_at=to_iso(start_s),
            window_seconds=payload.window_seconds,
            rssi_delta_threshold=payload.rssi_delta_threshold,
            expected_count=len(expected_area),
            observed_count=len(positions),
            event_count=len(events),
            events_used_count=used,
            events_before_start_count=before,
            expectations=[
                InventoryExpectationOut(
                    requested_id=requested, canonical_id=canonical, expected_area=area,
                )
                for requested, canonical, area in expectations
            ],
            missing=missing,
            misplaced=misplaced,
            unexpected=unexpected,
            unknown_position=unknown_position,
            reconciled_at=utc_now_iso(),
        )

        try:
            conn.execute(
                "INSERT INTO inventory_rounds (round_id, request_digest, result_json) "
                "VALUES (?, ?, ?)",
                (round_id, digest,
                 json.dumps(result.model_dump(), ensure_ascii=False, sort_keys=True)),
            )
            conn.commit()
        except sqlite3.IntegrityError:
            # 并发请求已抢先固化同一 round_id：回滚后重读，按摘要异同回放或 409
            conn.rollback()
            winner = conn.execute(
                "SELECT request_digest, result_json FROM inventory_rounds WHERE round_id = ?",
                (round_id,),
            ).fetchone()
            assert winner is not None
            if winner["request_digest"] == digest:
                return InventoryRoundOut(**json.loads(winner["result_json"])), True
            raise HTTPException(
                status_code=409,
                detail=f"盘点轮次 {round_id} 已被并发提交固化为另一份对账结果，"
                       f"本次请求未生效",
            )
        return result, False
    except HTTPException:
        conn.rollback()
        raise
    except Exception:
        conn.rollback()
        raise


def load_round(conn: sqlite3.Connection, round_id: str) -> InventoryRoundOut | None:
    """取回已固化的盘点轮次；不存在返回 None。"""
    row = conn.execute(
        "SELECT result_json FROM inventory_rounds WHERE round_id = ?", (round_id,)
    ).fetchone()
    if row is None:
        return None
    return InventoryRoundOut(**json.loads(row["result_json"]))
