"""光照剂量核算核心算法。

语义约定：
- 梯形法累计：相邻两条读数之间照度按线性变化处理，
  剂量 = (lux_a + lux_b) / 2 * Δt(小时)。
- 缺测段不插值：相邻读数间隔一旦**超过**最大采样间隔，该段不参与积分；
  窗口起止时刻可由相邻读数（含窗口外的最近一条）线性裁剪后覆盖。
- 断点不连通：``break_ts`` 给出多来源仲裁保持冲突的时刻（升序）。
  断点两侧的相邻权威点**绝不直接相连**——即使间隔合规也不积分，缺测、
  截至窗口末累计剂量、首次越线、covered/unknown/closed 统计均按断点后
  的各段分别处理；冲突时刻本身不作为积分端点。
- 不带日历（``open_intervals=None``）时，窗口内时间只分两类：被有效段覆盖的
  部分计入 covered_seconds，其余全部计入 unknown_seconds（窗口前/后无读数、
  跨缺测段、跨仲裁断点等）。
- 带展陈日历（``open_intervals`` 为绝对开放区间列表）时，梯形积分只在开放
  区间内裁剪：窗口内闭馆时长计入 closed_seconds，开放但未被有效段覆盖的
  时长计入 unknown_seconds，三者之和等于窗口时长。
- known_dose_luxh 只表示所查窗口 [start, end] 内的有效剂量；
  remaining_luxh / exceeded / first_exceeded_at 则依据**截至窗口末的
  全历史累计**计算（带日历时只累计开放历史，且同样不跨过断点），因此首次
  越线时刻不会随查询窗口缩窄而改变。
- 剂量在查询时实时计算，乱序到达或补传的读数会立即反映到结果中。
"""
from __future__ import annotations

import math
from bisect import bisect_left
from collections.abc import Iterator
from dataclasses import dataclass


@dataclass
class DoseResult:
    known_dose_luxh: float            # 窗口内已知（可积分）剂量，lux·h
    unknown_seconds: float            # 窗口内缺测未知时长（秒），未插值
    covered_seconds: float            # 窗口内被有效积分覆盖的时长（秒）
    remaining_luxh: float             # 截至窗口末的全历史剩余额度（越线时为负）
    exceeded: bool                    # 截至窗口末全历史是否已越线
    first_exceeded_at: float | None   # 全历史首次越线时刻（Unix 秒），未越线为 None
    reading_count: int                # 窗口内（含端点）的读数条数（不含窗口外相邻点）
    closed_seconds: float | None = None  # 窗口内闭馆时长（秒）；仅日历模式有值


def _crossing_offset(l0: float, l1: float, T: float, target_luxs: float) -> float | None:
    """在长度 T 秒的段内（照度由 l0 线性变化到 l1），求累计 target_luxs（lux·s）所需的秒数。

    dose(x) = l0*x + (l1-l0)*x^2 / (2T) = target_luxs
    """
    if target_luxs <= 0.0:
        return 0.0
    a = (l1 - l0) / (2.0 * T)
    b = l0
    if abs(a) < 1e-12:  # 照度恒定，退化为线性
        return target_luxs / b if b > 0.0 else None
    disc = b * b + 2.0 * (l1 - l0) / T * target_luxs
    if disc < 0.0:
        return None
    sqrt_d = math.sqrt(disc)
    roots = [(-b + sqrt_d) / (2.0 * a), (-b - sqrt_d) / (2.0 * a)]
    valid = [r for r in roots if 0.0 < r <= T]
    return min(valid) if valid else None


def _clip_dose_luxh(l0: float, l1: float, dt_s: float) -> float:
    """长度 dt_s 秒、照度由 l0 线性变化到 l1 的子段剂量（lux·h）。"""
    return (l0 + l1) / 2.0 * dt_s / 3600.0


def _lux_at(l0: float, l1: float, frac: float) -> float:
    """照度由 l0 线性变化到 l1 时，frac∈[0,1] 处的照度。"""
    return l0 + (l1 - l0) * frac


def _pieces(
    seg_lo: float,
    seg_hi: float,
    clip_lo: float,
    clip_hi: float,
    open_intervals: list[tuple[float, float]] | None,
) -> Iterator[tuple[float, float]]:
    """读数段 [seg_lo, seg_hi] 先按口径裁剪到 [clip_lo, clip_hi]，再按需与开放区间求交。

    ``open_intervals`` 为 None（不带日历）时裁剪结果即单个连续段；
    否则只保留落在开放区间内的子段（区间已按起点升序、互不重叠）。
    """
    lo = max(seg_lo, clip_lo)
    hi = min(seg_hi, clip_hi)
    if hi <= lo:
        return
    if open_intervals is None:
        yield lo, hi
        return
    for a, b in open_intervals:
        if b <= lo:
            continue
        if a >= hi:
            break
        p_lo = max(lo, a)
        p_hi = min(hi, b)
        if p_hi > p_lo:
            yield p_lo, p_hi


