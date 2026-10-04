"""展陈开放日历端到端测试：配置管理、开放区间裁剪、跨午夜、夏令时、归并保留目标日历。"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

# 2023-11-13 是周一（UTC）
MON = datetime(2023, 11, 13, 0, 0, tzinfo=timezone.utc).timestamp()
TUE = MON + 86400
HOUR = 3600


def iso(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, tz=timezone.utc).isoformat()


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("DB_PATH", str(tmp_path / "test.db"))
    from app.main import app

    with TestClient(app) as c:
        yield c


def make_exhibit(client: TestClient, limit: float = 100000.0, interval: float = 3600.0,
                 eid: str | None = None) -> str:
    body = {"name": "唐代绢画", "dose_limit_luxh": limit, "max_interval_seconds": interval}
    if eid is not None:
        body["id"] = eid
    resp = client.post("/exhibits", json=body)
    assert resp.status_code == 201, resp.text
    return resp.json()["canonical_id"]


def send(client: TestClient, exhibit_id: str, readings: list[dict]) -> dict:
    resp = client.post(f"/exhibits/{exhibit_id}/readings", json={"readings": readings})
    assert resp.status_code == 200, resp.text
    return resp.json()


def dose(client: TestClient, exhibit_id: str, start: float, end: float,
         calendar: str | None = None) -> dict:
    params = {"start": iso(start), "end": iso(end)}
    if calendar is not None:
        params["calendar"] = calendar
    resp = client.get(f"/exhibits/{exhibit_id}/dose", params=params)
    assert resp.status_code == 200, resp.text
    return resp.json()


def put_calendar(client: TestClient, exhibit_id: str, payload: dict) -> dict:
    resp = client.put(f"/exhibits/{exhibit_id}/calendar", json=payload)
    assert resp.status_code == 200, resp.text
    return resp.json()


def hourly_readings(start: float, hours: int, lux: float = 100.0,
                    step: float = HOUR, prefix: str = "r") -> list[dict]:
    return [
        {"id": f"{prefix}{i}", "ts": iso(start + i * step), "lux": lux}
        for i in range(hours + 1)
    ]


# ---------- 日历配置管理 ----------

def test_put_and_get_calendar_roundtrip(client):
    eid = make_exhibit(client)
    payload = {
        "timezone": "Asia/Shanghai",
        "weekly": [
            {"weekday": 1, "start": "09:00", "end": "17:00"},
            {"weekday": 2, "start": "09:30:30", "end": "12:00"},
            {"weekday": 6, "start": "22:00", "end": "02:00"},  # 跨午夜
        ],
        "exceptions": [
            {"date": "2026-10-01", "windows": []},
            {"date": "2026-10-02", "windows": [{"start": "10:00", "end": "16:00"}]},
        ],
    }
    out = put_calendar(client, eid, payload)
    assert out["requested_id"] == out["canonical_id"] == eid
    assert out["timezone"] == "Asia/Shanghai"
    assert out["weekly"] == [
        {"weekday": 1, "start": "09:00", "end": "17:00"},
        {"weekday": 2, "start": "09:30:30", "end": "12:00"},
        {"weekday": 6, "start": "22:00", "end": "02:00"},
    ]
    assert out["exceptions"] == [
        {"date": "2026-10-01", "windows": []},
        {"date": "2026-10-02", "windows": [{"start": "10:00", "end": "16:00"}]},
    ]
    assert out["updated_at"]

    got = client.get(f"/exhibits/{eid}/calendar")
    assert got.status_code == 200
    assert got.json() == out


def test_put_calendar_replaces_previous(client):
    eid = make_exhibit(client)
    put_calendar(client, eid, {"timezone": "UTC", "weekly": [
        {"weekday": 1, "start": "09:00", "end": "17:00"},
    ]})
    out = put_calendar(client, eid, {"timezone": "Europe/Paris", "weekly": [
        {"weekday": 3, "start": "10:00", "end": "12:00"},
    ], "exceptions": [{"date": "2026-12-25", "windows": []}]})
    assert out["timezone"] == "Europe/Paris"
    assert out["weekly"] == [{"weekday": 3, "start": "10:00", "end": "12:00"}]
    assert out["exceptions"] == [{"date": "2026-12-25", "windows": []}]


def test_calendar_validation(client):
    eid = make_exhibit(client)
    base = {"timezone": "UTC", "weekly": [{"weekday": 1, "start": "09:00", "end": "17:00"}]}
    # 未知 IANA 时区
    assert client.put(f"/exhibits/{eid}/calendar",
                      json={**base, "timezone": "Mars/Olympus"}).status_code == 422
    # 星期越界
    assert client.put(f"/exhibits/{eid}/calendar",
                      json={**base, "weekly": [{"weekday": 0, "start": "09:00", "end": "10:00"}]}
                      ).status_code == 422
    assert client.put(f"/exhibits/{eid}/calendar",
                      json={**base, "weekly": [{"weekday": 8, "start": "09:00", "end": "10:00"}]}
                      ).status_code == 422
    # 非法时刻
    for bad in ("25:00", "09:60", "9点", "09:00:99"):
        assert client.put(f"/exhibits/{eid}/calendar",
                          json={**base, "weekly": [{"weekday": 1, "start": bad, "end": "10:00"}]}
                          ).status_code == 422
    # 例外日期重复
    assert client.put(f"/exhibits/{eid}/calendar", json={**base, "exceptions": [
        {"date": "2026-10-01", "windows": []},
        {"date": "2026-10-01", "windows": [{"start": "10:00", "end": "12:00"}]},
    ]}).status_code == 422
    # 未配置的展品 → GET 404
    assert client.get(f"/exhibits/{eid}/calendar").status_code == 404
    # 展品不存在 → 404
    assert client.put("/exhibits/ghost/calendar", json=base).status_code == 404
    assert client.get("/exhibits/ghost/calendar").status_code == 404


def test_delete_calendar(client):
    eid = make_exhibit(client)
    put_calendar(client, eid, {"timezone": "UTC"})
    assert client.get(f"/exhibits/{eid}/calendar").status_code == 200
    assert client.delete(f"/exhibits/{eid}/calendar").status_code == 204
    assert client.get(f"/exhibits/{eid}/calendar").status_code == 404
    # 幂等：再删一次仍 204
    assert client.delete(f"/exhibits/{eid}/calendar").status_code == 204


# ---------- calendar=display 剂量核算 ----------

def test_dose_display_requires_configured_calendar(client):
    eid = make_exhibit(client)
    send(client, eid, [{"id": "r1", "ts": iso(MON), "lux": 100},
                       {"id": "r2", "ts": iso(MON + HOUR), "lux": 100}])
    resp = client.get(f"/exhibits/{eid}/dose",
                      params={"start": iso(MON), "end": iso(MON + HOUR), "calendar": "display"})
    assert resp.status_code == 409
    # 不带 calendar 参数不受影响
    d = dose(client, eid, MON, MON + HOUR)
    assert d["known_dose_luxh"] == pytest.approx(100.0)
    # 非法 calendar 取值 → 422
    assert client.get(f"/exhibits/{eid}/dose",
                      params={"start": iso(MON), "end": iso(MON + HOUR),
                              "calendar": "weekday"}).status_code == 422


def test_display_clips_to_open_hours_and_reports_closed(client):
    """工作日 09:00–17:00 开放：窗口为整个周一，闭馆时长单列，三者之和等于窗口。"""
    eid = make_exhibit(client)
    put_calendar(client, eid, {"timezone": "UTC", "weekly": [
        {"weekday": 1, "start": "09:00", "end": "17:00"},
    ]})
    send(client, eid, hourly_readings(MON, 18, lux=100.0))  # 周一 00:00–18:00 每小时

    d = dose(client, eid, MON, MON + 86400, calendar="display")
    assert d["covered_seconds"] == pytest.approx(8 * HOUR)   # 仅开放时段被覆盖
    assert d["closed_seconds"] == pytest.approx(16 * HOUR)   # 00–09 与 17–24 闭馆
    assert d["unknown_seconds"] == 0
    assert d["covered_seconds"] + d["unknown_seconds"] + d["closed_seconds"] == pytest.approx(86400)
    assert d["known_dose_luxh"] == pytest.approx(800.0)      # 100 lux × 8h
    assert d["reading_count"] == 19

    # 不带 calendar：同一窗口全天都可积分，且响应不含 closed_seconds
    plain = dose(client, eid, MON, MON + 86400)
    assert "closed_seconds" not in plain
    assert plain["covered_seconds"] == pytest.approx(18 * HOUR)
    assert plain["known_dose_luxh"] == pytest.approx(1800.0)


def test_display_open_time_without_coverage_counts_unknown(client):
    """开放但缺测的时长计入 unknown_seconds，而不是 closed_seconds。"""
    eid = make_exhibit(client)
    put_calendar(client, eid, {"timezone": "UTC", "weekly": [
        {"weekday": 1, "start": "09:00", "end": "17:00"},
    ]})
    send(client, eid, [
        {"id": "a", "ts": iso(MON + 9 * HOUR), "lux": 100},
        {"id": "b", "ts": iso(MON + 10 * HOUR), "lux": 100},
    ])
    d = dose(client, eid, MON, MON + 86400, calendar="display")
    assert d["covered_seconds"] == pytest.approx(HOUR)        # 09:00–10:00
    assert d["unknown_seconds"] == pytest.approx(7 * HOUR)    # 开放但无有效段
    assert d["closed_seconds"] == pytest.approx(16 * HOUR)
    assert d["covered_seconds"] + d["unknown_seconds"] + d["closed_seconds"] == pytest.approx(86400)
    assert d["known_dose_luxh"] == pytest.approx(100.0)


def test_display_no_readings_at_all(client):
    eid = make_exhibit(client, limit=500.0)
    put_calendar(client, eid, {"timezone": "UTC", "weekly": [
        {"weekday": 1, "start": "09:00", "end": "17:00"},
    ]})
    d = dose(client, eid, MON, MON + 86400, calendar="display")
    assert d["known_dose_luxh"] == 0
    assert d["covered_seconds"] == 0
    assert d["unknown_seconds"] == pytest.approx(8 * HOUR)
    assert d["closed_seconds"] == pytest.approx(16 * HOUR)
    assert d["remaining_luxh"] == pytest.approx(500.0)
    assert d["exceeded"] is False
    assert d["first_exceeded_at"] is None


def test_display_cross_midnight_window(client):
    """end <= start 表示跨午夜：22:00–02:00 的窗口延伸到次日。"""
    eid = make_exhibit(client, interval=7200.0)
    put_calendar(client, eid, {"timezone": "UTC", "weekly": [
        {"weekday": 1, "start": "22:00", "end": "02:00"},
    ]})
    send(client, eid, [
        {"id": "a", "ts": iso(MON + 21 * HOUR), "lux": 100},
        {"id": "b", "ts": iso(MON + 23 * HOUR), "lux": 100},
        {"id": "c", "ts": iso(TUE + HOUR), "lux": 100},
        {"id": "d", "ts": iso(TUE + 3 * HOUR), "lux": 100},
    ])
    # 窗口：周一 21:00 – 周二 03:00（6h），开放 22:00–02:00（4h）
    d = dose(client, eid, MON + 21 * HOUR, TUE + 3 * HOUR, calendar="display")
    assert d["covered_seconds"] == pytest.approx(4 * HOUR)
    assert d["closed_seconds"] == pytest.approx(2 * HOUR)  # 21–22 与 02–03
    assert d["unknown_seconds"] == 0
    assert d["known_dose_luxh"] == pytest.approx(400.0)


def test_display_full_day_window(client):
    """00:00–00:00（end == start）表示全天 24 小时开放。"""
    eid = make_exhibit(client)
    put_calendar(client, eid, {"timezone": "UTC", "weekly": [
        {"weekday": 1, "start": "00:00", "end": "00:00"},
    ]})
    send(client, eid, hourly_readings(MON, 24, lux=50.0))
    d = dose(client, eid, MON, MON + 86400, calendar="display")
    assert d["closed_seconds"] == 0
    assert d["covered_seconds"] == pytest.approx(86400)
    assert d["known_dose_luxh"] == pytest.approx(50.0 * 24)


def test_display_overlapping_windows_merge(client):
    """同一星期的重叠时段合并，覆盖时长不重复累计。"""
    eid = make_exhibit(client)
    put_calendar(client, eid, {"timezone": "UTC", "weekly": [
        {"weekday": 1, "start": "09:00", "end": "12:00"},
        {"weekday": 1, "start": "11:00", "end": "15:00"},
    ]})
    send(client, eid, hourly_readings(MON + 8 * HOUR, 8, lux=100.0))  # 08:00–16:00
    d = dose(client, eid, MON + 8 * HOUR, MON + 16 * HOUR, calendar="display")
    # 合并后开放 09:00–15:00 = 6h，而非 3h+4h=7h
    assert d["covered_seconds"] == pytest.approx(6 * HOUR)
    assert d["closed_seconds"] == pytest.approx(2 * HOUR)
    assert d["known_dose_luxh"] == pytest.approx(600.0)


def test_display_cross_midnight_window_merges_with_next_day(client):
    """跨午夜窗口与次日窗口相接/重叠时合并为一段开放区间。"""
    eid = make_exhibit(client, interval=7200.0)
    put_calendar(client, eid, {"timezone": "UTC", "weekly": [
        {"weekday": 1, "start": "22:00", "end": "02:00"},  # 周一 22:00 – 周二 02:00
        {"weekday": 2, "start": "01:00", "end": "03:00"},  # 周二 01:00 – 03:00
    ]})
    send(client, eid, hourly_readings(MON + 21 * HOUR, 6, lux=100.0))  # 周一 21:00 – 周二 03:00
    d = dose(client, eid, MON + 21 * HOUR, TUE + 3 * HOUR, calendar="display")
    # 合并后开放 22:00–03:00 = 5h（而非 4h+2h=6h）
    assert d["covered_seconds"] == pytest.approx(5 * HOUR)
    assert d["closed_seconds"] == pytest.approx(HOUR)  # 21:00–22:00
    assert d["unknown_seconds"] == 0
    assert d["known_dose_luxh"] == pytest.approx(500.0)


def test_display_exception_replaces_weekly_rule(client):
    """例外日整体替换每周规则：闭馆例外与自定义时段例外。"""
    eid = make_exhibit(client)
    put_calendar(client, eid, {
        "timezone": "UTC",
        "weekly": [{"weekday": 1, "start": "09:00", "end": "17:00"}],
        "exceptions": [
            {"date": "2023-11-13", "windows": []},                                   # 周一闭馆
            {"date": "2023-11-12", "windows": [{"start": "10:00", "end": "12:00"}]}, # 周日特开
        ],
    })
    send(client, eid, hourly_readings(MON - 86400, 48, lux=100.0))  # 周日 00:00 – 周二 00:00

    # 周一：虽有每周规则，但例外闭馆 → 全天 closed
    mon = dose(client, eid, MON, MON + 86400, calendar="display")
    assert mon["known_dose_luxh"] == 0
    assert mon["covered_seconds"] == 0
    assert mon["unknown_seconds"] == 0
    assert mon["closed_seconds"] == pytest.approx(86400)

    # 周日：本无每周规则，例外给出 10:00–12:00 → 仅 2h 开放
    sun = dose(client, eid, MON - 86400, MON, calendar="display")
    assert sun["covered_seconds"] == pytest.approx(2 * HOUR)
    assert sun["known_dose_luxh"] == pytest.approx(200.0)
    assert sun["closed_seconds"] == pytest.approx(22 * HOUR)


def test_display_history_and_first_exceeded_use_open_time_only(client):
    """剩余额度与首次越线按截至窗口末的开放历史计算；闭馆时段的读数不累计。"""
    eid = make_exhibit(client, limit=100.0, interval=3600.0)
    put_calendar(client, eid, {"timezone": "UTC", "weekly": [
        {"weekday": wd, "start": "10:00", "end": "11:00"} for wd in range(1, 8)
    ]})
    # 周一 08:00–12:00 每 30 分钟一条，恒定 100 lux
    send(client, eid, [
        {"id": f"r{i}", "ts": iso(MON + 8 * HOUR + i * 1800), "lux": 100}
        for i in range(9)
    ])
    d = dose(client, eid, MON + 8 * HOUR, MON + 12 * HOUR, calendar="display")
    # 只有 10:00–11:00 开放：累计 100 lux·h，恰好在 11:00 越线
    assert d["known_dose_luxh"] == pytest.approx(100.0)
    assert d["covered_seconds"] == pytest.approx(HOUR)
    assert d["closed_seconds"] == pytest.approx(3 * HOUR)
    assert d["exceeded"] is True
    assert d["remaining_luxh"] == pytest.approx(0.0)
    crossed = datetime.fromisoformat(d["first_exceeded_at"]).timestamp()
    assert crossed == pytest.approx(MON + 11 * HOUR, abs=1.0)

    # 不带 calendar：闭馆时段也累计，越线时刻提前到 09:00
    plain = dose(client, eid, MON + 8 * HOUR, MON + 12 * HOUR)
    assert plain["known_dose_luxh"] == pytest.approx(400.0)
    assert plain["remaining_luxh"] == pytest.approx(-300.0)
    crossed_plain = datetime.fromisoformat(plain["first_exceeded_at"]).timestamp()
    assert crossed_plain == pytest.approx(MON + 9 * HOUR, abs=1.0)


# ---------- 夏令时 ----------

def test_display_dst_spring_forward_nonexistent_boundary(client):
    """春季拨快：落入空档的开始边界顺延到首个有效时刻。

    America/New_York 2024-03-10（周日）02:00–03:00 不存在；
    每周日 02:30–03:30 的窗口展开为 03:00–03:30 EDT，即 07:00–07:30 UTC。
    """
    eid = make_exhibit(client)
    put_calendar(client, eid, {"timezone": "America/New_York", "weekly": [
        {"weekday": 7, "start": "02:30", "end": "03:30"},
    ]})
    t0 = datetime(2024, 3, 10, 6, 30, tzinfo=timezone.utc).timestamp()  # 01:30 EST
    send(client, eid, [
        {"id": "a", "ts": iso(t0), "lux": 120},              # 06:30 UTC = 01:30 EST（闭馆）
        {"id": "b", "ts": iso(t0 + 1800), "lux": 120},       # 07:00 UTC = 03:00 EDT
        {"id": "c", "ts": iso(t0 + 3600), "lux": 120},       # 07:30 UTC = 03:30 EDT
        {"id": "d", "ts": iso(t0 + 5400), "lux": 120},       # 08:00 UTC = 04:00 EDT（闭馆）
    ])
    d = dose(client, eid, t0, t0 + 5400, calendar="display")
    assert d["covered_seconds"] == pytest.approx(1800)   # 仅 07:00–07:30 UTC 开放
    assert d["closed_seconds"] == pytest.approx(3600)
    assert d["unknown_seconds"] == 0
    assert d["known_dose_luxh"] == pytest.approx(120 * 0.5)


def test_display_dst_fall_back_repeated_boundary(client):
    """秋季回拨：重复边界的开始取较早瞬间、结束取较晚瞬间。

    America/New_York 2024-11-03（周日）01:00–02:00 出现两次；
    每周日 01:30–01:45 的窗口展开为 01:30 EDT – 01:45 EST，
    即 05:30–06:45 UTC，共 75 分钟（重复的一小时被完整计入）。
    """
    eid = make_exhibit(client)
    put_calendar(client, eid, {"timezone": "America/New_York", "weekly": [
        {"weekday": 7, "start": "01:30", "end": "01:45"},
    ]})
    t0 = datetime(2024, 11, 3, 5, 30, tzinfo=timezone.utc).timestamp()  # 01:30 EDT
    send(client, eid, [
        {"id": "a", "ts": iso(t0), "lux": 60},          # 05:30 UTC = 01:30 EDT（较早瞬间）
        {"id": "b", "ts": iso(t0 + 2700), "lux": 60},   # 06:15 UTC = 01:15 EST
        {"id": "c", "ts": iso(t0 + 4500), "lux": 60},   # 06:45 UTC = 01:45 EST（较晚瞬间）
    ])
    d = dose(client, eid, t0, t0 + 4500, calendar="display")
    assert d["covered_seconds"] == pytest.approx(4500)  # 75 分钟
    assert d["closed_seconds"] == 0
    assert d["unknown_seconds"] == 0
    assert d["known_dose_luxh"] == pytest.approx(60 * 1.25)


# ---------- 归并与别名 ----------

def merge(client: TestClient, source: str, target: str) -> dict:
    p = client.post("/merges/preview", json={"source_id": source, "target_id": target})
    assert p.status_code == 200, p.text
    c = client.post("/merges/commit", json={"token": p.json()["token"], "resolutions": []})
    assert c.status_code == 200, c.text
    return c.json()


def test_merge_keeps_target_calendar(client):
    """归并保留目标档案日历；来源日历随档案删除；别名访问命中规范档案。"""
    src = make_exhibit(client, eid="src")
    tgt = make_exhibit(client, eid="tgt")
    put_calendar(client, src, {"timezone": "UTC", "weekly": [
        {"weekday": 1, "start": "09:00", "end": "17:00"},
    ]})
    put_calendar(client, tgt, {"timezone": "Asia/Shanghai", "weekly": [
        {"weekday": 2, "start": "10:00", "end": "12:00"},
    ]})
    merge(client, "src", "tgt")

    # 目标日历保持不变
    got = client.get("/exhibits/tgt/calendar")
    assert got.json()["timezone"] == "Asia/Shanghai"
    assert got.json()["weekly"] == [{"weekday": 2, "start": "10:00", "end": "12:00"}]

    # 别名访问命中规范档案的日历
    via_alias = client.get("/exhibits/src/calendar")
    assert via_alias.status_code == 200
    assert via_alias.json()["requested_id"] == "src"
    assert via_alias.json()["canonical_id"] == "tgt"
    assert via_alias.json()["timezone"] == "Asia/Shanghai"

    # 通过别名设置/删除日历也作用于规范档案
    put_calendar(client, "src", {"timezone": "UTC", "weekly": [
        {"weekday": 5, "start": "08:00", "end": "09:00"},
    ]})
    assert client.get("/exhibits/tgt/calendar").json()["weekly"] == [
        {"weekday": 5, "start": "08:00", "end": "09:00"},
    ]


def test_merge_target_without_calendar_stays_unconfigured(client):
    """目标无日历时归并不会带来来源日历：calendar=display 仍 409。"""
    src = make_exhibit(client, eid="src")
    tgt = make_exhibit(client, eid="tgt")
    put_calendar(client, src, {"timezone": "UTC", "weekly": [
        {"weekday": 1, "start": "09:00", "end": "17:00"},
    ]})
    merge(client, "src", "tgt")
    assert client.get("/exhibits/tgt/calendar").status_code == 404
    resp = client.get("/exhibits/src/dose",
                      params={"start": iso(MON), "end": iso(MON + HOUR), "calendar": "display"})
    assert resp.status_code == 409


def test_display_dose_via_alias_uses_canonical_calendar(client):
    """别名发起 calendar=display 查询时，按规范档案的日历裁剪。"""
    src = make_exhibit(client, eid="src")
    tgt = make_exhibit(client, eid="tgt")
    send(client, src, [{"id": f"r{i}", "ts": iso(MON + (9 + i) * HOUR), "lux": 100}
                       for i in range(4)])  # 周一 09:00–12:00 每小时
    put_calendar(client, tgt, {"timezone": "UTC", "weekly": [
        {"weekday": 1, "start": "10:00", "end": "11:00"},
    ]})
    merge(client, "src", "tgt")

    d = dose(client, "src", MON + 9 * HOUR, MON + 12 * HOUR, calendar="display")
    assert d["requested_id"] == "src"
    assert d["canonical_id"] == "tgt"
    # 读数已迁入 tgt；仅 10:00–11:00 开放被覆盖
    assert d["covered_seconds"] == pytest.approx(HOUR)
    assert d["closed_seconds"] == pytest.approx(2 * HOUR)
    assert d["known_dose_luxh"] == pytest.approx(100.0)
