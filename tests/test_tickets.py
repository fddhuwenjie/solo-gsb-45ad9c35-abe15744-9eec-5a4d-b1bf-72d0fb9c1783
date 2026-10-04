"""照度异常处置单：创建固化快照、request_key 幂等、结案核对与举证。"""
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


def make_exhibit(client: TestClient, eid: str | None = None,
                 limit: float = 100000.0, interval: float = 3600.0) -> str:
    body = {"name": "唐代绢画", "dose_limit_luxh": limit, "max_interval_seconds": interval}
    if eid is not None:
        body["id"] = eid
    resp = client.post("/exhibits", json=body)
    assert resp.status_code == 201, resp.text
    return resp.json()["canonical_id"]


def set_calendar_24h(client: TestClient, eid: str) -> None:
    """全天 24 小时开放（00:00–00:00 跨午夜到次日同一时刻）。"""
    resp = client.put(
        f"/exhibits/{eid}/calendar",
        json={
            "timezone": "UTC",
            "weekly": [{"weekday": d, "start": "00:00", "end": "00:00"}
                       for d in range(1, 8)],
        },
    )
    assert resp.status_code == 200, resp.text


def send(client: TestClient, exhibit_id: str, readings: list[dict]) -> dict:
    resp = client.post(f"/exhibits/{exhibit_id}/readings", json={"readings": readings})
    assert resp.status_code == 200, resp.text
    return resp.json()


def create_ticket(client: TestClient, eid: str, key: str = "t-1",
                  start: float = BASE, end: float = BASE + 6 * 3600, **kw):
    body = {
        "start": iso(start),
        "end": iso(end),
        "max_rate_lux_per_second": 1.0,
        "stuck_tolerance_lux": 2.0,
        "min_stuck_seconds": 1800,
        "request_key": key,
    }
    body.update(kw)
    return client.post(f"/exhibits/{eid}/illumination-tickets", json=body)


def close_ticket(client: TestClient, ticket_id: str, digest: str,
                 decision: str = "accepted", note: str = "说明", **kw):
    body = {"expected_digest": digest, "decision": decision, "note": note}
    body.update(kw)
    return client.post(f"/illumination-tickets/{ticket_id}/close", json=body)


def stuck_exhibit(client: TestClient, eid: str | None = None,
                  limit: float = 100000.0) -> tuple[str, dict]:
    """造一件带 stuck 异常的展品：4 条平稳读数成链（3 小时 ≥ min_stuck）。"""
    eid = make_exhibit(client, eid, limit=limit)
    set_calendar_24h(client, eid)
    out = send(client, eid, [
        {"id": "r1", "ts": iso(BASE), "lux": 100},
        {"id": "r2", "ts": iso(BASE + 3600), "lux": 100},
        {"id": "r3", "ts": iso(BASE + 7200), "lux": 100},
        {"id": "r4", "ts": iso(BASE + 10800), "lux": 100},
    ])
    return eid, out


# ---------- 创建：固化快照与 digest ----------

def test_create_freezes_snapshot_and_returns_digest(env):
    client, _ = env
    eid, ingest = stuck_exhibit(client)
    resp = create_ticket(client, eid)
    assert resp.status_code == 200, resp.text
    t = resp.json()
    assert t["status"] == "open"
    assert t["already_committed"] is False
    assert t["requested_id"] == eid and t["canonical_id"] == eid
    assert len(t["digest"]) == 64
    # 快照含重算的 stuck 事件、calendar=display 剂量与收据链末端哈希
    events = t["snapshot"]["events"]
    assert [e["type"] for e in events] == ["stuck"]
    assert events[0]["reading_ids"] == ["r1", "r2", "r3", "r4"]
    dose = t["snapshot"]["dose"]
    assert dose["exceeded"] is False
    assert dose["closed_seconds"] == 0.0
    assert dose["known_dose_luxh"] == pytest.approx(300.0)  # 100 lux × 3 h
    chain = t["snapshot"]["receipt_chain"]
    assert chain["chain_id"] == eid and chain["seq"] == 1
    assert chain["chain_hash"] == ingest["receipt"]["chain_hash"]
    assert t["snapshot"]["thresholds"]["min_stuck_seconds"] == 1800.0

    # GET 单张：快照与 digest 一致，open 状态附带 current_digest == digest
    got = client.get(f"/illumination-tickets/{t['ticket_id']}")
    assert got.status_code == 200
    g = got.json()
    assert g["digest"] == t["digest"]
    assert g["snapshot"] == t["snapshot"]
    assert g["current_digest"] == t["digest"]


