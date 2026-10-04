"""照度上报批次更正：作废收据中实际插入的读数，幂等落库，查询立即排除。"""
from __future__ import annotations

import json
import sqlite3
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


def make_exhibit(client: TestClient, eid: str | None = None,
                 limit: float = 1000.0, interval: float = 3600.0) -> str:
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


def void(client: TestClient, exhibit_id: str, receipt_id: str, request_key: str,
         reason: str = "传感器故障批次作废"):
    return client.post(
        f"/exhibits/{exhibit_id}/receipts/corrections",
        json={"receipt_id": receipt_id, "request_key": request_key, "reason": reason},
    )


# ---------- 基本作废 ----------

def test_correction_voids_only_inserted_and_excludes_everywhere(env):
    client, _ = env
    eid = make_exhibit(client, limit=100000.0)
    out = send(client, eid, [
        {"id": "r1", "ts": iso(BASE), "lux": 100},
        {"id": "r2", "ts": iso(BASE + 3600), "lux": 100},
        {"id": "r3", "ts": iso(BASE + 7200), "lux": 100},
    ])
    receipt_id = out["receipt"]["receipt_id"]

    # 第二次上报：r1 同数据 duplicate，r4 inserted
    out2 = send(client, eid, [
        {"id": "r1", "ts": iso(BASE), "lux": 100},
        {"id": "r4", "ts": iso(BASE + 10800), "lux": 100},
    ])
    rid2 = out2["receipt"]["receipt_id"]

    corr = void(client, eid, receipt_id, "key-1")
    assert corr.status_code == 200, corr.text
    data = corr.json()
    assert data["voided_count"] == 3
    assert [v["id"] for v in data["voided_readings"]] == ["r1", "r2", "r3"]
    assert data["seq"] == 1 and data["chain_id"] == data["canonical_id"] == eid
    assert data["receipt_id"] == receipt_id
    assert data["already_committed"] is False
    assert data["voided_readings"][0]["ts"].endswith("Z")

    # 读数列表立即排除已作废读数（只剩第二张收据插入的 r4）
    ids = [r["id"] for r in client.get(f"/exhibits/{eid}/readings").json()["readings"]]
    assert ids == ["r4"]

    # 剂量按剩余时间线重算：窗口内只剩 r4 一个点，无线性段可积
    dose = client.get(
        f"/exhibits/{eid}/dose",
        params={"start": iso(BASE), "end": iso(BASE + 10800)},
    ).json()
    assert dose["known_dose_luxh"] == 0.0
    assert dose["reading_count"] == 1

    # 诊断基于剩余时间线：r4 单点无任何事件（作废前三段间隔均合规，本就无 gap；
    # 这里验证诊断接口不再读到作废行——换成会产生 stuck 的场景单独验证）
    diag = client.post(f"/exhibits/{eid}/readings/diagnose", json={
        "start": iso(BASE), "end": iso(BASE + 20000),
        "max_rate_lux_per_second": 1.0,
        "stuck_tolerance_lux": 2.0, "min_stuck_seconds": 1800,
    })
    assert diag.status_code == 200
    assert diag.json()["events"] == []

    # 第二张收据中 r1 是 duplicate（已随第一张收据作废），只插入 r4
    corr2 = void(client, eid, rid2, "key-2", reason="重复批次核对")
    assert corr2.status_code == 200
    assert corr2.json()["voided_count"] == 1
    assert [v["id"] for v in corr2.json()["voided_readings"]] == ["r4"]
    # 作废后全部读数退出口径
    assert client.get(f"/exhibits/{eid}/readings").json()["readings"] == []


