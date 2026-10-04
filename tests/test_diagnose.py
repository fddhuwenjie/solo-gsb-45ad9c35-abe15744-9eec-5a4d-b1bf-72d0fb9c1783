"""照度采集质量诊断 API 测试：合并、时间冲突、gap、jump、stuck、窗口不变量、别名、只读。"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from app.diagnose import build_events, intersect_window, sort_events

BASE = 1_700_000_000  # 2023-11-14T22:13:20Z


def iso(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, tz=timezone.utc).isoformat()


def at(value: str) -> float:
    """解析 API 返回的 ISO 8601 时刻（Z 后缀）为 Unix 秒。"""
    return datetime.fromisoformat(value).timestamp()


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("DB_PATH", str(tmp_path / "test.db"))
    from app.main import app

    with TestClient(app) as c:
        yield c


def make_exhibit(client: TestClient, interval: float = 3600.0,
                 eid: str | None = None) -> str:
    body = {"name": "唐代绢画", "dose_limit_luxh": 1000.0,
            "max_interval_seconds": interval}
    if eid is not None:
        body["id"] = eid
    resp = client.post("/exhibits", json=body)
    assert resp.status_code == 201, resp.text
    return resp.json()["canonical_id"]


def send(client: TestClient, exhibit_id: str, readings: list[dict]) -> None:
    resp = client.post(f"/exhibits/{exhibit_id}/readings", json={"readings": readings})
    assert resp.status_code == 200, resp.text


def diagnose(client: TestClient, exhibit_id: str, start: float, end: float,
             *, rate: float = 1.0, tol: float = 2.0, min_dur: float = 1800.0) -> dict:
    resp = client.post(
        f"/exhibits/{exhibit_id}/readings/diagnose",
        json={
            "start": iso(start), "end": iso(end),
            "max_rate_lux_per_second": rate,
            "stuck_tolerance_lux": tol,
            "min_stuck_seconds": min_dur,
        },
    )
    assert resp.status_code == 200, resp.text
    return resp.json()


def types_of(events: list[dict]) -> list[str]:
    return [e["type"] for e in events]


def find(events: list[dict], etype: str) -> dict:
    hits = [e for e in events if e["type"] == etype]
    assert len(hits) == 1, f"expected exactly one {etype}: {events}"
    return hits[0]


# ---------- stuck：同时刻同值合并 + 最大事件 ----------

def test_same_timestamp_same_lux_merged_into_maximal_stuck(client):
    eid = make_exhibit(client)
    # t0 有三条重复读数（同值）；其余每小时一条、照度差 ≤ 2，持续 3h
    send(client, eid, [
        {"id": "a1", "ts": iso(BASE), "lux": 50},
        {"id": "a2", "ts": iso(BASE), "lux": 50},
        {"id": "a3", "ts": iso(BASE), "lux": 50},
        {"id": "b", "ts": iso(BASE + 3600), "lux": 51},
        {"id": "c", "ts": iso(BASE + 7200), "lux": 49},
        {"id": "d", "ts": iso(BASE + 10800), "lux": 50},
    ])
    out = diagnose(client, eid, BASE, BASE + 10800)
    assert types_of(out["events"]) == ["stuck"]
    ev = out["events"][0]
    # 合并后不产生 timestamp_conflict；同一最大事件覆盖全部四个时刻
    assert at(ev["start"]) == BASE
    assert at(ev["end"]) == BASE + 10800
    assert ev["duration_seconds"] == pytest.approx(10800.0)
    assert ev["lux_span"] == pytest.approx(2.0)
    assert ev["reading_ids"] == ["a1", "a2", "a3", "b", "c", "d"]
    # 非 stuck 指标不出现（exclude_unset）
    assert "interval_seconds" not in ev and "rate_lux_per_second" not in ev


def test_stuck_below_minimum_duration_not_emitted(client):
    eid = make_exhibit(client)
    send(client, eid, [
        {"id": "a", "ts": iso(BASE), "lux": 10},
        {"id": "b", "ts": iso(BASE + 600), "lux": 10},
        {"id": "c", "ts": iso(BASE + 1200), "lux": 10},
    ])
    # 平稳段只有 1200s，未达 1800s 门槛
    out = diagnose(client, eid, BASE, BASE + 1200, min_dur=1800.0)
    assert out["events"] == []
    # 恰好达到门槛（含等号）即产生事件
    out2 = diagnose(client, eid, BASE, BASE + 1200, min_dur=1200.0)
    assert types_of(out2["events"]) == ["stuck"]
    assert out2["events"][0]["duration_seconds"] == pytest.approx(1200.0)


# ---------- timestamp_conflict ----------

def test_timestamp_conflict_truncates_chain(client):
    eid = make_exhibit(client)
    # 前侧 t0→t+3600 平稳 1h（达默认 1800s 门槛）；t+7200 时刻两个读数照度不同 → 冲突
    # 冲突之后 t+7800、t+8400 另起平稳段（仅 600s，不达 1800s）
    send(client, eid, [
        {"id": "a", "ts": iso(BASE), "lux": 30},
        {"id": "b", "ts": iso(BASE + 3600), "lux": 31},
        {"id": "x", "ts": iso(BASE + 7200), "lux": 30},
        {"id": "y", "ts": iso(BASE + 7200), "lux": 80},
        {"id": "c", "ts": iso(BASE + 7800), "lux": 30},
        {"id": "d", "ts": iso(BASE + 8400), "lux": 30},
    ])
    out = diagnose(client, eid, BASE, BASE + 10000)
    by_type = {e["type"]: e for e in out["events"]}
    assert set(by_type) == {"stuck", "timestamp_conflict"}
    conflict = by_type["timestamp_conflict"]
    assert at(conflict["start"]) == at(conflict["end"]) == BASE + 7200
    assert conflict["reading_ids"] == ["x", "y"]
    assert conflict["lux_span"] == pytest.approx(50.0)
    assert "duration_seconds" not in conflict

    stuck = by_type["stuck"]
    # 冲突截断：前侧 stuck 只到 b，绝不跨越冲突时刻
    assert at(stuck["start"]) == BASE
    assert at(stuck["end"]) == BASE + 3600
    assert stuck["reading_ids"] == ["a", "b"]
    # 后链仅 600s 不达标，无第二个 stuck


# ---------- gap：截断 + 完整时间线 + 窗口不变量 ----------

def test_gap_truncates_stuck_and_full_timeline_boundaries_survive_window(client):
    eid = make_exhibit(client, interval=3600.0)
    send(client, eid, [
        {"id": "a", "ts": iso(BASE), "lux": 10},
        {"id": "b", "ts": iso(BASE + 3600), "lux": 10},
        {"id": "c", "ts": iso(BASE + 10800), "lux": 10},   # 与 b 间隔 7200 > 3600 → gap
        {"id": "d", "ts": iso(BASE + 14400), "lux": 10},
        {"id": "e", "ts": iso(BASE + 18000), "lux": 10},
    ])
    # 宽窗口：两个 stuck（各 3600s 达标）被中间 gap 截断
    wide = diagnose(client, eid, BASE, BASE + 18000, min_dur=3600.0)
    assert types_of(wide["events"]) == ["stuck", "gap", "stuck"]
    gap = wide["events"][1]
    assert at(gap["start"]) == BASE + 3600
    assert at(gap["end"]) == BASE + 10800
    assert gap["interval_seconds"] == pytest.approx(7200.0)
    assert gap["reading_ids"] == ["b", "c"]
    assert "rate_lux_per_second" not in gap
    assert wide["events"][0]["reading_ids"] == ["a", "b"]
    assert wide["events"][2]["reading_ids"] == ["c", "d", "e"]

    # 窄窗口只与后段 stuck 相交：边界、读数 ID 仍是完整时间线上的原始值
    narrow = diagnose(client, eid, BASE + 12000, BASE + 13000, min_dur=3600.0)
    assert types_of(narrow["events"]) == ["stuck"]
    st = narrow["events"][0]
    assert at(st["start"]) == BASE + 10800
    assert at(st["end"]) == BASE + 18000
    assert st["reading_ids"] == ["c", "d", "e"]

    # 窗口与 gap 仅在端点相接（闭区间相交）：gap 仍返回，边界不变
    touching = diagnose(client, eid, BASE, BASE + 3600, min_dur=3600.0)
    assert "gap" in types_of(touching["events"])
    g = find(touching["events"], "gap")
    assert g["interval_seconds"] == pytest.approx(7200.0)


def test_interval_equal_to_max_is_not_gap(client):
    eid = make_exhibit(client, interval=3600.0)
    send(client, eid, [
        {"id": "a", "ts": iso(BASE), "lux": 10},
        {"id": "b", "ts": iso(BASE + 3600), "lux": 10},  # 恰好等于上限：合规
    ])
    out = diagnose(client, eid, BASE, BASE + 3600, min_dur=3600.0)
    assert types_of(out["events"]) == ["stuck"]


# ---------- jump：速率超限 ----------

def test_jump_rate_exceeded_truncates_stuck(client):
    eid = make_exhibit(client, interval=3600.0)
    # 速率上限 1 lux/s：b→c 间隔 600s、Δlux=1000 → 1.667 lux/s → jump
    send(client, eid, [
        {"id": "a", "ts": iso(BASE), "lux": 10},
        {"id": "b", "ts": iso(BASE + 600), "lux": 11},
        {"id": "c", "ts": iso(BASE + 1200), "lux": 1011},
        {"id": "d", "ts": iso(BASE + 1800), "lux": 1012},
        {"id": "e", "ts": iso(BASE + 2400), "lux": 1011},
    ])
    out = diagnose(client, eid, BASE, BASE + 2400, rate=1.0, tol=2.0, min_dur=600.0)
    assert types_of(out["events"]) == ["stuck", "jump", "stuck"]
    jump = find(out["events"], "jump")
    assert at(jump["start"]) == BASE + 600
    assert at(jump["end"]) == BASE + 1200
    assert jump["interval_seconds"] == pytest.approx(600.0)
    assert jump["rate_lux_per_second"] == pytest.approx(1000.0 / 600.0)
    assert jump["reading_ids"] == ["b", "c"]
    # jump 两侧各自成链，互不连通
    stucks = [e for e in out["events"] if e["type"] == "stuck"]
    assert [s["reading_ids"] for s in stucks] == [["a", "b"], ["c", "d", "e"]]
    assert stucks[1]["lux_span"] == pytest.approx(1.0)


def test_rate_equal_to_limit_is_not_jump(client):
    eid = make_exhibit(client, interval=3600.0)
    # 600s 变化 600 lux，速率恰好 1 lux/s（含等号不判 jump）
    send(client, eid, [
        {"id": "a", "ts": iso(BASE), "lux": 0},
        {"id": "b", "ts": iso(BASE + 600), "lux": 600},
    ])
    out = diagnose(client, eid, BASE, BASE + 600, rate=1.0, tol=600.0, min_dur=600.0)
    assert types_of(out["events"]) == ["stuck"]


def test_oversize_gap_is_reported_as_gap_even_when_rate_would_exceed(client):
    """间隔超限时只记 gap，不重复记 jump。"""
    eid = make_exhibit(client, interval=3600.0)
    send(client, eid, [
        {"id": "a", "ts": iso(BASE), "lux": 0},
        {"id": "b", "ts": iso(BASE + 7200), "lux": 100000},
    ])
    out = diagnose(client, eid, BASE, BASE + 7200, rate=1.0, tol=2.0, min_dur=1.0)
    assert types_of(out["events"]) == ["gap"]


# ---------- 排序与筛选 ----------

def test_events_sorted_by_start_and_type_and_disjoint_window_excluded(client):
    eid = make_exhibit(client, interval=600.0)
    # t0→t+600 平稳 600s 达标；t+1200 与前点间隔 600 合规但速率超限 → jump；
    # t+1800 照度恢复但 Δlux=4995 超容差 → 无 stuck 延续；t+2400 与 t+1800
    # 间隔 600 合规且照度同值，距窗口末不足 600s 不构成 stuck。
    send(client, eid, [
        {"id": "a", "ts": iso(BASE), "lux": 5},
        {"id": "b", "ts": iso(BASE + 600), "lux": 5},
        {"id": "c", "ts": iso(BASE + 1200), "lux": 5000},
        {"id": "d", "ts": iso(BASE + 1800), "lux": 5},
        {"id": "e", "ts": iso(BASE + 2400), "lux": 5},
    ])
    out = diagnose(client, eid, BASE, BASE + 3000,
                   rate=1.0, tol=2.0, min_dur=600.0)
    starts = [e["start"] for e in out["events"]]
    assert starts == sorted(starts)
    # b→c、c→d 速率都超限（8.32 lux/s）→ 两个 jump 截断；
    # 末段 d→e 同值、持续 600s 达标，是被 jump 隔出的第二个 stuck。
    assert types_of(out["events"]) == ["stuck", "jump", "jump", "stuck"]

    # 窗口在最后两个点（1800,2400）之后、且不与任何事件相交 → 空
    after = diagnose(client, eid, BASE + 2500, BASE + 2700,
                     rate=1.0, tol=2.0, min_dur=600.0)
    assert after["events"] == []
    # 窗口在首个事件之前同样为空
    before = diagnose(client, eid, BASE - 1000, BASE - 100,
                      rate=1.0, tol=2.0, min_dur=600.0)
    assert before["events"] == []


# ---------- 空时间线 / 不存在档案 / 校验 ----------

def test_empty_timeline_returns_no_events(client):
    eid = make_exhibit(client)
    out = diagnose(client, eid, BASE, BASE + 3600)
    assert out["events"] == []
    assert out["canonical_id"] == eid
    assert out["max_interval_seconds"] == 3600.0


def test_diagnose_unknown_exhibit_404(client):
    resp = client.post("/exhibits/nope/readings/diagnose", json={
        "start": iso(BASE), "end": iso(BASE + 1),
        "max_rate_lux_per_second": 1.0,
        "stuck_tolerance_lux": 2.0,
        "min_stuck_seconds": 60.0,
    })
    assert resp.status_code == 404


def test_diagnose_request_validation(client):
    eid = make_exhibit(client)
    base_body = {
        "start": iso(BASE), "end": iso(BASE + 1),
        "max_rate_lux_per_second": 1.0,
        "stuck_tolerance_lux": 2.0,
        "min_stuck_seconds": 60.0,
    }

    def post(changes: dict) -> int:
        body = {**base_body, **changes}
        return client.post(f"/exhibits/{eid}/readings/diagnose", json=body).status_code

    assert post({"end": iso(BASE)}) == 422          # end <= start
    assert post({"end": iso(BASE - 1)}) == 422
    assert post({"max_rate_lux_per_second": 0}) == 422
    assert post({"stuck_tolerance_lux": -0.1}) == 422
    assert post({"min_stuck_seconds": 0}) == 422
    # 容差允许恰好为 0
    assert post({"stuck_tolerance_lux": 0}) == 200


# ---------- 别名命中规范档案 + 只读 ----------

def test_diagnose_via_alias_is_readonly_and_dose_unchanged(client, monkeypatch):
    from app.main import app

    src = make_exhibit(client, interval=3600.0, eid="src")
    tgt = make_exhibit(client, interval=3600.0, eid="tgt")
    send(client, src, [
        {"id": "r1", "ts": iso(BASE), "lux": 10},
        {"id": "r2", "ts": iso(BASE + 3600), "lux": 10},
    ])
    # 归并 src → tgt
    p = client.post("/merges/preview", json={"source_id": "src", "target_id": "tgt"})
    assert p.status_code == 200, p.text
    token = p.json()["token"]
    assert client.post("/merges/commit",
                       json={"token": token, "resolutions": []}).status_code == 200

    # 别名访问命中规范档案
    out = diagnose(client, "src", BASE, BASE + 3600, tol=0.0, min_dur=3600.0)
    assert out["requested_id"] == "src"
    assert out["canonical_id"] == "tgt"
    assert types_of(out["events"]) == ["stuck"]
    assert out["events"][0]["reading_ids"] == ["r1", "r2"]

    # 诊断不写库：读数集合、剂量结果不变；重复诊断结果一致
    ids_before = [r["id"] for r in
                  client.get("/exhibits/tgt/readings").json()["readings"]]
    assert ids_before == ["r1", "r2"]
    dose = client.get("/exhibits/tgt/dose",
                      params={"start": iso(BASE), "end": iso(BASE + 3600)}).json()
    assert dose["known_dose_luxh"] == pytest.approx(10.0)
    again = diagnose(client, "src", BASE, BASE + 3600, tol=0.0, min_dur=3600.0)
    assert again["events"] == out["events"]


# ---------- 纯函数层：乱序输入与窗口筛选 ----------

def test_build_events_accepts_unsorted_input():
    readings = [
        ("c", BASE + 7200, 50.0),
        ("b", BASE + 3600, 49.0),
        ("a", BASE, 50.0),
    ]
    events = sort_events(build_events(
        readings, max_interval_s=3600.0, max_rate_lux_per_s=10.0,
        stuck_tol_lux=2.0, min_stuck_s=7200.0,
    ))
    assert [e.type for e in events] == ["stuck"]
    assert events[0].reading_ids == ["a", "b", "c"]
    assert events[0].lux_span == pytest.approx(1.0)


def test_intersect_window_keeps_original_boundaries():
    readings = [
        ("a", 0.0, 1.0), ("b", 100.0, 1.0), ("c", 200.0, 1.0),
    ]
    events = build_events(
        readings, max_interval_s=50.0, max_rate_lux_per_s=10.0,
        stuck_tol_lux=0.0, min_stuck_s=200.0,
    )
    # 两个 gap：(0,50)、(100,150) 与一个 stuck? 不——间隔全部 100 > 50，
    # 每段只有单点，stuck 不成立，只有两个 gap
    assert [e.type for e in sort_events(events)] == ["gap", "gap"]
    hits = intersect_window(events, 120.0, 140.0)
    assert len(hits) == 1
    assert (hits[0].start, hits[0].end) == (100.0, 200.0)
    assert hits[0].reading_ids == ["b", "c"]


def test_arbitration_breaks_reset_chains_silently():
    """外部仲裁断点：链在两侧重置，不产生任何事件（含 timestamp_conflict）。

    断点两侧权威点间隔超限（本应 gap）、照度差极大（本应 jump），
    且各自的平稳段长度都足以成为 stuck——断点只把它们隔成两段 stuck。
    """
    readings = [
        ("a", 0.0, 10.0), ("b", 600.0, 10.0),       # 前侧平稳段
        ("c", 5000.0, 9000.0), ("d", 5600.0, 9000.0),  # 后侧平稳段
    ]
    events = build_events(
        readings, max_interval_s=3600.0, max_rate_lux_per_s=1.0,
        stuck_tol_lux=0.0, min_stuck_s=600.0,
        breaks=[2000.0],
    )
    assert [e.type for e in sort_events(events)] == ["stuck", "stuck"]
    assert [e.reading_ids for e in events] == [["a", "b"], ["c", "d"]]