def test_create_without_calendar_409(env):
    client, _ = env
    eid = make_exhibit(client)
    send(client, eid, [{"id": "r1", "ts": iso(BASE), "lux": 100}])
    resp = create_ticket(client, eid)
    assert resp.status_code == 409
    assert "日历" in resp.json()["detail"]


def test_create_unknown_exhibit_404_and_window_validation(env):
    client, _ = env
    assert create_ticket(client, "no-such").status_code == 404
    eid, _ = stuck_exhibit(client)
    assert create_ticket(client, eid, start=BASE, end=BASE).status_code == 422


def test_create_replay_same_request_key(env):
    client, _ = env
    eid, _ = stuck_exhibit(client)
    first = create_ticket(client, eid, key="k-1").json()
    # 创建后再上报读数改变库内状态：重放仍返回首次固化的原快照（不可变）
    send(client, eid, [{"id": "r9", "ts": iso(BASE + 5 * 3600), "lux": 500}])
    replay = create_ticket(client, eid, key="k-1")
    assert replay.status_code == 200
    r = replay.json()
    assert r["already_committed"] is True
    assert r["ticket_id"] == first["ticket_id"]
    assert r["digest"] == first["digest"]
    assert r["snapshot"] == first["snapshot"]
    # 仍只有一张处置单
    lst = client.get(f"/exhibits/{eid}/illumination-tickets").json()
    assert len(lst["tickets"]) == 1


def test_create_same_key_different_target_409(env):
    client, _ = env
    eid, _ = stuck_exhibit(client)
    assert create_ticket(client, eid, key="k-2").status_code == 200
    # 换窗口 / 换阈值 / 换展品 → 409
    assert create_ticket(client, eid, key="k-2", end=BASE + 8 * 3600).status_code == 409
    assert create_ticket(client, eid, key="k-2",
                         min_stuck_seconds=900).status_code == 409
    eid2, _ = stuck_exhibit(client)
    assert create_ticket(client, eid2, key="k-2").status_code == 409


def test_create_via_alias_after_merge(env):
    client, _ = env
    eid, _ = stuck_exhibit(client, eid="EX-1")
    dup = make_exhibit(client, "DUP-1")
    prev = client.post("/merges/preview", json={"source_id": "DUP-1", "target_id": "EX-1"})
    assert prev.status_code == 200, prev.text
    commit = client.post("/merges/commit",
                         json={"token": prev.json()["token"], "resolutions": []})
    assert commit.status_code == 200, commit.text
    # 用别名创建：解析到规范档案
    resp = create_ticket(client, "DUP-1", key="alias-1")
    assert resp.status_code == 200, resp.text
    t = resp.json()
    assert t["requested_id"] == "DUP-1" and t["canonical_id"] == "EX-1"
    # 别名列表接口同样解析
    lst = client.get("/exhibits/DUP-1/illumination-tickets").json()
    assert lst["canonical_id"] == "EX-1" and len(lst["tickets"]) == 1
    # 用别名重放同一 request_key（解析到同一规范档案）→ 回放
    replay = create_ticket(client, "DUP-1", key="alias-1")
    assert replay.json()["already_committed"] is True


# ---------- 结案：accepted ----------

def test_close_accepted_keeps_events_and_note(env):
    client, _ = env
    eid, _ = stuck_exhibit(client)
    t = create_ticket(client, eid).json()
    resp = close_ticket(client, t["ticket_id"], t["digest"],
                        decision="accepted", note="光照平稳段属展柜正常工况，保留事件")
    assert resp.status_code == 200, resp.text
    c = resp.json()
    assert c["status"] == "accepted"
    assert c["decision"] == "accepted"
    assert c["close_note"] == "光照平稳段属展柜正常工况，保留事件"
    assert c["closed_at"] is not None
    assert c["closed_digest"] == t["digest"]
    assert c["evidence"] is None
    assert c["events_cleared"] is None and c["dose_exceeded"] is None
    # 快照保持不可变：事件仍在
    assert [e["type"] for e in c["snapshot"]["events"]] == ["stuck"]
    # 再次结案 → 409
    again = close_ticket(client, t["ticket_id"], t["digest"], decision="accepted")
    assert again.status_code == 409
    # GET 单张：结案后不再给出 current_digest
    got = client.get(f"/illumination-tickets/{t['ticket_id']}").json()
    assert got["status"] == "accepted"
    assert got["current_digest"] is None
    assert got["digest"] == t["digest"]


