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


def _merge(client: TestClient, src: str, tgt: str) -> dict:
    token = client.post("/merges/preview",
                       json={"source_id": src, "target_id": tgt}).json()["token"]
    r = client.post("/merges/commit", json={"token": token, "resolutions": []})
    assert r.status_code == 200, r.text
    return r.json()


def _src_chain_one_handover(client: TestClient, src: str, tgt: str,
                             *, both_in_rounds: bool = False,
                             a1: str = "A", a2: str = "B") -> None:
    """src 在归并前固化一笔 r1(a1)→r2(a2) 的交接；tgt 无链。

    both_inrounds=True 时两轮预期清单同时列出 tgt（归并前是另一件展品）：
    r1 中两者同在 a1，r2 中 src 在 a2、tgt 在 a1——归并后 r2 对同一
    规范档案呈现两个身份定位区域冲突的固化证据。
    """
    make_exhibit(client, src)
    make_exhibit(client, tgt)
    if both_in_rounds:
        reconcile(client, "r1", round_body(BASE, [(src, a1), (tgt, a1)]))
        reconcile(client, "r2", round_body(BASE + 100, [(src, a2), (tgt, a1)]))
    else:
        reconcile(client, "r1", round_body(BASE, [(src, a1)]))
        reconcile(client, "r2", round_body(BASE + 100, [(src, a2)]))
    handover(client, src, ho(src, "r1", a1, "r2", a2))


# ---------- 跨归并续接：来源链改挂到无链目标后继续向前 ----------

def test_continuation_after_merge_via_target_id_or_alias(env):
    """来源档案已有交接链并归并到无链目标后，沿用归并前固化的接收轮作为
    下一笔来源轮；用目标 ID 或来源别名提交都按当前别名关系解释旧证据。"""
    client = env
    _src_chain_one_handover(client, "src", "tgt")
    committed = _merge(client, "src", "tgt")
    assert committed["handover_chain_carried"] is True

    # 归并后新固化的接收轮只列目标规范身份
    reconcile(client, "r3", round_body(BASE + 200, [("tgt", "C")]))

    # 修复前：r2 固化证据里是旧身份 src，与当前目标 tgt 直接比较 → 错误 409
    out = handover(client, "tgt", ho("tgt", "r2", "B", "r3", "C"))
    assert out["seq"] == 2
    assert out["canonical_id"] == "tgt" and out["requested_id"] == "tgt"

    # 用来源别名提交下一笔：来源轮 r3 只有 tgt 身份，同样按别名关系解释
    reconcile(client, "r4", round_body(BASE + 300, [("src", "D")]))
    out = handover(client, "src", ho("src", "r3", "C", "r4", "D"))
    assert out["seq"] == 3
    assert out["canonical_id"] == "tgt" and out["requested_id"] == "src"

    # 旧证据行（r1→r2，归并前固化）原样保留；目标 ID 与来源别名查到同一条链
    via_target = chain(client, "tgt")
    via_alias = chain(client, "src")
    assert [(h["seq"], h["source_round_id"], h["target_round_id"],
             h["source_area"], h["target_area"]) for h in via_target["history"]] == [
        (1, "r1", "r2", "A", "B"),
        (2, "r2", "r3", "B", "C"),
        (3, "r3", "r4", "C", "D"),
    ]
    assert via_target["history"] == via_alias["history"]
    assert via_target["current_round_id"] == "r4" and via_target["current_area"] == "D"


def test_continuation_after_chain_of_two_merges(env):
    """链式归并（src→mid→tgt）后，别名一跳改指向最终目标，旧证据仍可解释。"""
    client = env
    _src_chain_one_handover(client, "src", "mid")
    _merge(client, "src", "mid")
    make_exhibit(client, "tgt")
    _merge(client, "mid", "tgt")

    reconcile(client, "r3", round_body(BASE + 200, [("tgt", "C")]))
    # 最终目标 tgt 与旧别名 src/mid 都能解释 r2 中固化的 src 身份
    out = handover(client, "tgt", ho("tgt", "r2", "B", "r3", "C"))
    assert out["seq"] == 2 and out["canonical_id"] == "tgt"
    reconcile(client, "r4", round_body(BASE + 300, [("mid", "D")]))
    out = handover(client, "mid", ho("mid", "r3", "C", "r4", "D"))
    assert out["seq"] == 3 and out["canonical_id"] == "tgt"
    # 查询保持改挂语义：三个 ID 查到同一条链
    histories = [chain(client, eid)["history"] for eid in ("tgt", "mid", "src")]
    assert all(h == histories[0] for h in histories)


