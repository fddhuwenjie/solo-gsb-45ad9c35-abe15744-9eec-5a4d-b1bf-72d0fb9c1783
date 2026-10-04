"""RFID 馆藏盘点对账：窗口定位、四类差异、别名解析、摘要重放/409、并发固化、隔离性。"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

BASE = 1_700_000_000  # 2023-11-14T22:13:20Z


def iso(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, tz=timezone.utc).isoformat()


@pytest.fixture()
def env(tmp_path, monkeypatch):
    db_file = tmp_path / "test.db"
    monkeypatch.setenv("DB_PATH", str(db_file))
    from app.main import app

    with TestClient(app) as c:
        yield c, db_file


def make_exhibit(client: TestClient, eid: str, name: str | None = None) -> str:
    resp = client.post("/exhibits", json={
        "id": eid, "name": name or eid,
        "dose_limit_luxh": 1000.0, "max_interval_seconds": 3600.0,
    })
    assert resp.status_code == 201, resp.text
    return resp.json()["canonical_id"]


def send_readings(client: TestClient, exhibit_id: str, readings: list[dict]) -> None:
    resp = client.post(f"/exhibits/{exhibit_id}/readings", json={"readings": readings})
    assert resp.status_code == 200, resp.text


def reconcile(client: TestClient, round_id: str, body: dict,
              expected_status: int = 200) -> dict:
    resp = client.post(f"/inventory-rounds/{round_id}/reconcile", json=body)
    assert resp.status_code == expected_status, resp.text
    return resp.json()


def base_body(**overrides) -> dict:
    body = {
        "start_at": iso(BASE),
        "window_seconds": 60,
        "rssi_delta_threshold": 5,
        "expected": [],
        "events": [],
    }
    body.update(overrides)
    return body


def ev(exhibit_id: str, area: str, dt: float, rssi: float) -> dict:
    return {"exhibit_id": exhibit_id, "area": area, "ts": iso(dt), "rssi": rssi}


def ids(items: list[dict]) -> list[str]:
    return [x["canonical_id"] for x in items]


# ---------- 基本四类分类 ----------

def test_missing_misplaced_unexpected_and_correct(env):
    client, _ = env
    make_exhibit(client, "home")      # 在正确区域
    make_exhibit(client, "away")      # 错位
    make_exhibit(client, "ghost")     # 缺失
    make_exhibit(client, "intruder")  # 意外出现
    body = base_body(
        expected=[
            {"exhibit_id": "home", "area": "A"},
            {"exhibit_id": "away", "area": "A"},
            {"exhibit_id": "ghost", "area": "A"},
        ],
        events=[
            ev("home", "A", BASE + 5, -50),
            ev("away", "B", BASE + 5, -50),
            ev("intruder", "C", BASE + 5, -50),
        ],
    )
    out = reconcile(client, "r1", body)
    assert ids(out["missing"]) == ["ghost"]
    assert out["missing"][0]["expected_area"] == "A"
    assert ids(out["misplaced"]) == ["away"]
    assert out["misplaced"][0] == {
        "canonical_id": "away", "expected_area": "A", "actual_area": "B",
    }
    assert ids(out["unexpected"]) == ["intruder"]
    assert out["unexpected"][0]["actual_area"] == "C"
    assert out["unknown_position"] == []
    # 位置正确的 home 不出现在任何差异列表
    assert set(ids(out["missing"]) + ids(out["misplaced"]) +
               ids(out["unexpected"]) + ids(out["unknown_position"])) == {
        "ghost", "away", "intruder",
    }
    assert out["expected_count"] == 3
    assert out["observed_count"] == 3
    assert out["event_count"] == 3
    assert out["events_used_count"] == 3
    assert out["events_before_start_count"] == 0
    assert out["already_committed"] is False


def test_results_stably_sorted_by_canonical_id(env):
    client, _ = env
    for eid in ("m3", "m1", "m2", "u9", "u7"):
        make_exhibit(client, eid)
    body = base_body(
        expected=[{"exhibit_id": e, "area": "A"} for e in ("m3", "m1", "m2")],
        events=[
            ev("m1", "B", BASE + 1, -50),
            ev("m2", "B", BASE + 1, -50),
            ev("m3", "B", BASE + 1, -50),
            ev("u9", "B", BASE + 1, -50),
            ev("u7", "B", BASE + 1, -50),
        ],
    )
    out = reconcile(client, "r1", body)
    assert ids(out["misplaced"]) == ["m1", "m2", "m3"]
    assert ids(out["unexpected"]) == ["u7", "u9"]


# ---------- 窗口语义 ----------

def test_per_window_per_area_keeps_highest_rssi_and_last_window_wins(env):
    """窗口内每区域只保留最高 RSSI；以最后一个有事件的窗口判定位置。"""
    client, _ = env
    make_exhibit(client, "t")
    body = base_body(
        expected=[{"exhibit_id": "t", "area": "A"}],
        events=[
            # 窗口 0：B 区有更强信号，但 A 区较弱信号也存在（最后窗口才生效，不影响）
            ev("t", "A", BASE + 1, -40),
            ev("t", "B", BASE + 2, -55),
            ev("t", "A", BASE + 3, -60),   # 同窗 A 区较弱读数被最高值覆盖
            # 窗口 1（最后窗口）：只在 A 区出现 → 定位 A（尽管窗口 0 的 B 更强）
            ev("t", "A", BASE + 65, -70),
        ],
    )
    out = reconcile(client, "r1", body)
    assert out["misplaced"] == []
    assert out["missing"] == []
    assert out["unknown_position"] == []


def test_boundary_event_belongs_to_higher_window(env):
    """恰好落在窗口边界的事件归入更高窗口（floor 修正）。"""
    client, _ = env
    make_exhibit(client, "t")
    # BASE+60 恰好是窗口 1 的起点；若误归窗口 0，则最后窗口 1 无事件 → 定位 B
    body = base_body(
        expected=[{"exhibit_id": "t", "area": "A"}],
        events=[
            ev("t", "A", BASE, -50),
            ev("t", "B", BASE + 60, -50),
        ],
    )
    out = reconcile(client, "r1", body)
    assert ids(out["misplaced"]) == ["t"]
    assert out["misplaced"][0]["actual_area"] == "B"


def test_event_just_below_boundary_stays_in_lower_window(env):
    """边界下方（亚毫秒）的事件仍归低窗，不被浮点修正错误提窗。"""
    client, _ = env
    make_exhibit(client, "t")
    body = base_body(
        expected=[{"exhibit_id": "t", "area": "A"}],
        events=[
            ev("t", "A", BASE, -50),
            ev("t", "B", BASE + 59.999999, -50),  # 窗口 0 内，距边界 1µs
        ],
    )
    out = reconcile(client, "r1", body)
    # 最后窗口仍是窗口 0：A、B 同现且 RSSI 相同 → 位置不明（而非误判 B）
    assert ids(out["unknown_position"]) == ["t"]
    assert out["misplaced"] == []


def test_decimal_window_exact_boundary_event_enters_high_window(env):
    """小数窗口（0.1s）：起点后恰好 0.3s 的事件必须进第 3 窗。

    回归浮点 epoch 相减导致的错分：start=2026-09-21T08:00:00Z 时
    (ts.timestamp()-start.timestamp())/0.1 会把 0.3s 事件算成 2.9999…，
    误落第 2 窗并与 0.2s 事件合并，吞掉最后窗口（B 区）本应触发的错位。
    """
    client, _ = env
    make_exhibit(client, "T")
    body = {
        "start_at": "2026-09-21T08:00:00Z",
        "window_seconds": 0.1,
        "rssi_delta_threshold": 5,
        "expected": [{"exhibit_id": "T", "area": "A"}],
        "events": [
            {"exhibit_id": "T", "area": "A", "ts": "2026-09-21T08:00:00.2Z", "rssi": -50},
            # 恰好落在第 3 窗起点：必须与 0.2s 事件分窗，最后窗口只见 B → 错位
            {"exhibit_id": "T", "area": "B", "ts": "2026-09-21T08:00:00.3Z", "rssi": -50},
        ],
    }
    out = reconcile(client, "r1", body)
    assert out["misplaced"] == [
        {"canonical_id": "T", "expected_area": "A", "actual_area": "B"},
    ]
    assert out["unknown_position"] == []
    assert out["events_used_count"] == 2
    assert out["events_before_start_count"] == 0

    # 同一请求重放仍回放首次（精确分窗）结果
    replay = reconcile(client, "r1", body)
    assert replay["already_committed"] is True
    assert replay["misplaced"] == out["misplaced"]


def test_decimal_window_event_one_microsecond_below_boundary_stays_low(env):
    """小数窗口：边界下方 1µs 的真实偏移必须留在低窗，不被任何容差吸附。"""
    client, _ = env
    make_exhibit(client, "T")
    body = {
        "start_at": "2026-09-21T08:00:00Z",
        "window_seconds": 0.1,
        "rssi_delta_threshold": 5,
        "expected": [{"exhibit_id": "T", "area": "A"}],
        "events": [
            {"exhibit_id": "T", "area": "A", "ts": "2026-09-21T08:00:00.2Z", "rssi": -50},
            # 距第 3 窗起点仅 1µs：低窗内 A/B 同 RSSI → 位置不明，
            # 而非把真实偏移吞掉、提窗成“错位 B”
            {"exhibit_id": "T", "area": "B", "ts": "2026-09-21T08:00:00.299999Z",
             "rssi": -50},
        ],
    }
    out = reconcile(client, "r1", body)
    assert ids(out["unknown_position"]) == ["T"]
    assert out["misplaced"] == []


def test_events_before_start_ignored_and_counted(env):
    client, _ = env
    make_exhibit(client, "t")
    body = base_body(
        expected=[{"exhibit_id": "t", "area": "A"}],
        events=[
            ev("t", "B", BASE - 1, -50),
            ev("t", "B", BASE - 0.001, -50),
        ],
    )
    out = reconcile(client, "r1", body)
    assert ids(out["missing"]) == ["t"]
    assert out["observed_count"] == 0
    assert out["events_used_count"] == 0
    assert out["events_before_start_count"] == 2
    assert out["event_count"] == 2


def test_event_exactly_at_start_is_used(env):
    client, _ = env
    make_exhibit(client, "t")
    body = base_body(
        expected=[{"exhibit_id": "t", "area": "A"}],
        events=[ev("t", "A", BASE, -50)],
    )
    out = reconcile(client, "r1", body)
    assert out["missing"] == []
    assert out["events_used_count"] == 1
    assert out["events_before_start_count"] == 0


# ---------- 信号差阈值与位置不明 ----------

def test_unknown_position_when_delta_below_threshold(env):
    client, _ = env
    make_exhibit(client, "t")
    body = base_body(
        rssi_delta_threshold=5,
        expected=[{"exhibit_id": "t", "area": "A"}],
        events=[
            ev("t", "A", BASE + 1, -50),
            ev("t", "B", BASE + 2, -54),  # 差 4 < 5 → 位置不明
        ],
    )
    out = reconcile(client, "r1", body)
    assert out["unknown_position"] == [
        {"canonical_id": "t", "expected_area": "A"},
    ]
    # 位置不明不再同时记为错位
    assert out["misplaced"] == []


def test_delta_equal_threshold_is_locatable(env):
    """差值恰好等于阈值仍可定位（严格小于才算不明）。"""
    client, _ = env
    make_exhibit(client, "t")
    body = base_body(
        rssi_delta_threshold=5,
        expected=[{"exhibit_id": "t", "area": "A"}],
        events=[
            ev("t", "A", BASE + 1, -50),
            ev("t", "B", BASE + 2, -55),  # 差恰好 5 → 定位 A
        ],
    )
    out = reconcile(client, "r1", body)
    assert out["unknown_position"] == []
    assert out["misplaced"] == []


def test_threshold_zero_equal_rssi_tie_breaks_by_area_name(env):
    """阈值为 0 且双区域 RSSI 相同时：按区域标识字典序决胜，结果确定。"""
    client, _ = env
    make_exhibit(client, "t")
    body = base_body(
        rssi_delta_threshold=0,
        expected=[{"exhibit_id": "t", "area": "A"}],
        events=[
            ev("t", "B", BASE + 1, -60),
            ev("t", "A", BASE + 2, -60),  # 同 RSSI：A < B，定位 A
        ],
    )
    out = reconcile(client, "r1", body)
    assert out["misplaced"] == [] and out["unknown_position"] == []

    body["events"].reverse()  # 事件顺序不影响结果
    out2 = reconcile(client, "r2", body)
    assert out2["missing"] == [] and out2["unknown_position"] == []


def test_unexpected_tag_can_also_be_unknown(env):
    """不在预期清单中但被读到、信号歧义的标签记为位置不明（expected_area=null）。"""
    client, _ = env
    make_exhibit(client, "mystery")
    body = base_body(events=[
        ev("mystery", "A", BASE + 1, -50),
        ev("mystery", "B", BASE + 2, -51),
    ])
    out = reconcile(client, "r1", body)
    assert out["unexpected"] == []
    assert out["unknown_position"] == [
        {"canonical_id": "mystery", "expected_area": None},
    ]


def test_unknown_takes_precedence_over_misplacement(env):
    """最后窗口信号歧义时优先记位置不明，即使最强信号落在错误区域。"""
    client, _ = env
    make_exhibit(client, "t")
    body = base_body(
        rssi_delta_threshold=10,
        expected=[{"exhibit_id": "t", "area": "A"}],
        events=[
            ev("t", "B", BASE + 1, -50),  # 最强在 B（错位方向）
            ev("t", "A", BASE + 2, -55),  # 但差仅 5 < 10 → 不明
        ],
    )
    out = reconcile(client, "r1", body)
    assert ids(out["unknown_position"]) == ["t"]
    assert out["misplaced"] == []


# ---------- 别名解析 ----------

def test_aliases_resolve_to_canonical_in_expected_and_events(env):
    client, _ = env
    make_exhibit(client, "src")
    make_exhibit(client, "tgt")
    send_readings(client, "src", [{"id": "r1", "ts": iso(BASE), "lux": 1}])
    token = client.post("/merges/preview",
                        json={"source_id": "src", "target_id": "tgt"}).json()["token"]
    assert client.post("/merges/commit",
                       json={"token": token, "resolutions": []}).status_code == 200

    # 预期用别名 src、事件也用别名 src；解析后为同一件展品、定位正确
    body = base_body(
        expected=[{"exhibit_id": "src", "area": "A"}],
        events=[ev("src", "A", BASE + 5, -50)],
    )
    out = reconcile(client, "r1", body)
    assert out["expectations"] == [
        {"requested_id": "src", "canonical_id": "tgt", "expected_area": "A"},
    ]
    assert out["missing"] == out["misplaced"] == []

    # 链式别名（再归并一次）同样解析
    make_exhibit(client, "final")
    token2 = client.post("/merges/preview",
                         json={"source_id": "tgt", "target_id": "final"}).json()["token"]
    assert client.post("/merges/commit",
                       json={"token": token2, "resolutions": []}).status_code == 200
    out = reconcile(client, "r2", body)
    assert out["expectations"][0]["canonical_id"] == "final"


def test_duplicate_expected_after_alias_resolution_is_422(env):
    client, _ = env
    make_exhibit(client, "src")
    make_exhibit(client, "tgt")
    send_readings(client, "src", [{"id": "r1", "ts": iso(BASE), "lux": 1}])
    token = client.post("/merges/preview",
                        json={"source_id": "src", "target_id": "tgt"}).json()["token"]
    client.post("/merges/commit", json={"token": token, "resolutions": []})

    # src 与 tgt 解析后是同一规范档案：重复配置 → 422
    body = base_body(expected=[
        {"exhibit_id": "src", "area": "A"},
        {"exhibit_id": "tgt", "area": "B"},
    ])
    resp = client.post("/inventory-rounds/r1/reconcile", json=body)
    assert resp.status_code == 422
    assert "重复配置" in resp.json()["detail"]
    # 失败不留轮次
    assert client.get("/inventory-rounds/r1").status_code == 404


def test_duplicate_expected_same_id_is_422(env):
    client, _ = env
    make_exhibit(client, "t")
    body = base_body(expected=[
        {"exhibit_id": "t", "area": "A"},
        {"exhibit_id": "t", "area": "B"},
    ])
    assert client.post("/inventory-rounds/r1/reconcile", json=body).status_code == 422


def test_unknown_exhibit_404(env):
    client, _ = env
    body = base_body(expected=[{"exhibit_id": "ghost", "area": "A"}])
    assert client.post("/inventory-rounds/r1/reconcile", json=body).status_code == 404

    body = base_body(events=[ev("ghost", "A", BASE + 1, -50)])
    assert client.post("/inventory-rounds/r2/reconcile", json=body).status_code == 404


# ---------- 请求摘要：重放与 409 ----------

def test_replay_same_request_returns_original_result(env):
    client, _ = env
    make_exhibit(client, "t")
    body = base_body(
        expected=[{"exhibit_id": "t", "area": "A"}],
        events=[ev("t", "A", BASE + 5, -50)],
    )
    first = reconcile(client, "r1", body)
    assert first["already_committed"] is False

    second = reconcile(client, "r1", body)
    assert second["already_committed"] is True
    # 回放首次结果（digest、reconciled_at、各列表完全一致）
    assert second["request_digest"] == first["request_digest"]
    assert second["reconciled_at"] == first["reconciled_at"]
    assert second["missing"] == first["missing"]
    assert second["unexpected"] == first["unexpected"]

    # GET 取回同一份（无 already_committed 字段）
    got = client.get("/inventory-rounds/r1")
    assert got.status_code == 200
    assert got.json()["request_digest"] == first["request_digest"]
    assert "already_committed" not in got.json()


def test_replay_equivalent_requests_same_digest(env):
    """规范化后相同的请求：事件乱序、重复事件、不同时区写法、同值浮点写法 → 同摘要同结果。"""
    client, _ = env
    make_exhibit(client, "t")
    body = base_body(
        expected=[{"exhibit_id": "t", "area": "A"}],
        events=[
            ev("t", "A", BASE + 10, -50.0),
            ev("t", "A", BASE + 5, -50),
        ],
    )
    first = reconcile(client, "r1", body)

    body2 = base_body(
        expected=[{"exhibit_id": "t", "area": "A"}],
        events=[
            # 顺序颠倒、重复一条、时区偏移写法、整数/小数等价
            {"exhibit_id": "t", "area": "A",
             "ts": datetime.fromtimestamp(BASE + 10, tz=timezone.utc).isoformat(),
             "rssi": -50},
            {"exhibit_id": "t", "area": "A",
             "ts": datetime.fromtimestamp(BASE + 5, tz=timezone.utc).isoformat(),
             "rssi": -50.0},
            {"exhibit_id": "t", "area": "A", "ts": iso(BASE + 5), "rssi": -50.0},
        ],
    )
    second = reconcile(client, "r1", body2)
    assert second["already_committed"] is True
    assert second["request_digest"] == first["request_digest"]

    # 不同别名写法解析到同一规范档案，摘要也相同
    make_exhibit(client, "src")
    make_exhibit(client, "tgt")
    send_readings(client, "src", [{"id": "r1", "ts": iso(BASE), "lux": 1}])
    token = client.post("/merges/preview",
                        json={"source_id": "src", "target_id": "tgt"}).json()["token"]
    client.post("/merges/commit", json={"token": token, "resolutions": []})
    body3 = base_body(
        expected=[{"exhibit_id": "tgt", "area": "A"}],
        events=[ev("src", "A", BASE + 10, -50)],
    )
    third = reconcile(client, "round-x", body3)
    body4 = base_body(
        expected=[{"exhibit_id": "src", "area": "A"}],
        events=[ev("tgt", "A", BASE + 10, -50)],
    )
    fourth = reconcile(client, "round-x", body4)
    assert fourth["already_committed"] is True
    assert fourth["request_digest"] == third["request_digest"]


def test_different_request_same_round_409(env):
    client, _ = env
    make_exhibit(client, "t")
    body = base_body(
        expected=[{"exhibit_id": "t", "area": "A"}],
        events=[ev("t", "A", BASE + 5, -50)],
    )
    reconcile(client, "r1", body)

    # 改阈值
    conflict = dict(body, rssi_delta_threshold=9)
    r = client.post("/inventory-rounds/r1/reconcile", json=conflict)
    assert r.status_code == 409
    # 改窗口秒数
    assert client.post("/inventory-rounds/r1/reconcile",
                       json=dict(body, window_seconds=30)).status_code == 409
    # 改开始时刻
    assert client.post("/inventory-rounds/r1/reconcile",
                       json=dict(body, start_at=iso(BASE + 1))).status_code == 409
    # 改事件 RSSI
    body_changed = dict(body)
    body_changed["events"] = [ev("t", "A", BASE + 5, -60)]
    assert client.post("/inventory-rounds/r1/reconcile",
                       json=body_changed).status_code == 409
    # 改预期区域
    body_exp = dict(body, expected=[{"exhibit_id": "t", "area": "Z"}])
    assert client.post("/inventory-rounds/r1/reconcile",
                       json=body_exp).status_code == 409
    # 原结果未被覆盖
    got = client.get("/inventory-rounds/r1").json()
    assert got["rssi_delta_threshold"] == 5
    assert got["window_seconds"] == 60
    assert got["start_at"] == iso(BASE).replace("+00:00", "Z")


def test_get_unknown_round_404(env):
    client, _ = env
    assert client.get("/inventory-rounds/nope").status_code == 404


# ---------- 并发：只固化一份 ----------

def test_concurrent_same_request_only_persists_once(env):
    client, _ = env
    make_exhibit(client, "t")
    body = base_body(
        expected=[{"exhibit_id": "t", "area": "A"}],
        events=[ev("t", "A", BASE + 5, -50)],
    )

    def post(_: int) -> tuple[int, dict]:
        r = client.post("/inventory-rounds/r1/reconcile", json=body)
        return r.status_code, r.json()

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(post, range(12)))
    statuses = [s for s, _ in results]
    assert set(statuses) == {200}
    committed = [d for s, d in results if s == 200 and not d["already_committed"]]
    replayed = [d for s, d in results if s == 200 and d["already_committed"]]
    assert len(committed) == 1
    assert len(replayed) == 11
    digests = {d["request_digest"] for _, d in results}
    assert digests == {committed[0]["request_digest"]}
    reconciled_ats = {d["reconciled_at"] for _, d in results}
    assert reconciled_ats == {committed[0]["reconciled_at"]}


def test_concurrent_different_requests_one_winner_rest_409(env):
    client, _ = env
    make_exhibit(client, "t")

    def post(area: str) -> int:
        body = base_body(
            expected=[{"exhibit_id": "t", "area": area}],
            events=[ev("t", area, BASE + 5, -50)],
        )
        return client.post("/inventory-rounds/r1/reconcile", json=body).status_code

    with ThreadPoolExecutor(max_workers=8) as pool:
        statuses = list(pool.map(post, [f"A{i}" for i in range(8)]))
    # 恰好一个 200 首次固化，其余全部 409
    assert statuses.count(200) == 1
    assert statuses.count(409) == 7
    # 固化结果唯一且后续重放稳定
    got = client.get("/inventory-rounds/r1").json()
    assert got["expected_count"] == 1


# ---------- 参数校验 ----------

def test_request_validation_422(env):
    client, _ = env
    make_exhibit(client, "t")
    assert client.post("/inventory-rounds/r1/reconcile",
                       json=base_body(window_seconds=0)).status_code == 422
    assert client.post("/inventory-rounds/r1/reconcile",
                       json=base_body(window_seconds=-1)).status_code == 422
    assert client.post("/inventory-rounds/r1/reconcile",
                       json=base_body(rssi_delta_threshold=-0.1)).status_code == 422
    bad_area = base_body(expected=[{"exhibit_id": "t", "area": ""}])
    assert client.post("/inventory-rounds/r1/reconcile", json=bad_area).status_code == 422
    bad_ts = base_body(events=[{"exhibit_id": "t", "area": "A", "ts": "not-a-time",
                                "rssi": -50}])
    assert client.post("/inventory-rounds/r1/reconcile", json=bad_ts).status_code == 422


def test_empty_expected_and_events_round_is_all_empty(env):
    client, _ = env
    out = reconcile(client, "r1", base_body())
    assert out["expected_count"] == 0
    assert out["observed_count"] == 0
    assert out["missing"] == [] and out["misplaced"] == []
    assert out["unexpected"] == [] and out["unknown_position"] == []
    assert client.get("/inventory-rounds/r1").status_code == 200


def test_naive_start_time_treated_as_utc(env):
    client, _ = env
    make_exhibit(client, "t")
    body = base_body(
        start_at=datetime.fromtimestamp(BASE, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%S"),
        expected=[{"exhibit_id": "t", "area": "A"}],
        events=[{"exhibit_id": "t", "area": "A",
                 "ts": "2023-11-14T22:13:25", "rssi": -50}],
    )
    out = reconcile(client, "r1", body)
    assert out["start_at"] == "2023-11-14T22:13:20Z"
    assert out["events_used_count"] == 1
    assert out["missing"] == []


# ---------- 隔离性：不改读数、不碰收据链 ----------

def test_reconcile_does_not_modify_readings_or_receipts(env):
    client, db_file = env
    make_exhibit(client, "a")
    make_exhibit(client, "b")
    send_readings(client, "a", [
        {"id": "r1", "ts": iso(BASE), "lux": 100},
        {"id": "r2", "ts": iso(BASE + 3600), "lux": 100},
    ])
    chain_before = client.get("/exhibits/a/receipts").json()
    readings_before = client.get("/exhibits/a/readings").json()["readings"]

    body = base_body(
        expected=[
            {"exhibit_id": "a", "area": "A"},
            {"exhibit_id": "b", "area": "A"},
        ],
        events=[
            ev("a", "B", BASE + 5, -50),
            ev("a", "A", BASE + 5, -90),
        ],
    )
    reconcile(client, "r1", body)
    reconcile(client, "r1", body)  # 重放也不应产生副作用

    # 读数与收据链原样
    readings_after = client.get("/exhibits/a/readings").json()["readings"]
    chain_after = client.get("/exhibits/a/receipts").json()
    assert readings_after == readings_before
    assert chain_after["receipts"] == chain_before["receipts"]
    verify = client.get("/exhibits/a/receipts/verify").json()
    assert verify["ok"] is True and verify["total"] == 1

    # 库存表之外没有新增读数：b 仍无读数
    assert client.get("/exhibits/b/readings").json()["readings"] == []

    import sqlite3
    conn = sqlite3.connect(str(db_file))
    try:
        tables = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
        assert "inventory_rounds" in tables
        assert conn.execute("SELECT COUNT(*) FROM inventory_rounds").fetchone()[0] == 1
    finally:
        conn.close()