def test_close_accepted_rejects_evidence(env):
    client, _ = env
    eid, ingest = stuck_exhibit(client)
    t = create_ticket(client, eid).json()
    resp = close_ticket(client, t["ticket_id"], t["digest"], decision="accepted",
                        receipt_ids=[ingest["receipt"]["receipt_id"]])
    assert resp.status_code == 422


# ---------- 结案：resolved 举证 ----------

def test_close_resolved_requires_evidence(env):
    client, _ = env
    eid, _ = stuck_exhibit(client)
    t = create_ticket(client, eid).json()
    resp = close_ticket(client, t["ticket_id"], t["digest"], decision="resolved")
    assert resp.status_code == 422


def test_close_resolved_rejects_unknown_evidence_refs(env):
    client, _ = env
    eid, _ = stuck_exhibit(client)
    t = create_ticket(client, eid).json()
    r1 = close_ticket(client, t["ticket_id"], t["digest"], decision="resolved",
                      receipt_ids=["no-such-receipt"])
    assert r1.status_code == 422
    r2 = close_ticket(client, t["ticket_id"], t["digest"], decision="resolved",
                      correction_request_keys=["no-such-key"])
    assert r2.status_code == 422


def test_close_resolved_fails_while_events_remain(env):
    client, _ = env
    eid, ingest = stuck_exhibit(client)
    t = create_ticket(client, eid).json()
    # 未做任何处置，原 stuck 事件仍在 → 证明失败 409，且不写入
    resp = close_ticket(client, t["ticket_id"], t["digest"], decision="resolved",
                        receipt_ids=[ingest["receipt"]["receipt_id"]])
    assert resp.status_code == 409
    assert "未消失" in resp.json()["detail"]
    got = client.get(f"/illumination-tickets/{t['ticket_id']}").json()
    assert got["status"] == "open"
    assert got["closed_at"] is None


def test_close_resolved_after_correction(env):
    client, _ = env
    eid, ingest = stuck_exhibit(client)
    t = create_ticket(client, eid).json()
    assert [e["type"] for e in t["snapshot"]["events"]] == ["stuck"]

    # 处置：作废产生异常的批次（读数退出时间线）
    receipt_id = ingest["receipt"]["receipt_id"]
    fix = client.post(
        f"/exhibits/{eid}/receipts/corrections",
        json={"receipt_id": receipt_id, "request_key": "fix-stuck-1",
              "reason": "传感器卡死批次作废"},
    )
    assert fix.status_code == 200, fix.text

    # 作废改变了读数集合：旧 digest 已失效，结案前重新取 current_digest
    stale = close_ticket(client, t["ticket_id"], t["digest"], decision="resolved",
                         correction_request_keys=["fix-stuck-1"])
    assert stale.status_code == 409
    current = client.get(f"/illumination-tickets/{t['ticket_id']}").json()["current_digest"]
    assert current != t["digest"]

    resp = close_ticket(client, t["ticket_id"], current, decision="resolved",
                        note="卡死批次已作废，时间线重算正常",
                        correction_request_keys=["fix-stuck-1"])
    assert resp.status_code == 200, resp.text
    c = resp.json()
    assert c["status"] == "resolved"
    assert c["events_cleared"] is True
    assert c["dose_exceeded"] is False
    assert c["evidence"]["correction_request_keys"] == ["fix-stuck-1"]
    assert c["closed_digest"] == current
    # 创建快照不可变：原 stuck 事件仍固化在 snapshot 中
    assert [e["type"] for e in c["snapshot"]["events"]] == ["stuck"]


def test_close_resolved_with_receipt_evidence(env):
    client, _ = env
    eid, ingest = stuck_exhibit(client)
    t = create_ticket(client, eid).json()
    receipt_id = ingest["receipt"]["receipt_id"]
    fix = client.post(
        f"/exhibits/{eid}/receipts/corrections",
        json={"receipt_id": receipt_id, "request_key": "fix-stuck-2", "reason": "作废"},
    )
    assert fix.status_code == 200
    current = client.get(f"/illumination-tickets/{t['ticket_id']}").json()["current_digest"]
    # 也可以只引用涉及的收据
    resp = close_ticket(client, t["ticket_id"], current, decision="resolved",
                        note="批次已作废", receipt_ids=[receipt_id])
    assert resp.status_code == 200, resp.text
    assert resp.json()["evidence"]["receipt_ids"] == [receipt_id]


