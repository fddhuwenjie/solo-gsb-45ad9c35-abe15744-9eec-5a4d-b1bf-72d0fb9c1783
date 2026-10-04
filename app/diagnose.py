"""照度采集质量诊断：在规范档案的**完整时间线**上识别采集质量事件。

事件类型：
- ``timestamp_conflict``：同一时刻存在**照度不同**的原始读数，该时刻有歧义，
  不参与任何连续链并同时截断其前后侧的 stuck 链；同一时刻照度完全相同的
  多条读数先合并为时间线上的一个点（读数 ID 全部保留）；
- ``gap``：两个相邻无歧义读数的间隔**超过**展品 ``max_interval_seconds``
  （恰好等于不算 gap），缺测段两侧各自成链；
- ``jump``：间隔合规（不超过 max_interval_seconds）但照度变化速率
  ``|Δlux| / Δt`` **超过**调用方给定上限（恰好等于不算 jump）；
- ``stuck``：由相邻无歧义读数组成的**最大**连续链，链上每一对相邻读数的
  照度差都不超过卡死容差，且首末读数累计时长达到最短持续时长。
  gap、jump、照度差超过容差的相邻对、timestamp_conflict 与外部断点都会
  截断链。

``breaks`` 给出**外部仲裁断点**（多来源仲裁中保持冲突的时刻）：这些时刻
不落为时间线上的点，gap / jump / stuck 等连续链在其两侧各自重置，但**不
生成任何事件**（尤其不生成 ``timestamp_conflict``——仲裁冲突只在权威时间
线查询中保留其冲突状态）。这与原始读数自身的同时刻异值冲突不同：后者仍
产生 ``timestamp_conflict`` 事件。

事件先基于完整时间线一次性形成（与查询窗口无关），再由调用方筛选与窗口
**相交**的事件；筛选只做包含/排除，绝不裁剪事件的原始边界与相关读数 ID，
因此缩窄窗口不会改变任何事件本身。最终按 (起点, 类型) 稳定排序。
"""
from __future__ import annotations

from bisect import bisect_left
from dataclasses import dataclass

# 起点相同时的类型先后（实际数据中不同类型事件一般不会共用起点，
# 这里仅给出确定的总排序，保证输出稳定）
TYPE_ORDER: dict[str, int] = {
    "gap": 0,
    "jump": 1,
    "stuck": 2,
    "timestamp_conflict": 3,
}


@dataclass
class DiagnosisEvent:
    """一个采集质量事件；时间均为 Unix 秒（UTC）。"""

    type: str  # gap | jump | stuck | timestamp_conflict
    start: float
    end: float
    reading_ids: list[str]
    interval_seconds: float | None = None       # gap / jump：相邻读数间隔
    rate_lux_per_second: float | None = None    # jump：|Δlux| / Δt（lux/秒）
    duration_seconds: float | None = None       # stuck：首末读数累计时长
    lux_span: float | None = None               # stuck：链上照度跨度；conflict：同时刻不同值跨度


@dataclass
class _Point:
    """时间线上一个无歧义时刻：同时刻同值读数合并而成。"""

    ts: float
    lux: float
    ids: list[str]