def test_voided_middle_reading_recomputes_gap_in_dose_and_diagnose(env):
    client, _ = env
    eid = make_exhibit(client, limit=100000.0, interval=3600.0)
    out = send(client, eid, [
        {"id": "r1", "ts": iso(BASE), "lux": 100},
        {"id": "r2", "ts": iso(BASE + 1800), "lux": 100},
        {"id": "r3", "ts": iso(BASE + 3600), "lux": 100},
    ])
    # 作废前：整段覆盖 3600s、剂量 100 lux·h
    d0 = client.get(
        f"/exhibits/{eid}/dose",
        params={"start": iso(BASE), "end": iso(BASE + 3600)},
    ).json()
    assert d0["known_dose_luxh"] == pytest.approx(100.0)
    assert d0["covered_seconds"] == 3600.0

    rid = out["receipt"]["receipt_id"]
    # 单独再报一批，只作废 r2 需要另发一张收据——直接用同收据作废 r2 不支持选择，
    # 服务作废的是收据中全部 inserted；这里改为三批各一条，精确作废中间批。
    eid2 = make_exhibit(client, limit=100000.0, interval=3600.0)
    a = send(client, eid2, [{"id": "a", "ts": iso(BASE), "lux": 100}])
    send(client, eid2, [{"id": "b", "ts": iso(BASE + 1800), "lux": 100}])
    send(client, eid2, [{"id": "c", "ts": iso(BASE + 3600), "lux": 100}])
    mid = client.get(f"/exhibits/{eid2}/receipts").json()["receipts"][1]
    corr = void(client, eid2, mid["receipt_id"], "k-mid")
    assert corr.status_code == 200
    assert [v["id"] for v in corr.json()["voided_readings"]] == ["b"]

    d = client.get(
        f"/exhibits/{eid2}/dose",
        params={"start": iso(BASE), "end": iso(BASE + 3600)},
    ).json()
    # a 与 c 间隔 3600s（恰好等于上限）仍可积分：梯形 100 lux × 1h
    assert d["known_dose_luxh"] == pytest.approx(100.0)
    assert d["reading_count"] == 2

    # 作废后剩余两点间隔超过 max_interval 的场景：
    eid3 = make_exhibit(client, limit=100000.0, interval=1800.0)
    send(client, eid3, [{"id": "x", "ts": iso(BASE), "lux": 100}])
    m = send(client, eid3, [{"id": "y", "ts": iso(BASE + 1800), "lux": 100}])
    send(client, eid3, [{"id": "z", "ts": iso(BASE + 3600), "lux": 100}])
    void(client, eid3, m["receipt"]["receipt_id"], "k-y")
    d3 = client.get(
        f"/exhibits/{eid3}/dose",
        params={"start": iso(BASE), "end": iso(BASE + 3600)},
    ).json()
    assert d3["known_dose_luxh"] == 0.0
    assert d3["unknown_seconds"] == 3600.0
    diag = client.post(f"/exhibits/{eid3}/readings/diagnose", json={
        "start": iso(BASE), "end": iso(BASE + 3600),
        "max_rate_lux_per_second": 1.0,
        "stuck_tolerance_lux": 2.0, "min_stuck_seconds": 1.0,
    }).json()
    assert [e["type"] for e in diag["events"]] == ["gap"]
    assert diag["events"][0]["reading_ids"] == ["x", "z"]

    # 第一张收据（整批）未作废，rid 依然可以查询且内容不变
    rcpt = client.get(f"/exhibits/{eid}/receipts/1").json()
    assert rcpt["voids"] == []
    assert rcpt["content_hash"] == out["receipt"]["content_hash"]
    assert rcpt["chain_hash"] == out["receipt"]["chain_hash"]


def test_diagnose_recomputes_after_voiding_jump_reading(env):
    client, _ = env
    eid = make_exhibit(client, limit=100000.0, interval=3600.0)
    send(client, eid, [{"id": "a", "ts": iso(BASE), "lux": 100}])
    jump = send(client, eid, [{"id": "b", "ts": iso(BASE + 60), "lux": 500}])
    body = {
        "start": iso(BASE), "end": iso(BASE + 3600),
        "max_rate_lux_per_second": 1.0,
        "stuck_tolerance_lux": 2.0, "min_stuck_seconds": 1.0,
    }
    assert [e["type"] for e in client.post(
        f"/exhibits/{eid}/readings/diagnose", json=body).json()["events"]] == ["jump"]
    assert void(client, eid, jump["receipt"]["receipt_id"], "kj").status_code == 200
    events = client.post(f"/exhibits/{eid}/readings/diagnose", json=body).json()["events"]
    assert events == []