def test_close_resolved_fails_when_dose_still_exceeded(env):
    client, _ = env
    # 上限很低：jump 事件作废后消失，但剩余平稳读数的开放历史剂量仍越限。
    # min_stuck_seconds 抬高到 7200，避免 p1→p2 平稳段自身构成 stuck 事件。
    eid = make_exhibit(client, limit=50.0, interval=3600.0)
    set_calendar_24h(client, eid)
    send(client, eid, [
        {"id": "p1", "ts": iso(BASE), "lux": 100},
        {"id": "p2", "ts": iso(BASE + 3600), "lux": 100},
    ])
    out = send(client, eid, [
        {"id": "j1", "ts": iso(BASE + 3660), "lux": 900},   # 突变：速率超限
        {"id": "j2", "ts": iso(BASE + 7200), "lux": 100},
    ])
    t = create_ticket(client, eid, min_stuck_seconds=7200).json()
    assert [e["type"] for e in t["snapshot"]["events"]] == ["jump"]
    # 作废 j 批：jump 事件消失；p1→p2 段（1h × 100 lux = 100 lux·h > 50）仍越限
    fix = client.post(
        f"/exhibits/{eid}/receipts/corrections",
        json={"receipt_id": out["receipt"]["receipt_id"], "request_key": "fix-jump",
              "reason": "突变批次作废"},
    )
    assert fix.status_code == 200
    current = client.get(f"/illumination-tickets/{t['ticket_id']}").json()["current_digest"]
    resp = close_ticket(client, t["ticket_id"], current, decision="resolved",
                        correction_request_keys=["fix-jump"])
    assert resp.status_code == 409
    assert "越限" in resp.json()["detail"]
    # 未写入：仍为 open
    assert client.get(f"/illumination-tickets/{t['ticket_id']}").json()["status"] == "open"


# ---------- 结案：摘要核对（状态变化 → 409 不写入） ----------

def test_close_409_after_new_ingest(env):
    client, _ = env
    eid, _ = stuck_exhibit(client)
    t = create_ticket(client, eid).json()
    # 新上报：读数集合与收据链末端都变化 → 摘要不同
    send(client, eid, [{"id": "r9", "ts": iso(BASE + 5 * 3600), "lux": 120}])
    resp = close_ticket(client, t["ticket_id"], t["digest"], decision="accepted",
                        note="x")
    assert resp.status_code == 409
    assert client.get(f"/illumination-tickets/{t['ticket_id']}").json()["status"] == "open"


def test_close_409_after_correction_void(env):
    client, _ = env
    eid, ingest = stuck_exhibit(client)
    t = create_ticket(client, eid).json()
    fix = client.post(
        f"/exhibits/{eid}/receipts/corrections",
        json={"receipt_id": ingest["receipt"]["receipt_id"], "request_key": "void-1",
              "reason": "作废"},
    )
    assert fix.status_code == 200
    resp = close_ticket(client, t["ticket_id"], t["digest"], decision="accepted",
                        note="x")
    assert resp.status_code == 409
    assert client.get(f"/illumination-tickets/{t['ticket_id']}").json()["status"] == "open"


def test_close_409_after_calendar_change(env):
    client, _ = env
    eid, _ = stuck_exhibit(client)
    t = create_ticket(client, eid).json()
    # 改日历：指纹与 calendar=display 剂量口径都变化
    resp_cal = client.put(
        f"/exhibits/{eid}/calendar",
        json={"timezone": "UTC",
              "weekly": [{"weekday": d, "start": "09:00", "end": "17:00"}
                         for d in range(1, 8)]},
    )
    assert resp_cal.status_code == 200
    resp = close_ticket(client, t["ticket_id"], t["digest"], decision="accepted",
                        note="x")
    assert resp.status_code == 409
    assert client.get(f"/illumination-tickets/{t['ticket_id']}").json()["status"] == "open"


