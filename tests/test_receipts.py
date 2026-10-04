"""照度上报存证：收据生成、链哈希、整链校验、并发顺序、归并保留链。"""
from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from app.receipts import GENESIS_PREV_HASH, chain_hash_of, content_hash_of

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


def tamper(db_file, sql: str, params: tuple = ()) -> None:
    """直接改库模拟篡改/破坏（绕过 API）。"""
    conn = sqlite3.connect(str(db_file))
    try:
        conn.execute(sql, params)
        conn.commit()
    finally:
        conn.close()


def edit_payload(db_file, chain_id: str, seq: int, mutate: Callable[[dict], None]) -> None:
    """读出第 seq 张收据内容，用 mutate 篡改后写回（不重算哈希）。"""
    conn = sqlite3.connect(str(db_file))
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute(
            "SELECT payload_json FROM ingest_receipts WHERE chain_id = ? AND seq = ?",
            (chain_id, seq),
        ).fetchone()
        assert row is not None
        payload = json.loads(row["payload_json"])
        mutate(payload)
        conn.execute(
            "UPDATE ingest_receipts SET payload_json = ? WHERE chain_id = ? AND seq = ?",
            (json.dumps(payload, ensure_ascii=False, sort_keys=True), chain_id, seq),
        )
        conn.commit()
    finally:
        conn.close()


def three_receipt_chain(client: TestClient) -> str:
    eid = make_exhibit(client)
    for i in range(3):
        send(client, eid, [{"id": f"r{i}", "ts": iso(BASE + i), "lux": 10 + i}])
    return eid


# ---------- 收据生成与内容 ----------

def test_receipt_minted_in_same_transaction_and_hash_recomputable(env):
    client, _ = env
    eid = make_exhibit(client)
    out = send(client, eid, [
        {"id": "r2", "ts": iso(BASE + 3600), "lux": 100},
        {"id": "r1", "ts": iso(BASE), "lux": 100},
    ])
    receipt = out["receipt"]
    assert receipt["chain_id"] == eid
    assert receipt["seq"] == 1
    assert receipt["prev_hash"] == GENESIS_PREV_HASH
    assert len(receipt["content_hash"]) == 64
    assert len(receipt["chain_hash"]) == 64

    content = receipt["content"]
    assert content["chain_id"] == eid
    assert content["seq"] == 1
    assert content["receipt_id"] == receipt["receipt_id"]
    assert content["requested_id"] == eid
    # 请求内容按提交顺序逐条记录，ts 规范化为 Z 结尾
    assert [r["id"] for r in content["request"]["readings"]] == ["r2", "r1"]
    assert content["request"]["readings"][0]["ts"].endswith("Z")
    assert content["request"]["readings"][0]["lux"] == 100.0
    # 逐条结果与上报响应一致
    assert content["outcome"]["inserted"] == 2
    assert content["outcome"]["duplicates"] == 0
    assert content["outcome"]["conflicts"] == 0
    assert content["outcome"]["results"] == [
        {"id": "r2", "status": "inserted"},
        {"id": "r1", "status": "inserted"},
    ]
    # 哈希可由内容重算复现
    assert receipt["content_hash"] == content_hash_of(content)
    assert receipt["chain_hash"] == chain_hash_of(
        receipt["prev_hash"], receipt["content_hash"]
    )


def test_receipt_records_duplicate_and_conflict_results(env):
    client, _ = env
    eid = make_exhibit(client)
    first = send(client, eid, [{"id": "r1", "ts": iso(BASE), "lux": 100}])
    out = send(client, eid, [
        {"id": "r1", "ts": iso(BASE), "lux": 100},      # duplicate：同 ID 同数据
        {"id": "r1", "ts": iso(BASE), "lux": 999},      # conflict：同 ID 不同数据
        {"id": "r2", "ts": iso(BASE + 60), "lux": 50},  # inserted
    ])
    assert (out["inserted"], out["duplicates"], out["conflicts"]) == (1, 1, 1)

    receipt = out["receipt"]
    assert receipt["seq"] == 2
    assert receipt["prev_hash"] == first["receipt"]["chain_hash"]
    outcome = receipt["content"]["outcome"]
    assert (outcome["inserted"], outcome["duplicates"], outcome["conflicts"]) == (1, 1, 1)
    assert [r["status"] for r in outcome["results"]] == [
        "duplicate", "conflict", "inserted",
    ]


