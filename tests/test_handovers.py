"""跨盘点轮次馆藏区域交接：证据核验、链续接、单事务并发、只增不改、归并联动。"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient

BASE = 1_700_000_000  # 2023-11-14T22:13:20Z


def iso(epoch: float) -> str:
    from datetime import datetime, timezone

    return datetime.fromtimestamp(epoch, tz=timezone.utc).isoformat()


@pytest.fixture()
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("DB_PATH", str(tmp_path / "test.db"))
    from app.main import app

    with TestClient(app) as c:
        yield c


def make_exhibit(client: TestClient, eid: str) -> None:
    r = client.post("/exhibits", json={
        "id": eid, "name": eid,
        "dose_limit_luxh": 1000.0, "max_interval_seconds": 3600.0,
    })
    assert r.status_code == 201, r.text


def round_body(start: float, expected: list[tuple[str, str]],
               events: list[tuple[str, str, float]] | None = None,
               delta: float = 5) -> dict:
    if events is None:
        events = [(e, a, start + 5) for e, a in expected]
    return {
        "start_at": iso(start),
        "window_seconds": 60,
        "rssi_delta_threshold": delta,
        "expected": [{"exhibit_id": e, "area": a} for e, a in expected],
        "events": [
            {"exhibit_id": e, "area": a, "ts": iso(t), "rssi": -50}
            for e, a, t in events
        ],
    }


def reconcile(client: TestClient, round_id: str, body: dict) -> dict:
    r = client.post(f"/inventory-rounds/{round_id}/reconcile", json=body)
    assert r.status_code == 200, r.text
    return r.json()


def handover(client: TestClient, eid: str, body: dict, expected_status: int = 201) -> dict:
    r = client.post(f"/exhibits/{eid}/area-handovers", json=body)
    assert r.status_code == expected_status, r.text
    return r.json()


def chain(client: TestClient, eid: str, expected_status: int = 200) -> dict:
    r = client.get(f"/exhibits/{eid}/area-handovers")
    assert r.status_code == expected_status, r.text
    return r.json()


def ho(eid: str, src_round: str, src_area: str, tgt_round: str, tgt_area: str) -> dict:
    return {
        "exhibit_id": eid,
        "source_round_id": src_round,
        "target_round_id": tgt_round,
        "source_area": src_area,
        "target_area": tgt_area,
    }


# ---------- 基本交接 ----------

def test_first_handover_builds_chain_and_query_current_area(env):
    client = env
    make_exhibit(client, "vase")
    r1 = reconcile(client, "r1", round_body(BASE, [("vase", "A")]))
    r2 = reconcile(client, "r2", round_body(BASE + 86400, [("vase", "B")]))

    out = handover(client, "vase", ho("vase", "r1", "A", "r2", "B"))
    assert out["seq"] == 1
    assert out["canonical_id"] == "vase"
    assert out["requested_id"] == "vase"
    assert out["source_round_id"] == "r1" and out["target_round_id"] == "r2"
    assert out["source_area"] == "A" and out["target_area"] == "B"
    assert out["source_round_start_at"] == r1["start_at"]
    assert out["target_round_start_at"] == r2["start_at"]
    assert out["source_round_digest"] == r1["request_digest"]
    assert out["target_round_digest"] == r2["request_digest"]

    got = chain(client, "vase")
    assert got["current_round_id"] == "r2"
    assert got["current_area"] == "B"
    assert len(got["history"]) == 1
    assert got["history"][0]["seq"] == 1


def test_empty_history_before_any_handover(env):
    client = env
    make_exhibit(client, "vase")
    got = chain(client, "vase")
    assert got["current_area"] is None and got["current_round_id"] is None
    assert got["history"] == []


def test_chain_query_unknown_exhibit_404(env):
    assert chain(env, "ghost", expected_status=404)


# ---------- 证据核验失败均 409/422，不留记录 ----------

def test_unreconciled_round_rejected(env):
    client = env
    make_exhibit(client, "vase")
    reconcile(client, "r1", round_body(BASE, [("vase", "A")]))
    # r2 未固化 → 404
    handover(client, "vase", ho("vase", "r1", "A", "r2", "B"), expected_status=404)
    # r1 也不存在的情形
    handover(client, "vase", ho("vase", "x", "A", "r1", "A"), expected_status=404)
    assert chain(client, "vase")["history"] == []


def test_unknown_exhibit_handover_404(env):
    client = env
    reconcile(client, "r1", round_body(BASE, []))
    reconcile(client, "r2", round_body(BASE + 10, []))
    handover(client, "ghost", ho("ghost", "r1", "A", "r2", "B"),
             expected_status=404)


def test_rounds_must_be_strictly_increasing(env):
    client = env
    make_exhibit(client, "vase")
    # 同轮
    reconcile(client, "r1", round_body(BASE, [("vase", "A")]))
    handover(client, "vase", ho("vase", "r1", "A", "r1", "A"),
             expected_status=422)
    # 起始时间更早的轮作为接收方 → 422
    reconcile(client, "r0", round_body(BASE - 100, [("vase", "A")]))
    handover(client, "vase", ho("vase", "r1", "A", "r0", "A"),
             expected_status=422)
    # 起始时间相同（不同 round_id、同一 start_at）→ 422
    reconcile(client, "r1b", round_body(BASE, [("vase", "A")]))
    handover(client, "vase", ho("vase", "r1", "A", "r1b", "A"),
             expected_status=422)
    assert chain(client, "vase")["history"] == []


def test_missing_misplaced_unknown_position_block_handover(env):
    client = env
    make_exhibit(client, "vase")

    # 接收轮缺失：预期 A 但无事件
    reconcile(client, "r1", round_body(BASE, [("vase", "A")]))
    reconcile(client, "r-missing", round_body(BASE + 100, [("vase", "A")], events=[]))
    handover(client, "vase", ho("vase", "r1", "A", "r-missing", "A"),
             expected_status=409)

    # 接收轮错位：实际定位 B
    reconcile(client, "r-mis", round_body(
        BASE + 200, [("vase", "A")], events=[("vase", "B", BASE + 205)]))
    handover(client, "vase", ho("vase", "r1", "A", "r-mis", "B"),
             expected_status=409)
    # 即使按预期区域 A 上报也不行（该轮没有明确定位在 A）
    handover(client, "vase", ho("vase", "r1", "A", "r-mis", "A"),
             expected_status=409)

    # 接收轮位置不明：最后窗口 A/B 信号差不足阈值（差 3 < 阈值 5）
    reconcile(client, "r-unk", round_body(
        BASE + 300, [("vase", "A")],
        events=[("vase", "A", BASE + 305), ("vase", "B", BASE + 305)],
        delta=5))
    # 两事件同窗口默认同 RSSI=-50，差 0 < 5 → 位置不明
    handover(client, "vase", ho("vase", "r1", "A", "r-unk", "A"),
             expected_status=409)

    # 未列入接收轮预期（意外出现）→ 409
    reconcile(client, "r-unexp", round_body(
        BASE + 400, [], events=[("vase", "B", BASE + 405)]))
    handover(client, "vase", ho("vase", "r1", "A", "r-unexp", "B"),
             expected_status=409)

    # 来源轮同样核验：缺失的来源轮不得建链
    reconcile(client, "s-missing", round_body(BASE - 500, [("vase", "A")], events=[]))
    handover(client, "vase", ho("vase", "s-missing", "A", "r1", "A"),
             expected_status=409)

    assert chain(client, "vase")["history"] == []


def test_reported_area_must_match_located_area(env):
    client = env
    make_exhibit(client, "vase")
    reconcile(client, "r1", round_body(BASE, [("vase", "A")]))
    reconcile(client, "r2", round_body(BASE + 100, [("vase", "B")]))
    # 区域报错（实际是 B，报成 C）→ 409
    handover(client, "vase", ho("vase", "r1", "A", "r2", "C"),
             expected_status=409)
    handover(client, "vase", ho("vase", "r1", "X", "r2", "B"),
             expected_status=409)
    assert chain(client, "vase")["history"] == []


def test_path_and_body_exhibit_must_match(env):
    client = env
    make_exhibit(client, "a")
    make_exhibit(client, "b")
    reconcile(client, "r1", round_body(BASE, [("a", "A"), ("b", "A")]))
    reconcile(client, "r2", round_body(BASE + 100, [("a", "B"), ("b", "B")]))
    body = ho("b", "r1", "A", "r2", "B")
    handover(client, "a", body, expected_status=422)


# ---------- 续接规则 ----------

def _build_three_rounds(client: TestClient, eid: str = "vase") -> None:
    make_exhibit(client, eid)
    reconcile(client, "r1", round_body(BASE, [(eid, "A")]))
    reconcile(client, "r2", round_body(BASE + 100, [(eid, "B")]))
    reconcile(client, "r3", round_body(BASE + 200, [(eid, "C")]))


def test_chain_only_continues_from_last_receiver(env):
    client = env
    _build_three_rounds(client)
    handover(client, "vase", ho("vase", "r1", "A", "r2", "B"))

    # 正常续接到 r3
    out = handover(client, "vase", ho("vase", "r2", "B", "r3", "C"))
    assert out["seq"] == 2

    got = chain(client, "vase")
    assert got["current_area"] == "C" and got["current_round_id"] == "r3"
    assert [h["seq"] for h in got["history"]] == [1, 2]
    assert [(h["source_round_id"], h["target_round_id"]) for h in got["history"]] == [
        ("r1", "r2"), ("r2", "r3"),
    ]


def test_old_round_cannot_backfill_current_destination(env):
    client = env
    _build_three_rounds(client)
    handover(client, "vase", ho("vase", "r1", "A", "r2", "B"))
    # 跳过 r3 或从旧轮 r1 重新发起都拒绝
    handover(client, "vase", ho("vase", "r1", "A", "r3", "C"),
             expected_status=409)
    # 来源轮正确但来源区域不匹配（当前去向是 B）
    handover(client, "vase", ho("vase", "r2", "X", "r3", "C"),
             expected_status=409)
    # 当前去向未被覆盖
    got = chain(client, "vase")
    assert got["current_area"] == "B"
    assert len(got["history"]) == 1


def test_cannot_reuse_round_already_on_chain(env):
    client = env
    _build_three_rounds(client)
    handover(client, "vase", ho("vase", "r1", "A", "r2", "B"))
    handover(client, "vase", ho("vase", "r2", "B", "r3", "C"))
    reconcile(client, "r4", round_body(BASE + 300, [("vase", "A")]))
    # 已作为链上来源轮的旧轮不能再次作为来源
    handover(client, "vase", ho("vase", "r2", "B", "r4", "A"),
             expected_status=409)
    # 已在链上的接收轮 r3 也不能被新一笔重复占用（并发兜底索引）
    handover(client, "vase", ho("vase", "r3", "C", "r2", "B"),
             expected_status=422)  # 时间不递增先挡下
    assert len(chain(client, "vase")["history"]) == 2


# ---------- 并发：最多一笔生效 ----------

def test_concurrent_handovers_only_one_takes_effect(env):
    client = env
    _build_three_rounds(client)

    def post(kind: str) -> int:
        if kind == "same":
            body = ho("vase", "r1", "A", "r2", "B")
        else:  # 不同展品的交接不竞争，但本展品并发同位置续接
            body = ho("vase", "r1", "A", "r2", "B")
        return client.post("/exhibits/vase/area-handovers", json=body).status_code

    with ThreadPoolExecutor(max_workers=8) as pool:
        statuses = list(pool.map(post, ["same"] * 10))
    # 首次交接位置竞争：恰好一笔 201，其余 409（非幂等操作，无重放）
    assert statuses.count(201) == 1
    assert statuses.count(409) == 9
    rows = chain(client, "vase")["history"]
    assert len(rows) == 1
    assert rows[0]["target_round_id"] == "r2"


def test_concurrent_continuation_races(env):
    client = env
    _build_three_rounds(client)
    handover(client, "vase", ho("vase", "r1", "A", "r2", "B"))
    # r3 与 r4 都想从 r2/B 续接：只有一笔赢
    reconcile(client, "r4", round_body(BASE + 300, [("vase", "D")]))

    def post(round_id_area: tuple[str, str]) -> int:
        tgt_round, area = round_id_area
        return client.post("/exhibits/vase/area-handovers",
                           json=ho("vase", "r2", "B", tgt_round, area)).status_code

    with ThreadPoolExecutor(max_workers=8) as pool:
        statuses = list(pool.map(post, [("r3", "C"), ("r4", "D")] * 6))
    assert statuses.count(201) == 1
    history = chain(client, "vase")["history"]
    assert len(history) == 2
    # 输家随后以上一笔接收轮/区域续接仍可成功（不留下死链）：
    # 若 r3 胜出则续接 r4，若 r4 胜出（时间更晚）则 r3 已过期、只能接新轮
    winner_target = history[-1]["target_round_id"]
    winner_area = history[-1]["target_area"]
    if winner_target == "r3":
        out = handover(client, "vase", ho("vase", "r3", "C", "r4", "D"))
        assert out["seq"] == 3
        assert chain(client, "vase")["current_area"] == "D"
    else:
        # r4 胜出：不能再回填到更早的 r3
        handover(client, "vase", ho("vase", "r4", "D", "r3", "C"),
                 expected_status=422)


# ---------- 别名 ----------

def test_handover_accepts_merge_alias(env):
    client = env
    make_exhibit(client, "src")
    make_exhibit(client, "tgt")
    # src 归并进 tgt（双方都无读数碰撞、无交接链）
    token = client.post("/merges/preview",
                        json={"source_id": "src", "target_id": "tgt"}).json()["token"]
    committed = client.post("/merges/commit", json={"token": token, "resolutions": []})
    assert committed.status_code == 200, committed.text

    reconcile(client, "r1", round_body(BASE, [("src", "A")]))
    reconcile(client, "r2", round_body(BASE + 100, [("tgt", "B")]))
    # 用旧别名 src 提交与查询
    out = handover(client, "src", ho("src", "r1", "A", "r2", "B"))
    assert out["canonical_id"] == "tgt"
    assert out["requested_id"] == "src"
    got = chain(client, "src")
    assert got["canonical_id"] == "tgt" and got["current_area"] == "B"
    assert chain(client, "tgt")["history"] == got["history"]


# ---------- 跨归并续接：旧轮固化证据按当前别名关系解释 ----------

def _merge(client: TestClient, src: str, tgt: str) -> dict:
    token = client.post("/merges/preview",
                        json={"source_id": src, "target_id": tgt}).json()["token"]
    r = client.post("/merges/commit", json={"token": token, "resolutions": []})
    assert r.status_code == 200, r.text
    return r.json()


def test_continuation_after_merge_interprets_old_source_round_via_alias(env):
    """来源档案已有交接链并归并到无链目标：沿用归并前固化的接收轮作为
    下一笔来源轮，用目标 ID 或来源别名提交都应续接成功。"""
    client = env
    make_exhibit(client, "src")
    make_exhibit(client, "tgt")
    reconcile(client, "r1", round_body(BASE, [("src", "A")]))
    reconcile(client, "r2", round_body(BASE + 100, [("src", "B")]))
    handover(client, "src", ho("src", "r1", "A", "r2", "B"))

    committed = _merge(client, "src", "tgt")
    assert committed["handover_chain_carried"] is True

    # 归并后的新轮以目标 ID 固化
    reconcile(client, "r3", round_body(BASE + 200, [("tgt", "C")]))

    # 用目标 ID 提交：来源轮 r2 的固化证据里只有旧身份 src（别名）
    out = handover(client, "tgt", ho("tgt", "r2", "B", "r3", "C"))
    assert out["seq"] == 2
    assert out["canonical_id"] == "tgt" and out["requested_id"] == "tgt"
    # 证据沿用归并前固化的轮次摘要，不改写盘点结果与旧交接记录
    r2 = client.get("/inventory-rounds/r2").json()
    assert out["source_round_digest"] == r2["request_digest"]
    assert {e["canonical_id"] for e in r2["expectations"]} == {"src"}

    got = chain(client, "tgt")
    assert got["current_round_id"] == "r3" and got["current_area"] == "C"
    assert [(h["seq"], h["source_round_id"], h["target_round_id"])
            for h in got["history"]] == [(1, "r1", "r2"), (2, "r2", "r3")]

    # 再续接一笔，用来源别名提交，来源轮 r3 的固化身份是 tgt（规范 ID）
    reconcile(client, "r4", round_body(BASE + 300, [("tgt", "D")]))
    out = handover(client, "src", ho("src", "r3", "C", "r4", "D"))
    assert out["seq"] == 3
    assert out["canonical_id"] == "tgt" and out["requested_id"] == "src"
    assert chain(client, "src")["current_area"] == "D"


def test_chain_tip_round_and_area_must_match_across_merge(env):
    """跨归并续接仍须与链尖（上一笔接收轮/区域）完全一致。"""
    client = env
    make_exhibit(client, "src")
    make_exhibit(client, "tgt")
    reconcile(client, "r1", round_body(BASE, [("src", "A")]))
    reconcile(client, "r2", round_body(BASE + 100, [("src", "B")]))
    handover(client, "src", ho("src", "r1", "A", "r2", "B"))
    _merge(client, "src", "tgt")
    reconcile(client, "r3", round_body(BASE + 200, [("tgt", "C")]))

    # 从链尖之外的旧轮回填 → 409
    handover(client, "tgt", ho("tgt", "r1", "A", "r3", "C"),
             expected_status=409)
    # 来源轮是链尖但来源区域与固化去向不符 → 409
    handover(client, "tgt", ho("tgt", "r2", "X", "r3", "C"),
             expected_status=409)
    # 接收轮定位与所报区域不符 → 409（证据按目标 ID 解释）
    handover(client, "tgt", ho("tgt", "r2", "B", "r3", "B"),
             expected_status=409)
    # 失败不落库：链仍停在归并前的 r2/B
    got = chain(client, "tgt")
    assert len(got["history"]) == 1
    assert got["current_round_id"] == "r2" and got["current_area"] == "B"
    # 旧交接记录与盘点摘要未被改写
    old = got["history"][0]
    assert old["source_round_digest"] == \
        client.get("/inventory-rounds/r1").json()["request_digest"]


def _setup_two_identity_round(client: TestClient, round_id: str, start: float,
                              expected: list[tuple[str, str]],
                              events: list[tuple[str, str, float]],
                              delta: float = 5) -> None:
    reconcile(client, round_id, round_body(start, expected, events=events,
                                           delta=delta))


def test_premerge_round_two_identities_consistent_locations(env):
    """归并前轮次列出两个身份且都明确定位于同一区域：证据一致，续接成功。"""
    client = env
    # 在归并前完成：建链（src 的 r1->r2）+ 一轮双方都明确定位于 C 的 r3
    make_exhibit(client, "src")
    make_exhibit(client, "tgt")
    reconcile(client, "r1", round_body(BASE, [("src", "A")]))
    reconcile(client, "r2", round_body(BASE + 100, [("src", "B")]))
    handover(client, "src", ho("src", "r1", "A", "r2", "B"))
    # r3 固化在归并之前：两个身份各自预期且都定位在 C
    reconcile(client, "r3", round_body(BASE + 200, [("src", "C"), ("tgt", "C")]))
    _merge(client, "src", "tgt")

    # r3 在归并前固化（两份身份证据都合格且区域一致）→ 允许续接
    out = handover(client, "tgt", ho("tgt", "r2", "B", "r3", "C"))
    assert out["seq"] == 2
    assert chain(client, "src")["current_area"] == "C"


def test_premerge_round_two_identities_any_bad_evidence_blocks(env):
    """同轮两个身份中任一份证据不合格（缺失/错位/位置不明/意外出现），
    即使另一份明确定位也不得择一放行。"""
    client = env
    make_exhibit(client, "src")
    make_exhibit(client, "tgt")

    # 缺失：r-missing 中 src 明确定位 C，tgt 在预期清单但无事件
    _setup_two_identity_round(
        client, "r-missing", BASE + 200,
        expected=[("src", "C"), ("tgt", "C")],
        events=[("src", "C", BASE + 205)],
    )
    # 错位：r-mis 中 src 定位 C，tgt 定位 X
    _setup_two_identity_round(
        client, "r-mis", BASE + 300,
        expected=[("src", "C"), ("tgt", "C")],
        events=[("src", "C", BASE + 305), ("tgt", "X", BASE + 305)],
    )
    # 位置不明：r-unk 中 src 定位 C，tgt 最后窗口多区域信号差不足阈值
    _setup_two_identity_round(
        client, "r-unk", BASE + 400,
        expected=[("src", "C"), ("tgt", "C")],
        events=[("src", "C", BASE + 405),
                ("tgt", "C", BASE + 405), ("tgt", "X", BASE + 405)],
        delta=5,
    )
    # 意外出现：r-unexp 只预期 src 在 C，tgt 不在预期但被读到 C
    _setup_two_identity_round(
        client, "r-unexp", BASE + 500,
        expected=[("src", "C")],
        events=[("src", "C", BASE + 505), ("tgt", "C", BASE + 505)],
    )

    reconcile(client, "r1", round_body(BASE - 200, [("src", "A")]))
    reconcile(client, "r2", round_body(BASE - 100, [("src", "B")]))
    handover(client, "src", ho("src", "r1", "A", "r2", "B"))
    _merge(client, "src", "tgt")

    for rid in ("r-missing", "r-mis", "r-unk", "r-unexp"):
        handover(client, "tgt", ho("tgt", "r2", "B", rid, "C"),
                 expected_status=409)
        handover(client, "src", ho("src", "r2", "B", rid, "C"),
                 expected_status=409)

    # 全部失败、不落库：链尖仍是归并前的 r2/B
    got = chain(client, "tgt")
    assert len(got["history"]) == 1
    assert got["current_round_id"] == "r2" and got["current_area"] == "B"


def test_premerge_round_conflicting_locations_block(env):
    """同轮两个身份都明确定位但区域不一致（src 在 C、tgt 在 D）：
    定位区域冲突，不能择一放行，409 且不写入。"""
    client = env
    make_exhibit(client, "src")
    make_exhibit(client, "tgt")
    reconcile(client, "r1", round_body(BASE, [("src", "A")]))
    reconcile(client, "r2", round_body(BASE + 100, [("src", "B")]))
    handover(client, "src", ho("src", "r1", "A", "r2", "B"))
    # 双方各自预期各自的区域且都明确定位：归并后解释为同一展品位置冲突
    reconcile(client, "r3", round_body(
        BASE + 200, [("src", "C"), ("tgt", "D")]))
    _merge(client, "src", "tgt")

    handover(client, "tgt", ho("tgt", "r2", "B", "r3", "C"),
             expected_status=409)
    handover(client, "tgt", ho("tgt", "r2", "B", "r3", "D"),
             expected_status=409)
    assert chain(client, "tgt")["current_round_id"] == "r2"
    assert len(chain(client, "tgt")["history"]) == 1


def test_failed_continuation_leaves_old_records_untouched(env):
    """跨归并续接失败时，原盘点摘要与旧交接记录均不改写。"""
    client = env
    make_exhibit(client, "src")
    make_exhibit(client, "tgt")
    r1 = reconcile(client, "r1", round_body(BASE, [("src", "A")]))
    r2 = reconcile(client, "r2", round_body(BASE + 100, [("src", "B")]))
    h1 = handover(client, "src", ho("src", "r1", "A", "r2", "B"))
    _merge(client, "src", "tgt")
    # r3 中 src 已不存在（只有 tgt），但 tgt 缺失 → 409
    reconcile(client, "r3", round_body(BASE + 200, [("tgt", "C")], events=[]))

    handover(client, "tgt", ho("tgt", "r2", "B", "r3", "C"),
             expected_status=409)

    assert client.get("/inventory-rounds/r1").json()["request_digest"] == \
        r1["request_digest"]
    assert client.get("/inventory-rounds/r2").json()["request_digest"] == \
        r2["request_digest"]
    got = chain(client, "tgt")
    assert len(got["history"]) == 1
    assert got["history"][0]["created_at"] == h1["created_at"]
    assert got["history"][0]["source_round_digest"] == r1["request_digest"]


# ---------- 归并联动：单侧链改挂、双侧链拒绝 ----------

def test_merge_carries_single_side_chain_and_queryable_by_target_or_alias(env):
    client = env
    make_exhibit(client, "src")
    make_exhibit(client, "tgt")
    reconcile(client, "r1", round_body(BASE, [("src", "A"), ("tgt", "A")]))
    reconcile(client, "r2", round_body(BASE + 100, [("src", "B"), ("tgt", "A")]))
    # src 先有一笔交接；tgt 没有交接链
    handover(client, "src", ho("src", "r1", "A", "r2", "B"))

    preview = client.post("/merges/preview",
                          json={"source_id": "src", "target_id": "tgt"}).json()
    assert preview["source_handover_count"] == 1
    assert preview["target_handover_count"] == 0
    assert preview["area_handover_conflict"] is False
    assert preview["ready_to_commit"] is True

    committed = client.post("/merges/commit",
                            json={"token": preview["token"], "resolutions": []}).json()
    assert committed["handover_chain_carried"] is True

    # 归并后目标 ID 与来源别名查到同一条链，当前去向保留
    via_target = chain(client, "tgt")
    via_alias = chain(client, "src")
    assert via_target["canonical_id"] == "tgt"
    assert via_alias["requested_id"] == "src" and via_alias["canonical_id"] == "tgt"
    assert via_target["history"] == via_alias["history"]
    assert via_target["current_area"] == "B" and via_target["current_round_id"] == "r2"
    assert via_target["history"][0]["source_round_digest"]


def test_merge_rejected_when_both_sides_have_chains(env):
    client = env
    make_exhibit(client, "src")
    make_exhibit(client, "tgt")
    reconcile(client, "r1", round_body(BASE, [("src", "A"), ("tgt", "A")]))
    reconcile(client, "r2", round_body(BASE + 100, [("src", "B"), ("tgt", "B")]))
    handover(client, "src", ho("src", "r1", "A", "r2", "B"))
    handover(client, "tgt", ho("tgt", "r1", "A", "r2", "B"))

    preview = client.post("/merges/preview",
                          json={"source_id": "src", "target_id": "tgt"}).json()
    assert preview["area_handover_conflict"] is True
    assert preview["ready_to_commit"] is False

    r = client.post("/merges/commit",
                    json={"token": preview["token"], "resolutions": []})
    assert r.status_code == 409, r.text

    # 不改动任何记录：两个档案、两条链都在
    assert client.get("/exhibits/src").json()["canonical_id"] == "src"
    assert len(chain(client, "src")["history"]) == 1
    assert len(chain(client, "tgt")["history"]) == 1
    assert chain(client, "src")["current_area"] == "B"


def test_preview_token_invalidated_after_new_handover(env):
    client = env
    make_exhibit(client, "src")
    make_exhibit(client, "tgt")
    reconcile(client, "r1", round_body(BASE, [("src", "A"), ("tgt", "A")]))
    reconcile(client, "r2", round_body(BASE + 100, [("src", "B"), ("tgt", "A")]))
    token = client.post("/merges/preview",
                        json={"source_id": "src", "target_id": "tgt"}).json()["token"]
    # 预演后来源侧新增一笔交接
    handover(client, "src", ho("src", "r1", "A", "r2", "B"))
    r = client.post("/merges/commit", json={"token": token, "resolutions": []})
    assert r.status_code == 409
    assert client.get("/exhibits/src").status_code == 200


# ---------- 隔离性：盘点结果 / 读数 / 收据不变 ----------

def test_handover_does_not_modify_rounds_readings_or_receipts(env):
    client = env
    make_exhibit(client, "vase")
    send = [{"id": "x1", "ts": iso(BASE), "lux": 3}]
    rr = client.post("/exhibits/vase/readings", json={"readings": send})
    assert rr.status_code == 200
    r1 = reconcile(client, "r1", round_body(BASE, [("vase", "A")]))
    r2 = reconcile(client, "r2", round_body(BASE + 100, [("vase", "B")]))
    round_before = client.get("/inventory-rounds/r1").json()

    handover(client, "vase", ho("vase", "r1", "A", "r2", "B"))

    assert client.get("/inventory-rounds/r1").json() == round_before
    readings = client.get("/exhibits/vase/readings").json()["readings"]
    assert [r["id"] for r in readings] == ["x1"]
    receipts = client.get("/exhibits/vase/receipts").json()["receipts"]
    assert len(receipts) == 1
    verify = client.get("/exhibits/vase/receipts/verify").json()
    assert verify["ok"] is True
    # 固化摘要与轮次摘要一致
    assert chain(client, "vase")["history"][0]["source_round_digest"] == \
        r1["request_digest"]