# ---------- 收据查询标作废，链哈希不变 ----------

def test_receipt_marked_void_but_content_seq_and_hashes_unchanged(env):
    client, _ = env
    eid = make_exhibit(client, limit=100000.0)
    first = send(client, eid, [{"id": "r1", "ts": iso(BASE), "lux": 100}])
    second = send(client, eid, [{"id": "r2", "ts": iso(BASE + 60), "lux": 100}])
    assert void(client, eid, first["receipt"]["receipt_id"], "k1").status_code == 200

    # 单张查询标出作废；content / 哈希 / 序号完全不变
    one = client.get(f"/exhibits/{eid}/receipts/1").json()
    assert one["seq"] == 1
    assert one["content"] == first["receipt"]["content"]
    assert one["content_hash"] == first["receipt"]["content_hash"]
    assert one["chain_hash"] == first["receipt"]["chain_hash"]
    assert len(one["voids"]) == 1
    v = one["voids"][0]
    assert v["request_key"] == "k1" and v["voided_count"] == 1
    assert [x["id"] for x in v["voided_readings"]] == ["r1"]

    # 列表与按链标识直查同样带标记；未作废的第二张为空
    lst = client.get(f"/exhibits/{eid}/receipts").json()["receipts"]
    assert len(lst[0]["voids"]) == 1 and lst[1]["voids"] == []
    chain = client.get(f"/receipts/chains/{eid}").json()["receipts"]
    assert len(chain[0]["voids"]) == 1
    # 第二张收据序号、prev_hash 衔接未受影响（未插收据，链原样）
    assert lst[1]["prev_hash"] == second["receipt"]["prev_hash"]

    # 整链校验仍然通过
    verify = client.get(f"/exhibits/{eid}/receipts/verify").json()
    assert verify["ok"] is True and verify["checked"] == 2


# ---------- 幂等：重放 / 换目标 409 / 重复更正 409 ----------

def test_request_key_replay_returns_original_result(env):
    client, _ = env
    eid = make_exhibit(client, limit=100000.0)
    out = send(client, eid, [{"id": "r1", "ts": iso(BASE), "lux": 100}])
    rid = out["receipt"]["receipt_id"]
    first = void(client, eid, rid, "same-key", reason="原始原因")
    assert first.status_code == 200 and first.json()["already_committed"] is False

    again = void(client, eid, rid, "same-key", reason="原始原因")
    assert again.status_code == 200
    body = again.json()
    assert body["already_committed"] is True
    assert body["voided_count"] == 1
    assert body["voided_at"] == first.json()["voided_at"]

    # 用别名重放（归并后别名等价）——先不归并，直接用规范 ID 本身已覆盖；
    # 改原因 / 改收据 → 409
    assert void(client, eid, rid, "same-key", reason="别的原因").status_code == 409
    other = send(client, eid, [{"id": "r2", "ts": iso(BASE + 60), "lux": 100}])
    resp = void(client, eid, other["receipt"]["receipt_id"], "same-key")
    assert resp.status_code == 409
    assert "已绑定另一次更正" in resp.json()["detail"]


def test_replay_via_alias_after_merge_and_other_exhibit_conflict(env):
    client, _ = env
    src = make_exhibit(client, eid="src", limit=100000.0)
    tgt = make_exhibit(client, eid="tgt", limit=100000.0)
    out = send(client, tgt, [{"id": "t1", "ts": iso(BASE), "lux": 100}])
    rid = out["receipt"]["receipt_id"]
    assert void(client, "tgt", rid, "k-alias").status_code == 200

    # 归并 src -> tgt 后，用别名 src 重放同一 request_key：解析到同一规范档案 → 回放
    pv = client.post("/merges/preview", json={"source_id": "src", "target_id": "tgt"})
    assert pv.status_code == 200
    assert client.post("/merges/commit",
                       json={"token": pv.json()["token"], "resolutions": []}).status_code == 200
    replay = void(client, "src", rid, "k-alias")
    assert replay.status_code == 200
    assert replay.json()["already_committed"] is True
    assert replay.json()["canonical_id"] == "tgt"

    # 换一个完全不相关的 request_key 指向已作废收据 → 409（已被更正）
    other = void(client, "tgt", rid, "different-key")
    assert other.status_code == 409
    assert "已被更正" in other.json()["detail"]