def test_request_content_normalized_regardless_of_ts_format(env):
    """同一逻辑请求（时区写法不同）规范化为相同的请求内容。"""
    client, _ = env
    a = make_exhibit(client, eid="a")
    b = make_exhibit(client, eid="b")
    ra = send(client, a, [{"id": "r1", "ts": "2026-09-20T08:00:00+00:00", "lux": 100}])
    rb = send(client, b, [{"id": "r1", "ts": "2026-09-20T08:00:00Z", "lux": 100}])
    assert ra["receipt"]["content"]["request"] == rb["receipt"]["content"]["request"]
    reading = ra["receipt"]["content"]["request"]["readings"][0]
    assert reading["ts"] == "2026-09-20T08:00:00Z"


# ---------- 失败事务不留收据 ----------

def test_ingest_to_unknown_exhibit_leaves_no_receipt(env):
    client, _ = env
    resp = client.post("/exhibits/ghost/readings",
                       json={"readings": [{"id": "r1", "ts": iso(BASE), "lux": 1}]})
    assert resp.status_code == 404
    # 链不存在：按链标识查询/校验均 404
    assert client.get("/receipts/chains/ghost").status_code == 404
    assert client.get("/receipts/chains/ghost/verify").status_code == 404
    # 同名展品随后建档：首张收据 seq 仍为 1（失败事务没有留下收据）
    eid = make_exhibit(client, eid="ghost")
    out = send(client, eid, [{"id": "r1", "ts": iso(BASE), "lux": 1}])
    assert out["receipt"]["seq"] == 1


def test_mid_transaction_failure_rolls_back_readings_and_receipt(env, monkeypatch):
    """收据生成失败时整个事务回滚：读数与收据都不留。"""
    client, _ = env
    eid = make_exhibit(client)

    def boom(*args, **kwargs):
        raise RuntimeError("模拟收据生成失败")

    monkeypatch.setattr("app.main.mint_receipt", boom)
    with pytest.raises(RuntimeError):
        client.post(f"/exhibits/{eid}/readings",
                    json={"readings": [{"id": "r1", "ts": iso(BASE), "lux": 1}]})
    assert client.get(f"/exhibits/{eid}/readings").json()["readings"] == []
    assert client.get(f"/exhibits/{eid}/receipts").json()["receipts"] == []


# ---------- 收据查询 ----------

def test_list_and_get_receipts(env):
    client, _ = env
    eid = make_exhibit(client)
    send(client, eid, [{"id": "a", "ts": iso(BASE), "lux": 1}])
    send(client, eid, [{"id": "b", "ts": iso(BASE + 1), "lux": 2}])

    lst = client.get(f"/exhibits/{eid}/receipts").json()
    assert lst["requested_id"] == lst["canonical_id"] == lst["chain_id"] == eid
    assert [r["seq"] for r in lst["receipts"]] == [1, 2]
    assert lst["receipts"][1]["prev_hash"] == lst["receipts"][0]["chain_hash"]

    one = client.get(f"/exhibits/{eid}/receipts/2").json()
    assert one["seq"] == 2
    assert one["content"]["request"]["readings"][0]["id"] == "b"
    assert client.get(f"/exhibits/{eid}/receipts/99").status_code == 404

    # 无上报的展品：空链
    other = make_exhibit(client)
    empty = client.get(f"/exhibits/{other}/receipts").json()
    assert empty["receipts"] == []


# ---------- 整链校验 ----------

def test_verify_ok_chain(env):
    client, _ = env
    eid = three_receipt_chain(client)
    v = client.get(f"/exhibits/{eid}/receipts/verify").json()
    assert v == {
        "chain_id": eid, "ok": True, "checked": 3, "total": 3, "first_failure": None,
    }
    # 空链视为通过（checked = 0）
    other = make_exhibit(client)
    v2 = client.get(f"/exhibits/{other}/receipts/verify").json()
    assert v2["ok"] is True and v2["checked"] == 0 and v2["total"] == 0


