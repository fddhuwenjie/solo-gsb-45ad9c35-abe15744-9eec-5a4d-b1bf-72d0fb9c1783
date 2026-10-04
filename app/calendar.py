"""展陈开放日历：IANA 时区 + 每周开放时段 + 按本地日期替换的例外，展开为绝对开放区间。

语义约定：
- 每件展品最多一份日历：IANA 时区、若干每周开放时段（按本地星期几，
  1=周一 … 7=周日，ISO 8601）、若干按本地日期整体替换当日每周规则的例外
  （例外日 windows 为空表示当日闭馆）；
- 时段内**结束时间不晚于开始时间**表示跨午夜：窗口延伸到次日同一本地时刻
  （如 22:00–02:00；00:00–00:00 表示全天 24 小时开放）；
- 展开到绝对时间（Unix 秒）时，重叠或首尾相接的区间合并；
- 夏令时边界规则：
  - 本地边界时刻**不存在**（春季拨快落入空档）→ 顺延到首个有效时刻；
  - 本地边界时刻**重复**（秋季回拨）→ 开始边界取较早瞬间、结束边界取较晚瞬间，
    因此重复的一小时会被完整计入开放时长。
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

SECONDS_PER_DAY = 86400


@dataclass
class CalendarConfig:
    """一件展品的展陈开放日历（秒级本地时刻，0 <= t < 86400）。"""

    timezone: str
    weekly: dict[int, list[tuple[int, int]]] = field(default_factory=dict)   # isoweekday -> [(start_sec, end_sec)]
    exceptions: dict[date, list[tuple[int, int]]] = field(default_factory=dict)  # 本地日期 -> 当日窗口（空 = 闭馆）


# ---------- 夏令时边界解析 ----------

def _classify(naive: datetime, tz: ZoneInfo) -> str:
    """本地时刻分类：normal（唯一）/ ambiguous（重复）/ nonexistent（不存在）。"""
    d0 = naive.replace(tzinfo=tz, fold=0)
    d1 = naive.replace(tzinfo=tz, fold=1)
    if d0.utcoffset() == d1.utcoffset():
        return "normal"
    rt0 = d0.astimezone(timezone.utc).astimezone(tz).replace(tzinfo=None)
    rt1 = d1.astimezone(timezone.utc).astimezone(tz).replace(tzinfo=None)
    if rt0 == naive and rt1 == naive:
        return "ambiguous"
    return "nonexistent"


def _first_valid_epoch(naive: datetime, tz: ZoneInfo) -> float:
    """不存在的本地时刻顺延到的首个有效瞬间（Unix 秒）。

    fold=0 用空档前的偏移、fold=1 用空档后的偏移，两个换算结果正好夹住
    时区切换瞬间；在二者之间二分找到 utcoffset 翻转的整秒，即空档结束后
    首个有效的本地时刻（IANA 数据库的切换都发生在整秒上）。
    """
    lo = int(naive.replace(tzinfo=tz, fold=1).timestamp())  # 较早瞬间，切换前偏移
    hi = int(naive.replace(tzinfo=tz, fold=0).timestamp())  # 较晚瞬间，切换后偏移
    off_lo = datetime.fromtimestamp(lo, timezone.utc).astimezone(tz).utcoffset()
    # 不变式：lo 处偏移 == off_lo，hi 处偏移 != off_lo
    while hi - lo > 1:
        mid = (lo + hi) // 2
        if datetime.fromtimestamp(mid, timezone.utc).astimezone(tz).utcoffset() == off_lo:
            lo = mid
        else:
            hi = mid
    return float(hi)


def resolve_boundary(naive: datetime, tz: ZoneInfo, *, is_start: bool) -> float:
    """把本地边界时刻解析为绝对瞬间（Unix 秒）。

    - 正常时刻：唯一瞬间；
    - 重复时刻：开始边界取较早瞬间（fold=0），结束边界取较晚瞬间（fold=1）；
    - 不存在时刻：顺延到首个有效瞬间。
    """
    kind = _classify(naive, tz)
    if kind == "normal":
        return naive.replace(tzinfo=tz, fold=0).timestamp()
    if kind == "ambiguous":
        return naive.replace(tzinfo=tz, fold=0 if is_start else 1).timestamp()
    return _first_valid_epoch(naive, tz)


# ---------- 日历展开 ----------

def _windows_for_date(cfg: CalendarConfig, day: date) -> list[tuple[int, int]]:
    """某本地日期生效的窗口：例外日整体替换每周规则。"""
    if day in cfg.exceptions:
        return cfg.exceptions[day]
    return cfg.weekly.get(day.isoweekday(), [])


def open_intervals(cfg: CalendarConfig, range_start_s: float, range_end_s: float) -> list[tuple[float, float]]:
    """把 [range_start_s, range_end_s] 可能触及的开放时段展开为绝对区间（Unix 秒）。

    返回按起点升序、互不重叠（首尾相接亦已合并）的区间列表。
    跨午夜窗口从前一日延伸而来，因此展开范围向前多取一个本地日期。
    """
    tz = ZoneInfo(cfg.timezone)
    first_day = datetime.fromtimestamp(range_start_s, tz).date() - timedelta(days=1)
    last_day = datetime.fromtimestamp(range_end_s, tz).date()

    raw: list[tuple[float, float]] = []
    day = first_day
    while day <= last_day:
        midnight = datetime.combine(day, time())
        next_midnight = midnight + timedelta(days=1)
        for start_sec, end_sec in _windows_for_date(cfg, day):
            a = resolve_boundary(midnight + timedelta(seconds=start_sec), tz, is_start=True)
            if end_sec > start_sec:
                end_day_midnight = midnight
            else:
                # 结束不晚于开始：跨午夜到次日同一本地时刻
                end_day_midnight = next_midnight
            b = resolve_boundary(end_day_midnight + timedelta(seconds=end_sec), tz, is_start=False)
            if b > a:
                raw.append((a, b))
        day += timedelta(days=1)

    raw.sort()
    merged: list[list[float]] = []
    for a, b in raw:
        if merged and a <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], b)
        else:
            merged.append([a, b])
    return [(a, b) for a, b in merged]


# ---------- 存取 ----------

def load_calendar(conn: sqlite3.Connection, exhibit_id: str) -> CalendarConfig | None:
    """读取展品的展陈日历；未配置返回 None。exhibit_id 须为规范档案 ID。"""
    row = conn.execute(
        "SELECT timezone FROM exhibit_calendars WHERE exhibit_id = ?", (exhibit_id,)
    ).fetchone()
    if row is None:
        return None
    cfg = CalendarConfig(timezone=row["timezone"])
    for r in conn.execute(
        "SELECT weekday, start_sec, end_sec FROM calendar_weekly_windows "
        "WHERE exhibit_id = ? ORDER BY weekday, start_sec, end_sec",
        (exhibit_id,),
    ).fetchall():
        cfg.weekly.setdefault(r["weekday"], []).append((r["start_sec"], r["end_sec"]))
    days = conn.execute(
        "SELECT date FROM calendar_exception_days WHERE exhibit_id = ? ORDER BY date",
        (exhibit_id,),
    ).fetchall()
    for d in days:
        day = date.fromisoformat(d["date"])
        wins = conn.execute(
            "SELECT start_sec, end_sec FROM calendar_exception_windows "
            "WHERE exhibit_id = ? AND date = ? ORDER BY start_sec, end_sec",
            (exhibit_id, d["date"]),
        ).fetchall()
        cfg.exceptions[day] = [(w["start_sec"], w["end_sec"]) for w in wins]
    return cfg


def save_calendar(conn: sqlite3.Connection, exhibit_id: str, cfg: CalendarConfig) -> None:
    """整体替换展品日历（调用方负责提交事务）；级联清理旧窗口。"""
    conn.execute("DELETE FROM exhibit_calendars WHERE exhibit_id = ?", (exhibit_id,))
    conn.execute(
        "INSERT INTO exhibit_calendars (exhibit_id, timezone) VALUES (?, ?)",
        (exhibit_id, cfg.timezone),
    )
    for weekday in sorted(cfg.weekly):
        for start_sec, end_sec in cfg.weekly[weekday]:
            conn.execute(
                "INSERT INTO calendar_weekly_windows (exhibit_id, weekday, start_sec, end_sec) "
                "VALUES (?, ?, ?, ?)",
                (exhibit_id, weekday, start_sec, end_sec),
            )
    for day in sorted(cfg.exceptions):
        conn.execute(
            "INSERT INTO calendar_exception_days (exhibit_id, date) VALUES (?, ?)",
            (exhibit_id, day.isoformat()),
        )
        for start_sec, end_sec in cfg.exceptions[day]:
            conn.execute(
                "INSERT INTO calendar_exception_windows (exhibit_id, date, start_sec, end_sec) "
                "VALUES (?, ?, ?, ?)",
                (exhibit_id, day.isoformat(), start_sec, end_sec),
            )


def delete_calendar(conn: sqlite3.Connection, exhibit_id: str) -> bool:
    """删除展品日历；返回是否原本存在。"""
    cur = conn.execute("DELETE FROM exhibit_calendars WHERE exhibit_id = ?", (exhibit_id,))
    return cur.rowcount > 0