def compute_dose(
    readings: list[tuple[float, float]],  # 见下方取值要求，须按 ts 升序
    start_s: float,
    end_s: float,
    max_interval_s: float,
    dose_limit_luxh: float,
    open_intervals: list[tuple[float, float]] | None = None,
    break_ts: list[float] | None = None,
) -> DoseResult:
    """核算时段剂量。

    ``readings`` 必须包含：
    - 窗口内（含端点）的全部权威点；
    - 窗口起点之前最近的一个权威点（若存在）；
    - 窗口终点之后最近的一个权威点（若存在）。
    这样窗口起止处才能按相邻权威点线性裁剪，而截至窗口末的全历史累计
    （读数自然按 ts 升序）也能从最早一条完整算起。

    ``break_ts`` 为仲裁保持冲突的时刻（升序）：任意两个相邻权威点之间
    若存在断点，则该对**绝不构成积分段**（与间隔是否超限无关），两个口径
    都不累计，断点时长在窗口口径中按缺测处理（日历模式下开放部分计入
    unknown、闭馆部分计入 closed）。

    ``open_intervals`` 为展陈日历展开后的绝对开放区间（Unix 秒，升序、互不
    重叠），须覆盖从最早读数到窗口末的全部相关时间；传入后积分只在开放
    区间内裁剪，历史口径也只累计开放历史。为 None 时保持原有口径。
    """
    breaks = sorted(break_ts or [])

    def broken_between(lo: float, hi: float) -> bool:
        """(lo, hi) 内是否存在仲裁断点（开区间：与端点重合不影响该对）。"""
        i = bisect_left(breaks, lo)
        return i < len(breaks) and breaks[i] < hi

    window_known = 0.0
    covered = 0.0
    history_known = 0.0
    first_exceeded_at: float | None = None

    prev_ts: float | None = None
    prev_lux = 0.0
    for ts, lux in readings:
        if prev_ts is not None:
            dt = ts - prev_ts
            # 间隔超限或两点之间存在仲裁断点：不插值、不连通，两个口径都不累计
            if dt > 0.0 and dt <= max_interval_s and not broken_between(prev_ts, ts):
                # ---- 窗口口径：与 [start_s, end_s]（及开放区间）求交后梯形裁剪 ----
                for lo, hi in _pieces(prev_ts, ts, start_s, end_s, open_intervals):
                    l_lo = _lux_at(prev_lux, lux, (lo - prev_ts) / dt)
                    l_hi = _lux_at(prev_lux, lux, (hi - prev_ts) / dt)
                    window_known += _clip_dose_luxh(l_lo, l_hi, hi - lo)
                    covered += hi - lo

                # ---- 全历史口径：截至 end_s（日历模式下只计开放区间），
                #      逐段累计并判定首次越线 ----
                for lo, hi in _pieces(prev_ts, ts, float("-inf"), end_s, open_intervals):
                    h_dt = hi - lo
                    h_lux_lo = _lux_at(prev_lux, lux, (lo - prev_ts) / dt)
                    h_lux_hi = _lux_at(prev_lux, lux, (hi - prev_ts) / dt)
                    h_seg = _clip_dose_luxh(h_lux_lo, h_lux_hi, h_dt)
                    if first_exceeded_at is None and history_known + h_seg >= dose_limit_luxh:
                        need_luxs = (dose_limit_luxh - history_known) * 3600.0
                        offset = _crossing_offset(h_lux_lo, h_lux_hi, h_dt, need_luxs)
                        first_exceeded_at = lo + (offset if offset is not None else h_dt)
                    history_known += h_seg
            # 间隔超过上限或跨仲裁断点：缺测/冲突段不插值，两个口径都不累计
        prev_ts, prev_lux = ts, lux

    window_len = end_s - start_s
    closed: float | None = None
    if open_intervals is None:
        unknown = max(0.0, window_len - covered)
    else:
        open_in_window = 0.0
        for a, b in open_intervals:
            lo = max(a, start_s)
            hi = min(b, end_s)
            if hi > lo:
                open_in_window += hi - lo
        closed = max(0.0, window_len - open_in_window)
        unknown = max(0.0, open_in_window - covered)
    reading_count = sum(1 for ts, _ in readings if start_s <= ts <= end_s)

    return DoseResult(
        known_dose_luxh=window_known,
        unknown_seconds=unknown,
        covered_seconds=covered,
        remaining_luxh=dose_limit_luxh - history_known,
        exceeded=history_known >= dose_limit_luxh,
        first_exceeded_at=first_exceeded_at,
        reading_count=reading_count,
        closed_seconds=closed,
    )