def test_close_409_after_source_priority_change(env):
    client, _ = env
    eid, _ = stuck_exhibit(client)
    t = create_ticket(client, eid).json()
    resp_sp = client.put(
        f"/exhibits/{eid}/source-priority",
        json={"effective_at": iso(BASE), "priorities": ["sensor-a"],
              "expected_version": 0},
    )
    assert resp_sp.status_code == 201
    resp = close_ticket(client, t["ticket_id"], t["digest"], decision="accepted",
                        note="x")
    assert resp.status_code == 409
    assert client.get(f"/illumination-tickets/{t['ticket_id']}").json()["status"] == "open"


def test_close_409_after_calendar_deleted(env):
    client, _ = env
    eid, _ = stuck_exhibit(client)
    t = create_ticket(client, eid).json()
    assert client.delete(f"/exhibits/{eid}/calendar").status_code == 204
    # 日历删除后无法重算：GET 的 current_digest 为 null，结案 409
    got = client.get(f"/illumination-tickets/{t['ticket_id']}").json()
    assert got["current_digest"] is None
    resp = close_ticket(client, t["ticket_id"], t["digest"], decision="accepted",
                        note="x")
    assert resp.status_code == 409


def test_close_unknown_ticket_404(env):
    client, _ = env
    resp = close_ticket(client, "no-such-ticket", "0" * 64, decision="accepted")
    assert resp.status_code == 404
    assert client.get("/illumination-tickets/no-such-ticket").status_code == 404


def test_close_with_garbage_digest_409(env):
    client, _ = env
    eid, _ = stuck_exhibit(client)
    t = create_ticket(client, eid).json()
    resp = close_ticket(client, t["ticket_id"], "f" * 64, decision="accepted",
                        note="x")
    assert resp.status_code == 409


# ---------- 并发 ----------

def test_concurrent_create_same_request_key_single_ticket(env):
    client, _ = env
    eid, _ = stuck_exhibit(client)
    ids: list[str] = []
    replays: list[bool] = []

    def post(_: int) -> None:
        r = create_ticket(client, eid, key="conc-key")
        assert r.status_code == 200
        body = r.json()
        ids.append(body["ticket_id"])
        replays.append(body["already_committed"])

    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(post, range(8)))
    assert len(set(ids)) == 1
    assert replays.count(False) == 1 and replays.count(True) == 7
    lst = client.get(f"/exhibits/{eid}/illumination-tickets").json()
    assert len(lst["tickets"]) == 1


def test_concurrent_close_only_one_succeeds(env):
    client, _ = env
    eid, _ = stuck_exhibit(client)
    t = create_ticket(client, eid).json()
    digest = t["digest"]
    statuses: list[int] = []

    def close(i: int) -> None:
        r = close_ticket(client, t["ticket_id"], digest, decision="accepted",
                         note=f"n{i}")
        statuses.append(r.status_code)

    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(close, range(8)))
    assert sorted(statuses) == [200] + [409] * 7
    got = client.get(f"/illumination-tickets/{t['ticket_id']}").json()
    assert got["status"] == "accepted"


# ---------- 列表与既有接口不受影响 ----------

def test_list_tickets_and_existing_endpoints_untouched(env):
    client, _ = env
    eid, _ = stuck_exhibit(client)
    t1 = create_ticket(client, eid, key="L1").json()
    t2 = create_ticket(client, eid, key="L2", end=BASE + 8 * 3600).json()
    lst = client.get(f"/exhibits/{eid}/illumination-tickets").json()
    assert lst["requested_id"] == eid and lst["canonical_id"] == eid
    assert {x["ticket_id"] for x in lst["tickets"]} == {t1["ticket_id"], t2["ticket_id"]}
    # 既有接口行为不变：诊断与剂量口径与快照一致
    diag = client.post(
        f"/exhibits/{eid}/readings/diagnose",
        json={"start": iso(BASE), "end": iso(BASE + 6 * 3600),
              "max_rate_lux_per_second": 1.0, "stuck_tolerance_lux": 2.0,
              "min_stuck_seconds": 1800},
    ).json()
    assert [e["type"] for e in diag["events"]] == ["stuck"]
    dose = client.get(
        f"/exhibits/{eid}/dose",
        params={"start": iso(BASE), "end": iso(BASE + 6 * 3600), "calendar": "display"},
    ).json()
    assert dose["known_dose_luxh"] == pytest.approx(300.0)