def test_verify_detects_content_tamper(env):
    client, db_file = env
    eid = three_receipt_chain(client)
    edit_payload(db_file, eid, 2,
                 lambda p: p["request"]["readings"][0].update(lux=999.0))
    v = client.get(f"/exhibits/{eid}/receipts/verify").json()
    assert v["ok"] is False
    assert v["checked"] == 1
    assert v["total"] == 3
    assert v["first_failure"]["seq"] == 2
    assert v["first_failure"]["reason"] == "content_hash_mismatch"
    # 按链标识直查同样报告断点
    v2 = client.get(f"/receipts/chains/{eid}/verify").json()
    assert v2["ok"] is False and v2["first_failure"]["seq"] == 2


def test_verify_detects_chain_hash_tamper(env):
    client, db_file = env
    eid = three_receipt_chain(client)
    tamper(db_file,
           "UPDATE ingest_receipts SET chain_hash = ? WHERE chain_id = ? AND seq = 2",
           ("f" * 64, eid))
    v = client.get(f"/exhibits/{eid}/receipts/verify").json()
    assert v["ok"] is False
    assert v["checked"] == 1
    assert v["first_failure"]["seq"] == 2
    assert v["first_failure"]["reason"] == "chain_hash_mismatch"


def test_verify_detects_prev_hash_tamper(env):
    client, db_file = env
    eid = three_receipt_chain(client)
    tamper(db_file,
           "UPDATE ingest_receipts SET prev_hash = ? WHERE chain_id = ? AND seq = 2",
           ("1" * 64, eid))
    v = client.get(f"/exhibits/{eid}/receipts/verify").json()
    assert v["ok"] is False
    assert v["checked"] == 1
    assert v["first_failure"]["seq"] == 2
    assert v["first_failure"]["reason"] == "prev_hash_mismatch"


def test_verify_detects_seq_gap(env):
    client, db_file = env
    eid = three_receipt_chain(client)
    tamper(db_file, "DELETE FROM ingest_receipts WHERE chain_id = ? AND seq = 2", (eid,))
    v = client.get(f"/exhibits/{eid}/receipts/verify").json()
    assert v["ok"] is False
    assert v["checked"] == 1
    assert v["total"] == 2
    assert v["first_failure"]["seq"] == 2
    assert v["first_failure"]["reason"] == "seq_gap"


# ---------- 并发上报：唯一连续顺序 ----------

def test_concurrent_ingests_form_unique_contiguous_chain(env):
    client, _ = env
    eid = make_exhibit(client)

    def post(i: int) -> int:
        resp = client.post(
            f"/exhibits/{eid}/readings",
            json={"readings": [{"id": f"t{i}", "ts": iso(BASE + i), "lux": 1}]},
        )
        assert resp.status_code == 200, resp.text
        return resp.json()["receipt"]["seq"]

    with ThreadPoolExecutor(max_workers=8) as pool:
        seqs = list(pool.map(post, range(16)))
    # 16 个并发上报恰好占满 1..16，无重复、无缺口
    assert sorted(seqs) == list(range(1, 17))
    v = client.get(f"/exhibits/{eid}/receipts/verify").json()
    assert v["ok"] is True and v["checked"] == 16 and v["total"] == 16
    readings = client.get(f"/exhibits/{eid}/readings").json()["readings"]
    assert len(readings) == 16


# ---------- 归并与收据链 ----------

def preview(client: TestClient, source: str, target: str) -> dict:
    resp = client.post("/merges/preview", json={"source_id": source, "target_id": target})
    assert resp.status_code == 200, resp.text
    return resp.json()