def build_events(
    readings: list[tuple[str, float, float]],  # (reading_id, ts, lux)
    *,
    max_interval_s: float,
    max_rate_lux_per_s: float,
    stuck_tol_lux: float,
    min_stuck_s: float,
    breaks: list[float] | None = None,
) -> list[DiagnosisEvent]:
    """在完整时间线上构建全部质量事件（尚未按窗口筛选）。

    ``breaks`` 为外部仲裁断点（多来源仲裁保持冲突的时刻），须按时刻升序。
    断点不落点、不产生任何事件，只把连续链在其两侧重置：断点两侧相邻的
    权威点之间不形成 gap / jump，stuck 链同样在两侧分别起算。
    """
    # 按 (时刻, 读数 ID) 排序，保证同时刻读数相邻且 ID 顺序确定
    ordered = sorted(readings, key=lambda r: (r[1], r[0]))
    break_ts = sorted(breaks or [])

    def broken_between(lo: float, hi: float) -> bool:
        """(lo, hi) 内是否存在仲裁断点（开区间：与端点重合不影响该对）。"""
        i = bisect_left(break_ts, lo)
        return i < len(break_ts) and break_ts[i] < hi

    # ---- 按时刻分组：同值合并 / 异值记 timestamp_conflict ----
    groups: list[tuple[float, list[tuple[str, float]]]] = []
    for rid, ts, lux in ordered:
        if groups and groups[-1][0] == ts:
            groups[-1][1].append((rid, lux))
        else:
            groups.append((ts, [(rid, lux)]))

    events: list[DiagnosisEvent] = []
    run: list[_Point] = []

    def flush_stuck() -> None:
        """结束当前平稳链：时长达标则合并为一个最大 stuck 事件。"""
        if len(run) >= 2:
            duration = run[-1].ts - run[0].ts
            if duration >= min_stuck_s:
                luxs = [p.lux for p in run]
                ids: list[str] = []
                for p in run:
                    ids.extend(p.ids)
                events.append(DiagnosisEvent(
                    type="stuck",
                    start=run[0].ts,
                    end=run[-1].ts,
                    reading_ids=ids,
                    duration_seconds=duration,
                    lux_span=max(luxs) - min(luxs),
                ))
        run.clear()

    prev: _Point | None = None
    for ts, members in groups:
        lux_values = {lux for _, lux in members}
        ids = [rid for rid, _ in members]
        if len(lux_values) > 1:
            # 同一时刻照度不一致：有歧义，截断连续链，该时刻不落点
            flush_stuck()
            events.append(DiagnosisEvent(
                type="timestamp_conflict",
                start=ts,
                end=ts,
                reading_ids=ids,
                lux_span=max(lux_values) - min(lux_values),
            ))
            prev = None
            continue

        # 外部仲裁断点：两侧连续链各自重置，不产生 gap / jump / stuck，
        # 也不产生 timestamp_conflict（仲裁冲突仅保留在权威时间线查询中）。
        if prev is not None and broken_between(prev.ts, ts):
            flush_stuck()
            prev = None

        cur = _Point(ts=ts, lux=members[0][1], ids=ids)
        if prev is not None:
            dt = cur.ts - prev.ts  # 分组后必为正
            delta = abs(cur.lux - prev.lux)
            if dt > max_interval_s:
                # 间隔超限：gap，gap 两侧各自成链（新链从 cur 起）
                flush_stuck()
                events.append(DiagnosisEvent(
                    type="gap",
                    start=prev.ts,
                    end=cur.ts,
                    reading_ids=prev.ids + cur.ids,
                    interval_seconds=dt,
                ))
                run = [cur]
            elif delta / dt > max_rate_lux_per_s:
                # 间隔合规但速率超限：jump，同样截断
                flush_stuck()
                events.append(DiagnosisEvent(
                    type="jump",
                    start=prev.ts,
                    end=cur.ts,
                    reading_ids=prev.ids + cur.ids,
                    interval_seconds=dt,
                    rate_lux_per_second=delta / dt,
                ))
                run = [cur]
            elif delta > stuck_tol_lux:
                # 既非 gap 也非 jump，但照度差超过卡死容差：不能并入 stuck 链
                flush_stuck()
                run = [cur]
            else:
                # 相邻照度差在容差内：延续当前最大平稳链
                run.append(cur)
        else:
            run = [cur]
        prev = cur

    flush_stuck()
    return events


def intersect_window(
    events: list[DiagnosisEvent], start_s: float, end_s: float
) -> list[DiagnosisEvent]:
    """筛选与闭窗口 [start_s, end_s] 相交的事件；边界相接也算相交。

    只筛选，不改变事件的 start / end / reading_ids 等任何内容。
    """
    return [e for e in events if e.start <= end_s and e.end >= start_s]


def sort_events(events: list[DiagnosisEvent]) -> list[DiagnosisEvent]:
    """按 (起点, 类型) 稳定排序。"""
    return sorted(events, key=lambda e: (e.start, TYPE_ORDER[e.type]))