def test_same_receipt_cannot_be_corrected_twice(env):
    client, _ = env
    eid = make_exhibit(client, limit=100000.0)
    out = send(client, eid, [{"id": "r1", "ts": iso(BASE), "lux": 100}])
    rid = out["receipt"]["receipt_id"]
    assert void(client, eid, rid, "key-a").status_code == 200
    resp = void(client, eid, rid, "key-b")
    assert resp.status_code == 409
    assert "已被更正" in resp.json()["detail"]


# ---------- 409：收据链 / 归并状态 / 读数归属变化 ----------

def test_correcting_source_chain_receipt_after_merge_is_409(env):
    client, _ = env
    src = make_exhibit(client, eid="src", limit=100000.0)
    tgt = make_exhibit(client, eid="tgt", limit=100000.0)
    out = send(client, src, [
        {"id": "s1", "ts": iso(BASE), "lux": 100},
        {"id": "s2", "ts": iso(BASE + 60), "lux": 100},
    ])
    rid = out["receipt"]["receipt_id"]
    pv = client.post("/merges/preview", json={"source_id": "src", "target_id": "tgt"})
    assert client.post("/merges/commit",
                       json={"token": pv.json()["token"], "resolutions": []}).status_code == 200

    # 归并后用来源别名更正来源链收据：链保留但档案已删除、读数已搬到 tgt → 409
    resp = void(client, "src", rid, "k-after-merge")
    assert resp.status_code == 409
    assert "链" in resp.json()["detail"] or "归并" in resp.json()["detail"]
    # 无部分变更：读数仍在（目标档案下），更正记录不存在
    ids = [r["id"] for r in client.get("/exhibits/tgt/readings").json()["readings"]]
    assert set(ids) == {"s1", "s2"}
    # 来源链收据仍可按链标识查询、且未被标出作废
    rcpt = client.get(f"/receipts/chains/src").json()["receipts"][0]
    assert rcpt["voids"] == []


def test_correcting_target_receipt_after_keep_source_override_is_409(env):
    """归并 keep_source 覆盖了目标读数内容：目标收据记录的读数归属已变化 → 409。"""
    client, _ = env
    src = make_exhibit(client, eid="src", limit=100000.0)
    tgt = make_exhibit(client, eid="tgt", limit=100000.0)
    # 目标先上报 r1（这张收据记录 lux=100）
    out = send(client, tgt, [{"id": "r1", "ts": iso(BASE), "lux": 100}])
    rid = out["receipt"]["receipt_id"]
    # 来源同 ID 不同内容（冲突），归并时 keep_source 覆盖目标
    send(client, src, [{"id": "r1", "ts": iso(BASE), "lux": 999}])
    pv = client.post("/merges/preview", json={"source_id": "src", "target_id": "tgt"}).json()
    assert pv["conflict_count"] == 1
    committed = client.post("/merges/commit", json={
        "token": pv["token"],
        "resolutions": [{"reading_id": "r1", "action": "keep_source"}],
    })
    assert committed.status_code == 200

    resp = void(client, "tgt", rid, "k-override")
    assert resp.status_code == 409
    assert "归属已变化" in resp.json()["detail"] or "不一致" in resp.json()["detail"]
    # 读数仍在、未作废
    rows = client.get("/exhibits/tgt/readings").json()["readings"]
    assert [(r["id"], r["lux"]) for r in rows] == [("r1", 999.0)]


def test_broken_chain_rejected_with_409(env):
    client, db_file = env
    eid = make_exhibit(client, limit=100000.0)
    out = send(client, eid, [{"id": "r1", "ts": iso(BASE), "lux": 100}])
    rid = out["receipt"]["receipt_id"]
    conn = sqlite3.connect(str(db_file))
    conn.execute(
        "UPDATE ingest_receipts SET chain_hash = ? WHERE receipt_id = ?",
        ("f" * 64, rid),
    )
    conn.commit()
    conn.close()

    resp = void(client, eid, rid, "k-tampered")
    assert resp.status_code == 409
    assert "链" in resp.json()["detail"]
    # 读数未被作废
    assert [r["id"] for r in client.get(f"/exhibits/{eid}/readings").json()["readings"]] == ["r1"]