def test_failure_after_merge_does_not_persist_and_keeps_tip(env):
    """跨归并续接核验失败不落库：链尖仍是归并改挂来的旧接收轮。"""
    client = env
    _src_chain_one_handover(client, "src", "tgt")
    _merge(client, "src", "tgt")
    # 接收轮缺失（预期 C 但无事件）→ 409
    reconcile(client, "r-miss", round_body(BASE + 200, [("tgt", "C")], events=[]))
    handover(client, "tgt", ho("tgt", "r2", "B", "r-miss", "C"),
             expected_status=409)
    # 链尖未被推进：当前去向仍是 r2/B，只有归并带来的 1 笔历史
    got = chain(client, "tgt")
    assert got["current_round_id"] == "r2" and got["current_area"] == "B"
    assert len(got["history"]) == 1

    # 旧轮不能借归并身份回填；来源区域与链尖区域不符
    reconcile(client, "r3", round_body(BASE + 300, [("tgt", "C")]))
    handover(client, "src", ho("src", "r1", "A", "r3", "C"),
             expected_status=409)
    handover(client, "src", ho("src", "r2", "X", "r3", "C"),
             expected_status=409)
    got = chain(client, "src")
    assert got["current_area"] == "B" and len(got["history"]) == 1

    # 条件修复后同轮语义仍可续接（失败不占用）
    out = handover(client, "tgt", ho("tgt", "r2", "B", "r3", "C"))
    assert out["seq"] == 2


# ---------- 跨归并冲突回归：同轮多个身份须一起核对，不择一放行 ----------

def test_source_round_multi_identity_area_conflict_blocks_continuation(env):
    """链尖来源轮（归并前固化）列有两个身份且定位区域冲突时不能续接：
    不得只挑选与所报区域一致的那份身份放行。旧交接记录保持不变。"""
    client = env
    # r1 两身份同在 A；r2 中 src=B、tgt=A（归并前是两件展品，归并后冲突）
    _src_chain_one_handover(client, "src", "tgt", both_in_rounds=True)
    _merge(client, "src", "tgt")
    reconcile(client, "r3", round_body(BASE + 200, [("tgt", "C")]))

    # 报 B（src 身份）：tgt 身份定位 A，冲突 → 409
    handover(client, "tgt", ho("tgt", "r2", "B", "r3", "C"),
             expected_status=409)
    # 报 A（tgt 身份）：src 身份定位 B，同样冲突 → 409
    handover(client, "src", ho("src", "r2", "A", "r3", "C"),
             expected_status=409)
    # 失败不落库：链尖仍是改挂来的 r2/B，无新记录
    got = chain(client, "tgt")
    assert len(got["history"]) == 1
    assert got["current_round_id"] == "r2" and got["current_area"] == "B"