def test_merge_preserves_both_chains_and_new_ingests_join_target_chain(env):
    client, _ = env
    src = make_exhibit(client, eid="src")
    tgt = make_exhibit(client, eid="tgt")
    send(client, src, [{"id": "s1", "ts": iso(BASE), "lux": 10}])
    send(client, src, [{"id": "s2", "ts": iso(BASE + 1), "lux": 10}])
    send(client, tgt, [{"id": "t1", "ts": iso(BASE + 2), "lux": 10}])
    src_hashes = [r["chain_hash"] for r in client.get("/exhibits/src/receipts").json()["receipts"]]
    tgt_hashes = [r["chain_hash"] for r in client.get("/exhibits/tgt/receipts").json()["receipts"]]

    token = preview(client, "src", "tgt")["token"]
    out = client.post("/merges/commit", json={"token": token, "resolutions": []})
    assert out.status_code == 200, out.text
    assert out.json()["receipt_chains"] == ["src", "tgt"]

    # 两条既有链原样保留、分别校验通过；旧哈希未被重写
    v_src = client.get("/receipts/chains/src/verify").json()
    v_tgt = client.get("/receipts/chains/tgt/verify").json()
    assert v_src["ok"] is True and v_src["checked"] == 2
    assert v_tgt["ok"] is True and v_tgt["checked"] == 1
    src_chain = client.get("/receipts/chains/src").json()["receipts"]
    tgt_chain = client.get("/receipts/chains/tgt").json()["receipts"]
    assert [r["chain_hash"] for r in src_chain] == src_hashes
    assert [r["chain_hash"] for r in tgt_chain] == tgt_hashes

    # 归并后经来源别名上报：继续接入目标原链（seq 递增、prev_hash 衔接目标链尾）
    ing = send(client, "src", [{"id": "s3", "ts": iso(BASE + 3), "lux": 10}])
    assert ing["canonical_id"] == "tgt"
    assert ing["receipt"]["chain_id"] == "tgt"
    assert ing["receipt"]["seq"] == 2
    assert ing["receipt"]["prev_hash"] == tgt_hashes[-1]
    assert ing["receipt"]["content"]["requested_id"] == "src"

    # 来源链不再增长；目标链整链校验通过
    assert len(client.get("/receipts/chains/src").json()["receipts"]) == 2
    v = client.get("/receipts/chains/tgt/verify").json()
    assert v["ok"] is True and v["checked"] == 2

    # 展品维度的收据查询解析别名：经来源别名看到的是目标链
    lst = client.get("/exhibits/src/receipts").json()
    assert lst["requested_id"] == "src"
    assert lst["canonical_id"] == lst["chain_id"] == "tgt"
    assert [r["seq"] for r in lst["receipts"]] == [1, 2]

    # 同令牌重放：receipt_chains 一并回放
    again = client.post("/merges/commit", json={"token": token, "resolutions": []})
    assert again.status_code == 200
    assert again.json()["already_committed"] is True
    assert again.json()["receipt_chains"] == ["src", "tgt"]


def test_merge_rejected_when_receipt_chain_broken(env):
    client, db_file = env
    src = make_exhibit(client, eid="src")
    tgt = make_exhibit(client, eid="tgt")
    send(client, src, [{"id": "s1", "ts": iso(BASE), "lux": 10}])
    send(client, tgt, [{"id": "t1", "ts": iso(BASE + 1), "lux": 10}])
    # 篡改来源链第 1 张收据的链哈希
    tamper(db_file,
           "UPDATE ingest_receipts SET chain_hash = ? WHERE chain_id = ? AND seq = 1",
           ("0" * 64, src))

    token = preview(client, "src", "tgt")["token"]
    resp = client.post("/merges/commit", json={"token": token, "resolutions": []})
    assert resp.status_code == 409
    assert "收据链" in resp.json()["detail"]
    # 无部分变更：来源档案与读数仍在，来源 ID 未成为别名
    assert client.get("/exhibits/src").json()["canonical_id"] == "src"
    assert [r["id"] for r in client.get("/exhibits/src/readings").json()["readings"]] == ["s1"]
    # 目标链不受影响，仍可正常上报
    ing = send(client, "tgt", [{"id": "t2", "ts": iso(BASE + 2), "lux": 10}])
    assert ing["receipt"]["seq"] == 2