def test_unknown_receipt_and_exhibit_status_codes(env):
    client, _ = env
    eid = make_exhibit(client, limit=100000.0)
    assert void(client, eid, "deadbeef", "k").status_code == 404
    assert void(client, "ghost", "deadbeef", "k").status_code == 404


# ---------- 事务幂等：并发 ----------

def test_concurrent_different_request_keys_only_one_committed(env):
    client, _ = env
    eid = make_exhibit(client, limit=100000.0)
    out = send(client, eid, [{"id": "r1", "ts": iso(BASE), "lux": 100}])
    rid = out["receipt"]["receipt_id"]
    results: list[int] = []

    def post(i: int) -> None:
        r = void(client, eid, rid, f"conc-key-{i}")
        results.append(r.status_code)

    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(post, range(8)))
    assert sorted(results) == [200] + [409] * 7
    # 读数只被作废一次，仍是作废态；只有一条更正记录
    assert client.get(f"/exhibits/{eid}/readings").json()["readings"] == []


def test_concurrent_same_request_key_single_result(env):
    client, _ = env
    eid = make_exhibit(client, limit=100000.0)
    out = send(client, eid, [{"id": "r1", "ts": iso(BASE), "lux": 100}])
    rid = out["receipt"]["receipt_id"]
    statuses: list[int] = []
    replays: list[bool] = []

    def post(_: int) -> None:
        r = void(client, eid, rid, "same-conc-key")
        statuses.append(r.status_code)
        replays.append(r.json().get("already_committed"))

    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(post, range(8)))
    assert statuses == [200] * 8
    assert replays.count(False) == 1 and replays.count(True) == 7


# ---------- 作废后读数 ID 重报 ----------

def test_reupload_voided_reading_id_does_not_resurrect(env):
    client, _ = env
    eid = make_exhibit(client, limit=100000.0)
    out = send(client, eid, [{"id": "r1", "ts": iso(BASE), "lux": 100}])
    assert void(client, eid, out["receipt"]["receipt_id"], "k").status_code == 200

    # 同 ID 同数据重报：duplicate，行仍是作废态，不会复活
    again = send(client, eid, [{"id": "r1", "ts": iso(BASE), "lux": 100}])
    assert again["results"] == [{"id": "r1", "status": "duplicate"}]
    assert client.get(f"/exhibits/{eid}/readings").json()["readings"] == []
    # 同 ID 不同数据：conflict
    conflict = send(client, eid, [{"id": "r1", "ts": iso(BASE), "lux": 101}])
    assert conflict["results"] == [{"id": "r1", "status": "conflict"}]


# ---------- 归并携带作废状态 ----------

def test_merge_carries_voided_readings_and_correction_records_survive(env):
    client, _ = env
    src = make_exhibit(client, eid="src", limit=100000.0)
    tgt = make_exhibit(client, eid="tgt", limit=100000.0)
    out = send(client, src, [
        {"id": "s1", "ts": iso(BASE), "lux": 100},
        {"id": "s2", "ts": iso(BASE + 60), "lux": 100},
    ])
    assert void(client, "src", out["receipt"]["receipt_id"], "k-src").status_code == 200
    send(client, tgt, [{"id": "t1", "ts": iso(BASE + 120), "lux": 100}])

    pv = client.post("/merges/preview", json={"source_id": "src", "target_id": "tgt"})
    assert pv.status_code == 200
    committed = client.post("/merges/commit",
                            json={"token": pv.json()["token"], "resolutions": []})
    assert committed.status_code == 200, committed.text

    # 作废读数搬到目标后仍为作废态：列表/剂量只看到 t1
    ids = [r["id"] for r in client.get("/exhibits/tgt/readings").json()["readings"]]
    assert ids == ["t1"]

    # 更正记录不随档案删除，来源链收据仍标出作废；两条链均校验通过
    src_rcpt = client.get("/receipts/chains/src").json()["receipts"][0]
    assert len(src_rcpt["voids"]) == 1
    assert [v["id"] for v in src_rcpt["voids"][0]["voided_readings"]] == ["s1", "s2"]
    assert client.get("/receipts/chains/src/verify").json()["ok"] is True
    assert client.get("/receipts/chains/tgt/verify").json()["ok"] is True