@pytest.mark.parametrize(
    "expected,events",
    [
        # 旧身份 src 错位到 X，新身份 tgt 正常 C
        ([("src", "B"), ("tgt", "C")],
         [("src", "X", BASE + 205), ("tgt", "C", BASE + 205)]),
        # 旧身份 src 缺失（预期 B 但无事件），tgt 正常 C
        ([("src", "B"), ("tgt", "C")],
         [("tgt", "C", BASE + 205)]),
        # 旧身份 src 位置不明（同窗口 B/X 信号无差），tgt 正常 C
        ([("src", "B"), ("tgt", "C")],
         [("src", "B", BASE + 205), ("src", "X", BASE + 205),
          ("tgt", "C", BASE + 205)]),
        # 两个身份都明确定位但区域互相冲突（src=B、tgt=C），即使报 C 也不放行
        ([("src", "B"), ("tgt", "C")],
         [("src", "B", BASE + 205), ("tgt", "C", BASE + 205)]),
        # 旧身份 src 意外出现于 X（未列入预期但被读到），tgt 正常 C
        ([("tgt", "C")],
         [("src", "X", BASE + 205), ("tgt", "C", BASE + 205)]),
    ],
)
def test_receiver_round_any_identity_bad_or_conflict_blocks(env, expected, events):
    """接收轮归并前固化列有多个身份时，缺失 / 错位 / 位置不明 / 意外出现 /
    定位区域冲突任一存在都 409，不得择一放行；失败不写入、不改写盘点摘要。"""
    client = env
    _src_chain_one_handover(client, "src", "tgt")
    # 接收轮必须在归并**之前**固化（归并后 src/tgt 解析为同一规范档案，
    # 对账重复预期会 422，无法再造出同轮多身份的固化证据）
    reconcile(client, "r3", round_body(BASE + 200, expected, events=events))
    digest_before = client.get("/inventory-rounds/r3").json()["request_digest"]
    _merge(client, "src", "tgt")

    handover(client, "tgt", ho("tgt", "r2", "B", "r3", "C"),
             expected_status=409)
    got = chain(client, "tgt")
    assert len(got["history"]) == 1
    assert got["current_round_id"] == "r2" and got["current_area"] == "B"
    # 原盘点固化结果不被改写
    assert client.get("/inventory-rounds/r3").json()["request_digest"] == digest_before


def test_multi_identity_all_located_same_area_passes(env):
    """接收轮多个身份都明确定位在同一区域（与所报区域一致）时通过——
    “一起核对、全部合格才放行”的正向分支（两轮均在归并前固化）。"""
    client = env
    # r1/r2 同时列两身份且定位一致（都在 A、都在 B）
    make_exhibit(client, "src")
    make_exhibit(client, "tgt")
    reconcile(client, "r1", round_body(BASE, [("src", "A"), ("tgt", "A")]))
    reconcile(client, "r2", round_body(BASE + 100, [("src", "B"), ("tgt", "B")]))
    handover(client, "src", ho("src", "r1", "A", "r2", "B"))
    # 接收轮同样在归并前固化：src/tgt 都定位在 C（多身份同区域，全部合格）
    reconcile(client, "r3", round_body(BASE + 200, [("src", "C"), ("tgt", "C")]))
    _merge(client, "src", "tgt")
    out = handover(client, "src", ho("src", "r2", "B", "r3", "C"))
    assert out["seq"] == 2 and out["canonical_id"] == "tgt"
    assert chain(client, "tgt")["current_area"] == "C"


# ---------- 隔离性：跨归并续接不改写原盘点摘要与旧交接记录 ----------

def test_cross_merge_continuation_preserves_inventory_and_old_handover(env):
    client = env
    _src_chain_one_handover(client, "src", "tgt")
    r1_before = client.get("/inventory-rounds/r1").json()
    r2_before = client.get("/inventory-rounds/r2").json()
    old_before = chain(client, "src")["history"][0]
    _merge(client, "src", "tgt")
    reconcile(client, "r3", round_body(BASE + 200, [("tgt", "C")]))
    handover(client, "src", ho("src", "r2", "B", "r3", "C"))

    # 原盘点摘要（归并前以 src 身份固化）原样保留
    assert client.get("/inventory-rounds/r1").json() == r1_before
    assert client.get("/inventory-rounds/r2").json() == r2_before
    # 旧交接记录（seq=1）证据行不改写：仍是 r1→r2、A→B、旧轮次摘要与固化时刻
    old = chain(client, "tgt")["history"][0]
    assert (old["source_round_id"], old["target_round_id"],
            old["source_area"], old["target_area"]) == ("r1", "r2", "A", "B")
    assert old["source_round_digest"] == old_before["source_round_digest"]
    assert old["target_round_digest"] == old_before["target_round_digest"]
    assert old["created_at"] == old_before["created_at"]


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
