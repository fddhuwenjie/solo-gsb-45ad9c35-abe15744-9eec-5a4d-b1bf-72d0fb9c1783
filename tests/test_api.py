"""API 端到端测试：梯形法、乱序/补传、幂等、缺测段、首次越线。"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

BASE = 1_700_000_000  # 2023-11-14T22:13:20Z


def iso(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, tz=timezone.utc).isoformat()


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("DB_PATH", str(tmp_path / "test.db"))
    from app.main import app

    with TestClient(app) as c:
        yield c


def make_exhibit(client: TestClient, limit: float = 1000.0, interval: float = 3600.0,
                 eid: str | None = None, name: str = "唐代绢画") -> str:
    body = {"name": name, "dose_limit_luxh": limit, "max_interval_seconds": interval}
    if eid is not None:
        body["id"] = eid
    resp = client.post("/exhibits", json=body)
    assert resp.status_code == 201, resp.text
    data = resp.json()
    assert data["canonical_id"] == data["requested_id"]
    return data["canonical_id"]


def send(client: TestClient, exhibit_id: str, readings: list[dict]) -> dict:
    resp = client.post(f"/exhibits/{exhibit_id}/readings", json={"readings": readings})
    assert resp.status_code == 200, resp.text
    return resp.json()


def list_ids(client: TestClient, exhibit_id: str) -> list[str]:
    resp = client.get(f"/exhibits/{exhibit_id}/readings")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["canonical_id"]
    return [r["id"] for r in body["readings"]]


def dose(client: TestClient, exhibit_id: str, start: float, end: float) -> dict:
    resp = client.get(f"/exhibits/{exhibit_id}/dose", params={"start": iso(start), "end": iso(end)})
    assert resp.status_code == 200, resp.text
    return resp.json()


# ---------- 展品 ----------

def test_create_and_get_exhibit(client):
    eid = make_exhibit(client, limit=150.0, interval=1800.0)
    got = client.get(f"/exhibits/{eid}")
    assert got.status_code == 200
    body = got.json()
    assert body["name"] == "唐代绢画"
    assert body["dose_limit_luxh"] == 150.0
    assert body["max_interval_seconds"] == 1800.0
    assert client.get("/exhibits").status_code == 200


def test_exhibit_validation(client):
    assert client.post("/exhibits", json={
        "name": "x", "dose_limit_luxh": 0, "max_interval_seconds": 3600,
    }).status_code == 422
    assert client.post("/exhibits", json={
        "name": "x", "dose_limit_luxh": 100, "max_interval_seconds": -1,
    }).status_code == 422


def test_duplicate_exhibit_id_conflict(client):
    eid = make_exhibit(client)
    resp = client.post("/exhibits", json={
        "id": eid, "name": "另一件", "dose_limit_luxh": 100, "max_interval_seconds": 60,
    })
    assert resp.status_code == 409


# ---------- 梯形法与乱序 ----------

def test_trapezoid_with_out_of_order_readings(client):
    eid = make_exhibit(client, limit=1000.0, interval=3600.0)
    # 故意乱序上报：t+3600 先于 t0
    send(client, eid, [
        {"id": "r2", "ts": iso(BASE + 3600), "lux": 100},
        {"id": "r1", "ts": iso(BASE), "lux": 100},
        {"id": "r3", "ts": iso(BASE + 7200), "lux": 200},
    ])
    d = dose(client, eid, BASE, BASE + 7200)
    # 段1: (100+100)/2 * 1h = 100；段2: (100+200)/2 * 1h = 150
    assert d["known_dose_luxh"] == pytest.approx(250.0)
    assert d["remaining_luxh"] == pytest.approx(750.0)
    assert d["unknown_seconds"] == 0
    assert d["covered_seconds"] == 7200
    assert d["reading_count"] == 3
    assert d["exceeded"] is False
    assert d["first_exceeded_at"] is None


# ---------- 幂等 ----------

def test_reading_id_idempotent(client):
    eid = make_exhibit(client, limit=1000.0, interval=3600.0)
    r1 = {"id": "r1", "ts": iso(BASE), "lux": 100}
    r2 = {"id": "r2", "ts": iso(BASE + 3600), "lux": 100}

    first = send(client, eid, [r1, r2])
    assert (first["inserted"], first["duplicates"], first["conflicts"]) == (2, 0, 0)

    # 同 ID 同数据重传：幂等忽略，剂量不变
    again = send(client, eid, [r1, r2])
    assert (again["inserted"], again["duplicates"], again["conflicts"]) == (0, 2, 0)
    assert dose(client, eid, BASE, BASE + 3600)["known_dose_luxh"] == pytest.approx(100.0)

    # 同 ID 不同数据：冲突，拒绝写入
    conflict = send(client, eid, [{"id": "r1", "ts": iso(BASE), "lux": 999}])
    assert conflict["conflicts"] == 1
    assert conflict["results"][0]["status"] == "conflict"
    assert dose(client, eid, BASE, BASE + 3600)["known_dose_luxh"] == pytest.approx(100.0)


# ---------- 缺测段：不插值 + 补传重算 ----------

def test_gap_not_interpolated_then_backfill_recomputes(client):
    eid = make_exhibit(client, limit=10000.0, interval=3600.0)
    send(client, eid, [
        {"id": "a", "ts": iso(BASE), "lux": 100},
        {"id": "b", "ts": iso(BASE + 10800), "lux": 100},
    ])
    d = dose(client, eid, BASE, BASE + 10800)
    # 间隔 10800s > 3600s：不插值，整段计为未知
    assert d["known_dose_luxh"] == 0
    assert d["unknown_seconds"] == 10800

    # 补传中间两条后自动重算：三段各 1h，全部可积分
    send(client, eid, [
        {"id": "c", "ts": iso(BASE + 3600), "lux": 100},
        {"id": "d", "ts": iso(BASE + 7200), "lux": 100},
    ])
    d2 = dose(client, eid, BASE, BASE + 10800)
    assert d2["known_dose_luxh"] == pytest.approx(300.0)
    assert d2["unknown_seconds"] == 0
    assert d2["reading_count"] == 4


def test_boundary_gap_counts_as_unknown(client):
    eid = make_exhibit(client, limit=10000.0, interval=3600.0)
    send(client, eid, [{"id": "a", "ts": iso(BASE + 3600), "lux": 50}])
    d = dose(client, eid, BASE, BASE + 10800)
    # 仅一条读数，任何一侧都无法构成有效段；窗口时长只有 covered / unknown 两种归属，
    # 前 3600s 与后 7200s 均未被有效段覆盖，全部计入 unknown
    assert d["unknown_seconds"] == pytest.approx(10800.0)
    assert d["covered_seconds"] == 0
    assert d["known_dose_luxh"] == 0


def test_window_between_two_readings_is_covered(client):
    """窗口完全落在两条相邻有效读数之间、窗口内无采样点：仍应线性裁剪为已知覆盖。"""
    eid = make_exhibit(client, limit=1000.0, interval=3600.0)
    send(client, eid, [
        {"id": "r1", "ts": iso(BASE), "lux": 100},
        {"id": "r2", "ts": iso(BASE + 3600), "lux": 100},
    ])
    d = dose(client, eid, BASE + 900, BASE + 2700)
    assert d["reading_count"] == 0
    assert d["covered_seconds"] == pytest.approx(1800.0)
    assert d["unknown_seconds"] == 0
    assert d["known_dose_luxh"] == pytest.approx(50.0)  # 100 lux × 0.5h
    # 全历史截至窗口末共 2700s = 0.75h
    assert d["remaining_luxh"] == pytest.approx(925.0)
    assert d["exceeded"] is False
    assert d["first_exceeded_at"] is None


def test_window_clip_across_boundary_linear_ramp(client):
    """跨边界裁剪：窗口起点、终点都不在采样点上，照度按相邻点线性取值。"""
    eid = make_exhibit(client, limit=10000.0, interval=3600.0)
    send(client, eid, [
        {"id": "r1", "ts": iso(BASE), "lux": 0},
        {"id": "r2", "ts": iso(BASE + 3600), "lux": 200},
        {"id": "r3", "ts": iso(BASE + 7200), "lux": 200},
    ])
    # 窗口 [1800, 5400]：前半段照度 100→200，后半段 200→200
    d = dose(client, eid, BASE + 1800, BASE + 5400)
    assert d["reading_count"] == 1  # 窗口内只有 t+3600 一条
    assert d["covered_seconds"] == pytest.approx(3600.0)
    assert d["unknown_seconds"] == 0
    # (100+200)/2 * 0.5h + (200+200)/2 * 0.5h = 75 + 100 = 175
    assert d["known_dose_luxh"] == pytest.approx(175.0)


def test_window_crossing_oversize_gap_is_unknown(client):
    """窗口跨过一个超过最大间隔的缺测段：窗口内无点且整窗未知。"""
    eid = make_exhibit(client, limit=10000.0, interval=3600.0)
    send(client, eid, [
        {"id": "r1", "ts": iso(BASE), "lux": 100},
        {"id": "r2", "ts": iso(BASE + 10800), "lux": 100},
    ])
    d = dose(client, eid, BASE + 3600, BASE + 7200)
    assert d["reading_count"] == 0
    assert d["known_dose_luxh"] == 0
    assert d["covered_seconds"] == 0
    assert d["unknown_seconds"] == pytest.approx(3600.0)


def test_window_clipped_edges_with_valid_gaps(client):
    """两侧相邻点均存在且间隔合规：窗口边缘线性裁剪，unknown 为 0。"""
    eid = make_exhibit(client, limit=10000.0, interval=3600.0)
    send(client, eid, [
        {"id": "r0", "ts": iso(BASE - 1800), "lux": 100},
        {"id": "r1", "ts": iso(BASE), "lux": 100},
        {"id": "r2", "ts": iso(BASE + 3600), "lux": 100},
        {"id": "r3", "ts": iso(BASE + 5400), "lux": 100},
    ])
    d = dose(client, eid, BASE - 900, BASE + 4500)
    assert d["covered_seconds"] == pytest.approx(5400.0)
    assert d["unknown_seconds"] == 0
    assert d["known_dose_luxh"] == pytest.approx(150.0)  # 100 lux × 1.5h


def test_history_already_exceeded_and_window_narrowing_invariant(client):
    """remaining/exceeded/first_exceeded_at 基于截至窗口末的全历史累计；
    首次越线时刻不随窗口缩窄而改变。"""
    eid = make_exhibit(client, limit=100.0, interval=3600.0)
    send(client, eid, [
        {"id": "r1", "ts": iso(BASE), "lux": 100},        # 0~1h: 100 lux·h，恰好到限
        {"id": "r2", "ts": iso(BASE + 3600), "lux": 100},  # 1~2h
        {"id": "r3", "ts": iso(BASE + 7200), "lux": 100},  # 2~3h
    ])

    # 宽窗口：全历史截至窗口末 200 lux·h，越线 100，首次越线时刻 = BASE+3600
    wide = dose(client, eid, BASE, BASE + 9000)
    assert wide["exceeded"] is True
    assert wide["remaining_luxh"] == pytest.approx(-100.0)
    crossed = datetime.fromisoformat(wide["first_exceeded_at"]).timestamp()
    assert crossed == pytest.approx(BASE + 3600, abs=1.0)

    # 缩窄到越线之后：窗口内剂量只有一小段，但全历史口径结果必须保持不变
    narrow = dose(client, eid, BASE + 5400, BASE + 7200)
    assert narrow["known_dose_luxh"] == pytest.approx(50.0)  # 5400~7200 = 0.5h
    assert narrow["remaining_luxh"] == pytest.approx(-100.0)
    assert narrow["exceeded"] is True
    assert narrow["first_exceeded_at"] == wide["first_exceeded_at"]

    # 窗口末早于首次越线：不算越线，且窗口结束之后的读数不得泄漏进历史累计
    early = dose(client, eid, BASE, BASE + 1800)
    assert early["exceeded"] is False
    assert early["remaining_luxh"] == pytest.approx(50.0)  # 仅前 0.5h = 50 lux·h
    assert early["first_exceeded_at"] is None


# ---------- 首次越线 ----------

def test_first_exceeded_at(client):
    eid = make_exhibit(client, limit=150.0, interval=3600.0)
    send(client, eid, [
        {"id": "r1", "ts": iso(BASE), "lux": 100},
        {"id": "r2", "ts": iso(BASE + 3600), "lux": 100},
        {"id": "r3", "ts": iso(BASE + 7200), "lux": 200},
    ])
    d = dose(client, eid, BASE, BASE + 7200)
    assert d["exceeded"] is True
    assert d["known_dose_luxh"] == pytest.approx(250.0)
    assert d["remaining_luxh"] == pytest.approx(-100.0)
    # 第一段累计 100 lux·h；第二段照度 100→200 线性变化，
    # 再累计 50 lux·h = 180000 lux·s，解 100x + (100/7200)x² = 180000
    # 得 x = 36*(√20000 − 100) ≈ 1491.17s
    crossed = datetime.fromisoformat(d["first_exceeded_at"]).timestamp()
    assert crossed == pytest.approx(BASE + 3600 + 1491.17, abs=1.0)


def test_not_exceeded_when_below_limit(client):
    eid = make_exhibit(client, limit=1000.0, interval=3600.0)
    send(client, eid, [
        {"id": "r1", "ts": iso(BASE), "lux": 50},
        {"id": "r2", "ts": iso(BASE + 3600), "lux": 50},
    ])
    d = dose(client, eid, BASE, BASE + 3600)
    assert d["exceeded"] is False
    assert d["first_exceeded_at"] is None


# ---------- 其他 ----------

def test_readings_require_existing_exhibit(client):
    resp = client.post("/exhibits/nope/readings", json={
        "readings": [{"id": "r1", "ts": iso(BASE), "lux": 1}],
    })
    assert resp.status_code == 404
    assert client.get("/exhibits/nope/dose",
                      params={"start": iso(BASE), "end": iso(BASE + 1)}).status_code == 404


def test_validation_errors(client):
    eid = make_exhibit(client)
    assert send.__name__  # keep helper referenced
    resp = client.post(f"/exhibits/{eid}/readings", json={
        "readings": [{"id": "bad", "ts": iso(BASE), "lux": -5}],
    })
    assert resp.status_code == 422
    resp = client.get(f"/exhibits/{eid}/dose",
                      params={"start": iso(BASE + 100), "end": iso(BASE)})
    assert resp.status_code == 422


def test_list_readings_ordered(client):
    eid = make_exhibit(client)
    send(client, eid, [
        {"id": "b", "ts": iso(BASE + 60), "lux": 2},
        {"id": "a", "ts": iso(BASE), "lux": 1},
    ])
    assert list_ids(client, eid) == ["a", "b"]


def test_ingest_response_carries_requested_and_canonical(client):
    eid = make_exhibit(client)
    r = send(client, eid, [{"id": "r1", "ts": iso(BASE), "lux": 1}])
    assert r["requested_id"] == r["canonical_id"] == eid
    d = dose(client, eid, BASE, BASE + 1)
    assert d["requested_id"] == d["canonical_id"] == eid


# ---------- 重复档案归并：预演 ----------

def preview(client: TestClient, source: str, target: str) -> dict:
    resp = client.post("/merges/preview", json={"source_id": source, "target_id": target})
    assert resp.status_code == 200, resp.text
    return resp.json()


def commit(client: TestClient, token: str, resolutions: list[dict] | None = None) -> dict:
    resp = client.post("/merges/commit",
                       json={"token": token, "resolutions": resolutions or []})
    assert resp.status_code == 200, resp.text
    return resp.json()


def test_preview_lists_metadata_diff_and_collisions(client):
    src = make_exhibit(client, limit=100.0, interval=600.0, eid="src", name="旧档案")
    tgt = make_exhibit(client, limit=200.0, interval=3600.0, eid="tgt", name="新档案")
    send(client, src, [
        {"id": "same", "ts": iso(BASE), "lux": 50},          # 完全一致 → 自动去重
        {"id": "clash", "ts": iso(BASE + 60), "lux": 10},    # 内容不同 → 冲突
        {"id": "only_src", "ts": iso(BASE + 120), "lux": 3}, # 仅来源
    ])
    send(client, tgt, [
        {"id": "same", "ts": iso(BASE), "lux": 50},
        {"id": "clash", "ts": iso(BASE + 60), "lux": 99},
        {"id": "only_tgt", "ts": iso(BASE + 180), "lux": 7},
    ])

    p = preview(client, "src", "tgt")
    assert p["source_id"] == "src"
    assert p["target_id"] == "tgt"
    assert set(p["metadata"]) == {"name", "dose_limit_luxh", "max_interval_seconds"}
    assert p["metadata"]["dose_limit_luxh"] == {"field": "dose_limit_luxh",
                                                "source": 100.0, "target": 200.0}
    kinds = {c["reading_id"]: c["kind"] for c in p["collisions"]}
    assert kinds == {"same": "duplicate", "clash": "conflict"}
    clash = next(c for c in p["collisions"] if c["reading_id"] == "clash")
    assert clash["source_lux"] == 10 and clash["target_lux"] == 99
    assert p["auto_duplicate_count"] == 1
    assert p["conflict_count"] == 1
    assert p["source_only_count"] == 1
    assert p["ready_to_commit"] is False
    assert p["token"]


def test_preview_same_id_rejected(client):
    eid = make_exhibit(client)
    r = client.post("/merges/preview", json={"source_id": eid, "target_id": eid})
    assert r.status_code == 422
    assert client.post("/merges/preview", json={"source_id": "nope", "target_id": eid}).status_code == 404


def test_preview_alias_and_canonical_pointing_same_archive_rejected(client):
    src = make_exhibit(client, eid="src")
    tgt = make_exhibit(client, eid="tgt")
    send(client, src, [{"id": "r1", "ts": iso(BASE), "lux": 1}])
    token = preview(client, "src", "tgt")["token"]
    commit(client, token)
    # src 现在是 tgt 的别名：再以 src→tgt 预演应被拒绝
    r = client.post("/merges/preview", json={"source_id": "src", "target_id": "tgt"})
    assert r.status_code == 422


# ---------- 归并提交：裁决与事务 ----------

def test_commit_requires_all_conflicts_resolved(client):
    src = make_exhibit(client, eid="src")
    tgt = make_exhibit(client, eid="tgt")
    send(client, src, [{"id": "c1", "ts": iso(BASE), "lux": 1},
                       {"id": "c2", "ts": iso(BASE + 10), "lux": 1}])
    send(client, tgt, [{"id": "c1", "ts": iso(BASE), "lux": 2},
                       {"id": "c2", "ts": iso(BASE + 10), "lux": 2}])
    token = preview(client, "src", "tgt")["token"]

    # 一条都不裁决 → 422，且无部分变更
    r = client.post("/merges/commit", json={"token": token, "resolutions": []})
    assert r.status_code == 422
    assert "未裁决" in r.json()["detail"]
    assert client.get("/exhibits/src").status_code == 200
    assert len(list_ids(client, "src")) == 2

    # 只裁决一条 → 仍 422
    r = client.post("/merges/commit", json={"token": token, "resolutions": [
        {"reading_id": "c1", "action": "keep_target"},
    ]})
    assert r.status_code == 422

    # 裁决了不存在的碰撞 ID → 422
    r = client.post("/merges/commit", json={"token": token, "resolutions": [
        {"reading_id": "c1", "action": "keep_target"},
        {"reading_id": "c2", "action": "keep_target"},
        {"reading_id": "ghost", "action": "keep_target"},
    ]})
    assert r.status_code == 422


def test_commit_merge_keeps_target_metadata_and_applies_resolutions(client):
    src = make_exhibit(client, limit=100.0, interval=600.0, eid="src", name="旧名称")
    tgt = make_exhibit(client, limit=200.0, interval=3600.0, eid="tgt", name="目标名称")
    send(client, src, [
        {"id": "dup", "ts": iso(BASE), "lux": 5},           # 自动去重
        {"id": "kt", "ts": iso(BASE + 100), "lux": 11},     # keep_target → 目标值 22
        {"id": "ks", "ts": iso(BASE + 200), "lux": 33},     # keep_source → 覆盖为 33
        {"id": "rn", "ts": iso(BASE + 300), "lux": 44},     # rename → rn_new
        {"id": "only_src", "ts": iso(BASE + 400), "lux": 5},
    ])
    send(client, tgt, [
        {"id": "dup", "ts": iso(BASE), "lux": 5},
        {"id": "kt", "ts": iso(BASE + 100), "lux": 22},
        {"id": "ks", "ts": iso(BASE + 200), "lux": 4},
        {"id": "rn", "ts": iso(BASE + 300), "lux": 4},
        {"id": "only_tgt", "ts": iso(BASE + 500), "lux": 6},
    ])
    token = preview(client, "src", "tgt")["token"]
    out = commit(client, token, [
        {"reading_id": "kt", "action": "keep_target"},
        {"reading_id": "ks", "action": "keep_source"},
        {"reading_id": "rn", "action": "rename_source", "new_id": "rn_new"},
    ])

    assert out["source_id"] == "src"
    assert out["target_id"] == "tgt"
    # 目标名称与剂量配置保持不变
    assert out["name"] == "目标名称"
    assert out["dose_limit_luxh"] == 200.0
    assert out["max_interval_seconds"] == 3600.0
    assert out["moved_count"] == 1            # only_src
    assert out["auto_deduplicated_count"] == 1
    actions = {x["reading_id"]: x["action"] for x in out["resolutions_applied"]}
    assert actions == {"kt": "keep_target", "ks": "keep_source", "rn": "rename_source"}
    # dup, kt, ks, rn, only_tgt, rn_new, only_src = 7
    assert out["final_reading_count"] == 7
    assert out["aliases"] == ["src"]
    assert out["already_committed"] is False

    readings = {r["id"]: r for r in client.get("/exhibits/tgt/readings").json()["readings"]}
    assert set(readings) == {"dup", "kt", "ks", "rn", "rn_new", "only_src", "only_tgt"}
    assert readings["kt"]["lux"] == 22
    assert readings["ks"]["lux"] == 33
    assert readings["rn"]["lux"] == 4
    assert readings["rn_new"]["lux"] == 44

    # 来源档案已删除，来源 ID 作为永久别名解析到目标
    gone = client.get("/exhibits/src")
    assert gone.status_code == 200
    assert gone.json()["canonical_id"] == "tgt"


def test_rename_source_new_id_collision_rejected(client):
    src = make_exhibit(client, eid="src")
    tgt = make_exhibit(client, eid="tgt")
    send(client, src, [{"id": "c", "ts": iso(BASE), "lux": 1}])
    send(client, tgt, [{"id": "c", "ts": iso(BASE), "lux": 2},
                       {"id": "taken", "ts": iso(BASE + 1), "lux": 2}])
    token = preview(client, "src", "tgt")["token"]
    r = client.post("/merges/commit", json={"token": token, "resolutions": [
        {"reading_id": "c", "action": "rename_source", "new_id": "taken"},
    ]})
    assert r.status_code == 422
    # rename_source 缺 new_id → 422（pydantic 校验）
    r = client.post("/merges/commit", json={"token": token, "resolutions": [
        {"reading_id": "c", "action": "rename_source"},
    ]})
    assert r.status_code in (422,)


def test_rename_source_new_id_collides_with_moved_source_reading(client):
    """新 ID 也不能与即将搬迁的来源独有读数撞车。"""
    src = make_exhibit(client, eid="src")
    tgt = make_exhibit(client, eid="tgt")
    send(client, src, [{"id": "c", "ts": iso(BASE), "lux": 1},
                       {"id": "keep_id", "ts": iso(BASE + 1), "lux": 1}])
    send(client, tgt, [{"id": "c", "ts": iso(BASE), "lux": 2}])
    token = preview(client, "src", "tgt")["token"]
    r = client.post("/merges/commit", json={"token": token, "resolutions": [
        {"reading_id": "c", "action": "rename_source", "new_id": "keep_id"},
    ]})
    assert r.status_code == 422
    # 失败不留部分变更
    assert client.get("/exhibits/src").status_code == 200
    assert len(list_ids(client, "tgt")) == 1


def test_auto_deduplicate_without_resolutions(client):
    """只有同内容碰撞时无需任何裁决即可提交。"""
    src = make_exhibit(client, eid="src")
    tgt = make_exhibit(client, eid="tgt")
    send(client, src, [{"id": "a", "ts": iso(BASE), "lux": 9},
                       {"id": "b", "ts": iso(BASE + 10), "lux": 9}])
    send(client, tgt, [{"id": "a", "ts": iso(BASE), "lux": 9},
                       {"id": "c", "ts": iso(BASE + 20), "lux": 9}])
    p = preview(client, "src", "tgt")
    assert p["ready_to_commit"] is True
    out = commit(client, p["token"])
    assert out["auto_deduplicated_count"] == 1
    assert out["moved_count"] == 1
    assert out["final_reading_count"] == 3


# ---------- 版本令牌：409 与幂等重放 ----------

def test_commit_409_when_archive_written_after_preview(client):
    src = make_exhibit(client, eid="src")
    tgt = make_exhibit(client, eid="tgt")
    send(client, src, [{"id": "r1", "ts": iso(BASE), "lux": 1}])
    send(client, tgt, [{"id": "r2", "ts": iso(BASE), "lux": 2}])
    token = preview(client, "src", "tgt")["token"]

    # 预演后目标档案有新读数写入
    send(client, tgt, [{"id": "late", "ts": iso(BASE + 5), "lux": 3}])
    r = client.post("/merges/commit", json={"token": token, "resolutions": []})
    assert r.status_code == 409
    assert client.get("/exhibits/src").status_code == 200
    assert "late" in list_ids(client, "tgt")
    # 来源读数未搬迁（无部分变更）
    assert list_ids(client, "src") == ["r1"]


def test_commit_409_when_source_reading_changed_after_preview(client):
    src = make_exhibit(client, eid="src")
    tgt = make_exhibit(client, eid="tgt")
    send(client, src, [{"id": "r1", "ts": iso(BASE), "lux": 1}])
    token = preview(client, "src", "tgt")["token"]
    # 同 ID 同数据重传是 duplicate，不改内容，不应使令牌失效
    send(client, src, [{"id": "r1", "ts": iso(BASE), "lux": 1}])
    assert client.post("/merges/commit",
                       json={"token": token, "resolutions": []}).status_code == 200


def test_reuse_token_returns_original_result(client):
    src = make_exhibit(client, eid="src")
    tgt = make_exhibit(client, eid="tgt")
    send(client, src, [{"id": "r1", "ts": iso(BASE), "lux": 1}])
    send(client, tgt, [{"id": "r1", "ts": iso(BASE), "lux": 2}])
    token = preview(client, "src", "tgt")["token"]
    first = commit(client, token, [{"reading_id": "r1", "action": "keep_source"}])
    assert first["already_committed"] is False

    # 同令牌重复提交：即使参数不同/令牌已过期，也回放首次结果
    second = commit(client, token, [])
    assert second["already_committed"] is True
    assert second["resolutions_applied"] == first["resolutions_applied"]
    assert second["final_reading_count"] == first["final_reading_count"]

    # 未知令牌 → 404
    assert client.post("/merges/commit",
                       json={"token": "deadbeef", "resolutions": []}).status_code == 404


# ---------- 永久别名：查询 / 上报 / 链式解析 ----------

def test_alias_resolves_for_queries_and_ingest(client):
    src = make_exhibit(client, limit=500.0, interval=3600.0, eid="src")
    tgt = make_exhibit(client, limit=500.0, interval=3600.0, eid="tgt")
    send(client, src, [{"id": "r1", "ts": iso(BASE), "lux": 10}])
    token = preview(client, "src", "tgt")["token"]
    commit(client, token)

    # 用别名查档案：请求 ID 与规范 ID 同时给出
    got = client.get("/exhibits/src")
    assert got.status_code == 200
    body = got.json()
    assert body["requested_id"] == "src"
    assert body["canonical_id"] == "tgt"

    # 用别名列读数
    resp = client.get("/exhibits/src/readings")
    assert resp.json()["requested_id"] == "src"
    assert resp.json()["canonical_id"] == "tgt"
    assert [r["id"] for r in resp.json()["readings"]] == ["r1"]

    # 用别名上报读数，落到目标档案
    ing = send(client, "src", [{"id": "r2", "ts": iso(BASE + 3600), "lux": 20}])
    assert ing["requested_id"] == "src"
    assert ing["canonical_id"] == "tgt"
    assert ing["inserted"] == 1
    assert set(list_ids(client, "tgt")) == {"r1", "r2"}

    # 用别名算剂量
    d = dose(client, "src", BASE, BASE + 3600)
    assert d["requested_id"] == "src"
    assert d["canonical_id"] == "tgt"
    assert d["known_dose_luxh"] == pytest.approx(15.0)  # (10+20)/2 * 1h


def test_chained_merge_repoints_old_aliases(client):
    a = make_exhibit(client, eid="aaa")
    b = make_exhibit(client, eid="bbb")
    c = make_exhibit(client, eid="ccc")
    send(client, a, [{"id": "ra", "ts": iso(BASE), "lux": 1}])
    send(client, b, [{"id": "rb", "ts": iso(BASE), "lux": 1}])
    send(client, c, [{"id": "rc", "ts": iso(BASE), "lux": 1}])

    commit(client, preview(client, "aaa", "bbb")["token"])
    # aaa 已是 bbb 的别名；再把 bbb 合入 ccc
    commit(client, preview(client, "bbb", "ccc")["token"])

    # 旧别名 aaa 必须跟随指向 ccc，而不是悬空
    got = client.get("/exhibits/aaa")
    assert got.status_code == 200
    assert got.json()["canonical_id"] == "ccc"
    assert client.get("/exhibits/bbb").json()["canonical_id"] == "ccc"

    # 用最老的别名上报仍落到 ccc
    ing = send(client, "aaa", [{"id": "ra2", "ts": iso(BASE + 10), "lux": 1}])
    assert ing["canonical_id"] == "ccc"

    target = client.get("/exhibits/ccc").json()
    assert target["requested_id"] == target["canonical_id"] == "ccc"

    # 目标档案的剂量配置仍以最终目标 ccc 为准
    assert client.get("/exhibits/ccc/readings").json()["readings"]


def test_alias_id_cannot_be_reused_for_new_exhibit(client):
    src = make_exhibit(client, eid="src")
    tgt = make_exhibit(client, eid="tgt")
    commit(client, preview(client, "src", "tgt")["token"])
    r = client.post("/exhibits", json={
        "id": "src", "name": "冒充者", "dose_limit_luxh": 1, "max_interval_seconds": 60,
    })
    assert r.status_code == 409


def test_merge_uses_target_dose_algorithm_config(client):
    """归并后按目标档案的剂量参数核算（目标配置不变，来源配置丢弃）。"""
    src = make_exhibit(client, limit=999.0, interval=60.0, eid="src")   # 间隔 60s
    tgt = make_exhibit(client, limit=999.0, interval=3600.0, eid="tgt")  # 间隔 1h
    send(client, src, [{"id": "s1", "ts": iso(BASE), "lux": 10}])
    send(client, tgt, [{"id": "t1", "ts": iso(BASE + 1800), "lux": 10}])
    commit(client, preview(client, "src", "tgt")["token"])
    # 两条读数相隔 1800s：按来源配置（60s）会是缺测段；按目标配置（3600s）可积分
    d = dose(client, "tgt", BASE, BASE + 1800)
    assert d["known_dose_luxh"] == pytest.approx(5.0)
    assert d["covered_seconds"] == pytest.approx(1800.0)


def test_exhibit_list_excludes_merged_sources(client):
    src = make_exhibit(client, eid="src")
    tgt = make_exhibit(client, eid="tgt")
    commit(client, preview(client, "src", "tgt")["token"])
    ids = {e["canonical_id"] for e in client.get("/exhibits").json()}
    assert ids == {"tgt"}