def test_preview_token_invalidated_by_correction_after_preview(env):
    """预演后发生批次更正（读数集合版本变化）→ 提交 409。"""
    client, _ = env
    src = make_exhibit(client, eid="src", limit=100000.0)
    tgt = make_exhibit(client, eid="tgt", limit=100000.0)
    out = send(client, src, [{"id": "s1", "ts": iso(BASE), "lux": 100}])
    send(client, tgt, [{"id": "t1", "ts": iso(BASE + 60), "lux": 100}])
    pv = client.post("/merges/preview",
                     json={"source_id": "src", "target_id": "tgt"}).json()
    # 预演后作废来源读数
    assert void(client, "src", out["receipt"]["receipt_id"], "k-late").status_code == 200
    resp = client.post("/merges/commit", json={"token": pv["token"], "resolutions": []})
    assert resp.status_code == 409


# ---------- 旧库迁移 ----------

def test_legacy_db_without_void_columns_migrates(tmp_path, monkeypatch):
    db_file = tmp_path / "legacy.db"
    # 手工建一个只有旧 readings 表结构的库
    conn = sqlite3.connect(str(db_file))
    conn.execute(
        "CREATE TABLE exhibits (id TEXT PRIMARY KEY, name TEXT NOT NULL, "
        "dose_limit_luxh REAL NOT NULL, max_interval_seconds REAL NOT NULL, created_at TEXT)"
    )
    conn.execute(
        "CREATE TABLE readings (exhibit_id TEXT NOT NULL, reading_id TEXT NOT NULL, "
        "ts REAL NOT NULL, lux REAL NOT NULL, "
        "received_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')), "
        "PRIMARY KEY (exhibit_id, reading_id))"
    )
    conn.execute(
        "INSERT INTO exhibits VALUES ('e1', '旧档案', 1000, 3600, '2026-01-01T00:00:00Z')"
    )
    conn.execute(
        "INSERT INTO readings VALUES ('e1', 'old1', 1700000000, 50, '2026-01-01T00:00:00Z')"
    )
    conn.commit()
    conn.close()

    monkeypatch.setenv("DB_PATH", str(db_file))
    from app.main import app

    with TestClient(app) as c:
        # 旧读数迁移后默认未作废，照常出现在列表/剂量中
        rows = c.get("/exhibits/e1/readings").json()["readings"]
        assert [r["id"] for r in rows] == ["old1"]
        # 旧读数没有对应收据（早于收据功能上线）；对新上报可正常更正
        out = send(c, "e1", [{"id": "new1", "ts": iso(BASE), "lux": 100}])
        corr = void(c, "e1", out["receipt"]["receipt_id"], "k-migrated")
        assert corr.status_code == 200
        assert corr.json()["voided_count"] == 1
        ids = [r["id"] for r in c.get("/exhibits/e1/readings").json()["readings"]]
        assert ids == ["old1"]


def test_correcting_all_duplicate_receipt_voids_zero_and_is_replayable(env):
    client, _ = env
    eid = make_exhibit(client, limit=100000.0)
    send(client, eid, [{"id": "r1", "ts": iso(BASE), "lux": 100}])
    dup = send(client, eid, [{"id": "r1", "ts": iso(BASE), "lux": 100}])
    corr = void(client, eid, dup["receipt"]["receipt_id"], "k-dup")
    assert corr.status_code == 200
    assert corr.json()["voided_count"] == 0
    assert corr.json()["voided_readings"] == []
    again = void(client, eid, dup["receipt"]["receipt_id"], "k-dup")
    assert again.status_code == 200
    assert again.json()["already_committed"] is True
    # 同一张零作废收据也不能换 request_key 再更正
    assert void(client, eid, dup["receipt"]["receipt_id"], "k-dup-2").status_code == 409
