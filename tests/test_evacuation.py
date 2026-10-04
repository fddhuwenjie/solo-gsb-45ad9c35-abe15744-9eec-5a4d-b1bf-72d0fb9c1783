"""展品应急撤离编排：时变最早到达、容量竞争、半开封闭绕行/等待、平局、别名、错误码与隔离性。"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

START = "2026-09-26T08:00:00Z"
START_EPOCH = datetime(2026, 9, 26, 8, 0, tzinfo=timezone.utc).timestamp()


def iso(offset: float) -> str:
    return datetime.fromtimestamp(
        START_EPOCH + offset, tz=timezone.utc
    ).isoformat().replace("+00:00", "Z")


@pytest.fixture()
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("DB_PATH", str(tmp_path / "test.db"))
    from app.main import app

    with TestClient(app) as c:
        yield c, tmp_path / "test.db"


def make_exhibit(client: TestClient, eid: str) -> str:
    resp = client.post("/exhibits", json={
        "id": eid, "name": eid,
        "dose_limit_luxh": 1000.0, "max_interval_seconds": 3600.0,
    })
    assert resp.status_code == 201, resp.text
    return resp.json()["canonical_id"]


def edge(u: str, v: str, travel: int, capacity: int = 1,
         closures: list[list[int]] | None = None) -> dict:
    return {
        "from": u, "to": v, "travel_seconds": travel,
        "capacity": capacity,
        "closures": [{"start": s, "end": e} for s, e in (closures or [])],
    }


def body(exhibits: list[dict], *, nodes: list[str], edges: list[dict],
         exits: list[str], start_at: str = START) -> dict:
    return {
        "start_at": start_at,
        "nodes": nodes,
        "edges": edges,
        "exits": exits,
        "exhibits": exhibits,
    }


def plan(client: TestClient, payload: dict, expected_status: int = 200) -> dict:
    resp = client.post("/evacuation-plan", json=payload)
    assert resp.status_code == expected_status, resp.text
    return resp.json()


def ex(eid: str, node: str, priority: int = 0) -> dict:
    return {"exhibit_id": eid, "start_node": node, "priority": priority}


LINE = {
    "nodes": ["A", "B", "E"],
    "edges": [edge("A", "B", 5), edge("B", "E", 3)],
    "exits": ["E"],
}


# ---------- 基本编排 ----------

def test_basic_single_exhibit_legs_and_timestamps(env):
    client, _ = env
    make_exhibit(client, "x")
    out = plan(client, body([ex("x", "A", 1)], **LINE))
    (r,) = out["results"]
    assert r["exit_node"] == "E"
    assert r["path"] == ["A", "B", "E"]
    assert r["completion_offset_seconds"] == 8
    assert r["wait_seconds"] == 0
    assert r["completed_at"] == iso(8)
    assert out["total_completion_offset_seconds"] == 8
    assert out["start_at"] == START

    l1, l2 = r["legs"]
    assert l1 == {
        "from": "A", "to": "B", "travel_seconds": 5,
        "enter_offset_seconds": 0, "leave_offset_seconds": 5,
        "enter_at": iso(0), "leave_at": iso(5), "wait_seconds": 0,
    }
    assert l2["from"] == "B" and l2["to"] == "E"
    assert (l2["enter_offset_seconds"], l2["leave_offset_seconds"],
            l2["wait_seconds"]) == (5, 8, 0)


def test_start_node_is_exit_zero_legs(env):
    client, _ = env
    make_exhibit(client, "x")
    b = body([ex("x", "E", 1)], nodes=["E"], edges=[], exits=["E"])
    out = plan(client, b)
    (r,) = out["results"]
    assert r["path"] == ["E"]
    assert r["legs"] == []
    assert r["completion_offset_seconds"] == 0
    assert r["wait_seconds"] == 0
    assert r["completed_at"] == START
    assert out["total_completion_offset_seconds"] == 0


# ---------- 容量竞争（半开 [进入,离开)） ----------

def test_capacity_contention_second_waits_for_release(env):
    """容量 1、通行 10：第二件必须等到第一件离开（t=10）整点才能进入。"""
    client, _ = env
    make_exhibit(client, "a")
    make_exhibit(client, "b")
    b = body(
        [ex("a", "A", 2), ex("b", "A", 1)],
        nodes=["A", "B"], edges=[edge("A", "B", 10, 1)], exits=["B"],
    )
    out = plan(client, b)
    by_id = {r["canonical_id"]: r for r in out["results"]}
    assert by_id["a"]["legs"][0]["enter_offset_seconds"] == 0
    assert by_id["b"]["legs"][0]["enter_offset_seconds"] == 10
    assert by_id["b"]["legs"][0]["wait_seconds"] == 10
    assert by_id["b"]["completion_offset_seconds"] == 20
    assert out["total_completion_offset_seconds"] == 20


def test_half_open_entry_at_exact_release_does_not_conflict(env):
    """半开区间：恰好在他人离开时刻进入不算冲突（容量 1 仍可紧接进入）。"""
    client, _ = env
    make_exhibit(client, "a")
    make_exhibit(client, "b")
    # 通行 6：第一件 [0,6)；第二件 6 进入 [6,12)，与 [0,6) 不重叠
    b = body(
        [ex("a", "A", 2), ex("b", "A", 1)],
        nodes=["A", "B"], edges=[edge("A", "B", 6, 1)], exits=["B"],
    )
    out = plan(client, b)
    second = [r for r in out["results"] if r["canonical_id"] == "b"][0]
    assert second["legs"][0]["enter_offset_seconds"] == 6
    assert second["legs"][0]["wait_seconds"] == 6


def test_capacity_two_allows_concurrent_third_waits(env):
    client, _ = env
    for eid in ("a", "b", "c"):
        make_exhibit(client, eid)
    b = body(
        [ex("a", "A", 3), ex("b", "A", 2), ex("c", "A", 1)],
        nodes=["A", "B"], edges=[edge("A", "B", 10, 2)], exits=["B"],
    )
    out = plan(client, b)
    enters = {r["canonical_id"]: r["legs"][0]["enter_offset_seconds"]
              for r in out["results"]}
    assert enters == {"a": 0, "b": 0, "c": 10}


def test_capacity_contention_on_shared_downstream_corridor(env):
    """两条支路汇入同一容量 1 通道：先编排展品占用后，后者在汇合节点等待。"""
    client, _ = env
    make_exhibit(client, "a")
    make_exhibit(client, "b")
    # a: S1 -> M（2s），b: S2 -> M（5s）；M -> E 容量 1 通行 4
    # a 先编排（优先级高），t=2 进入 M->E 占用 [2,6)；
    # b t=5 到达 M，[5,9) 与 [2,6) 重叠 -> 等到 6 进入，6+4=10 到达
    b = body(
        [ex("a", "S1", 2), ex("b", "S2", 1)],
        nodes=["S1", "S2", "M", "E"],
        edges=[edge("S1", "M", 2), edge("S2", "M", 5), edge("M", "E", 4, 1)],
        exits=["E"],
    )
    out = plan(client, b)
    by_id = {r["canonical_id"]: r for r in out["results"]}
    assert by_id["a"]["completion_offset_seconds"] == 6
    assert by_id["b"]["path"] == ["S2", "M", "E"]
    assert by_id["b"]["legs"][1]["enter_offset_seconds"] == 6
    assert by_id["b"]["legs"][1]["wait_seconds"] == 1
    assert by_id["b"]["completion_offset_seconds"] == 10


def test_capacity_staggered_under_saturation_enters_immediately(env):
    """错峰未饱和：与窗口重叠的占用条数达到容量，但任一时刻实际并发未满。

    通道容量 3、通行 10；三件已编排展品分别 1、10、19 进入，占用
    [1,11)、[10,20)、[19,29)。新展品经 10 秒支路在 10 到达瓶颈，窗口
    [10,20) 与全部三条占用半开重叠（旧实现按重叠条数 3 >= 容量 3 误判，
    推迟到 11）；但窗口内任一时刻实际并发最多为 2（10-11 与 19-20），
    容量未满，正确结果是 10 整点立即进入。
    """
    client, _ = env
    for eid in ("a", "b", "c", "x"):
        make_exhibit(client, eid)
    # 远端支路让 a/b/c/x 分别在 1、10、19、10 到达瓶颈 P->B
    b = body(
        [
            ex("a", "F1", 4), ex("b", "F2", 3), ex("c", "F3", 2),
            ex("x", "Fx", 1),
        ],
        nodes=["F1", "F2", "F3", "Fx", "P", "B"],
        edges=[
            edge("F1", "P", 1), edge("F2", "P", 10), edge("F3", "P", 19),
            edge("Fx", "P", 10), edge("P", "B", 10, 3),
        ],
        exits=["B"],
    )
    out = plan(client, b)
    by_id = {r["canonical_id"]: r for r in out["results"]}
    assert by_id["a"]["legs"][1]["enter_offset_seconds"] == 1
    assert by_id["b"]["legs"][1]["enter_offset_seconds"] == 10
    assert by_id["c"]["legs"][1]["enter_offset_seconds"] == 19
    # 新展品 10 到达 P：重叠占用有 3 条，但实际瞬时并发峰值仅 2，不等待
    assert by_id["x"]["legs"][1]["enter_offset_seconds"] == 10
    assert by_id["x"]["legs"][1]["wait_seconds"] == 0
    assert by_id["x"]["completion_offset_seconds"] == 20


def test_capacity_true_saturation_waits_for_actual_release(env):
    """真实饱和：同一时刻确有 capacity 件占用时必须等待，修复不得矫枉过正。

    通道容量 3、通行 10；三件展品分别在 0、2、4 进入瓶颈，在 [4,10)
    瞬时并发为 3。第四件经 4 秒支路在 4 到达，加入后峰值将达到 4 →
    必须等待；最早释放发生在 10（[0,10) 离开，半开整点不计并发），
    10 进入后峰值为 2，立即放行。若实现错把容量口径放宽成只看
    “重叠条数以外”的计数，此处会误放行。
    """
    client, _ = env
    for eid in ("a", "b", "c", "x"):
        make_exhibit(client, eid)
    # 让 a/b/c 的瓶颈段分别在 0、2、4 进入 P->B（容量 3、通行 10）
    b = body(
        [
            ex("a", "P", 4), ex("b", "F2", 3), ex("c", "F3", 2),
            ex("x", "Fx", 1),
        ],
        nodes=["F2", "F3", "Fx", "P", "B"],
        edges=[
            edge("F2", "P", 2), edge("F3", "P", 4), edge("Fx", "P", 4),
            edge("P", "B", 10, 3),
        ],
        exits=["B"],
    )
    out = plan(client, b)
    by_id = {r["canonical_id"]: r for r in out["results"]}
    assert by_id["a"]["legs"][0]["enter_offset_seconds"] == 0
    assert by_id["b"]["legs"][1]["enter_offset_seconds"] == 2
    assert by_id["c"]["legs"][1]["enter_offset_seconds"] == 4
    assert by_id["x"]["legs"][1]["enter_offset_seconds"] == 10
    assert by_id["x"]["legs"][1]["wait_seconds"] == 6
    assert by_id["x"]["completion_offset_seconds"] == 20


def test_capacity_half_open_exact_release_under_staggered_load(env):
    """恰好释放：候选整点恰有一条占用离开，瞬时并发降到容量以下即放行。

    容量 2、通行 10：瓶颈已占用 [0,10) 与 [5,15)。新展品经 10 秒支路
    在 10 到达瓶颈，[0,10) 在 10 整点恰好离开（半开不计），窗口
    [10,20) 内只剩 [5,15) 一条占用（15 之后为 0），峰值 1 < 2，
    应 10 立即进入。
    """
    client, _ = env
    for eid in ("a", "b", "x"):
        make_exhibit(client, eid)
    b = body(
        [ex("a", "P", 3), ex("b", "F", 2), ex("x", "Fx", 1)],
        nodes=["F", "Fx", "P", "B"],
        edges=[edge("F", "P", 5), edge("Fx", "P", 10), edge("P", "B", 10, 2)],
        exits=["B"],
    )
    out = plan(client, b)
    by_id = {r["canonical_id"]: r for r in out["results"]}
    assert by_id["a"]["legs"][0]["enter_offset_seconds"] == 0
    assert by_id["b"]["legs"][1]["enter_offset_seconds"] == 5
    assert by_id["x"]["legs"][1]["enter_offset_seconds"] == 10
    assert by_id["x"]["legs"][1]["wait_seconds"] == 0
    assert by_id["x"]["completion_offset_seconds"] == 20


def test_capacity_then_closure_then_capacity_consecutive_blocking(env):
    """容量与封闭连续阻塞：每次推进后重新检查两类约束，不跳过更早空档。

    瓶颈 A->B 容量 1、通行 4、封闭 [6,10)。四件高优先级展品依次占用
    [0,4)、[4,8)（跨越封闭允许）、[10,14)、[14,18)；新展品 0 到达：
      - t=0 容量满 → 推到 4；4 仍被 [4,8) 占用 → 推到 8；
      - t=8 落在封闭 [6,10) → 推到 10；
      - t=10 又被 [10,14) 占用 → 推到 14；14 被 [14,18) 占用 → 推到 18；
      - 18 时刻两类约束都满足，进入，22 完成。
    中途 8、10、14 每个候选都必须在推进后重新同时检查容量与封闭。
    """
    client, _ = env
    for i in range(5):
        make_exhibit(client, f"p{i}")
    b = body(
        [ex(f"p{i}", "A", 10 - i) for i in range(4)] + [ex("p4", "A", 0)],
        nodes=["A", "B"],
        edges=[edge("A", "B", 4, 1, closures=[[6, 10]])],
        exits=["B"],
    )
    out = plan(client, b)
    rows = {
        r["canonical_id"]: (
            r["legs"][0]["enter_offset_seconds"],
            r["legs"][0]["wait_seconds"],
            r["completion_offset_seconds"],
        )
        for r in out["results"]
    }
    assert rows == {
        "p0": (0, 0, 4),
        "p1": (4, 4, 8),
        "p2": (10, 10, 14),
        "p3": (14, 14, 18),
        "p4": (18, 18, 22),
    }
    assert out["total_completion_offset_seconds"] == 22


# ---------- 半开封闭时段：等待与绕行 ----------

def test_closure_waits_to_exact_end_when_no_detour(env):
    """直达通道封闭 [0,10)，无绕行路径：等待到 10 整点进入，11 到达。"""
    client, _ = env
    make_exhibit(client, "x")
    b = body(
        [ex("x", "A", 1)],
        nodes=["A", "E"],
        edges=[edge("A", "E", 1, 1, closures=[[0, 10]])],
        exits=["E"],
    )
    out = plan(client, b)
    (r,) = out["results"]
    assert r["path"] == ["A", "E"]
    assert r["legs"][0]["enter_offset_seconds"] == 10
    assert r["legs"][0]["wait_seconds"] == 10
    assert r["completion_offset_seconds"] == 11


def test_closure_half_open_end_is_enterable(env):
    """封闭右端点不含：t == end 整点可立即进入（封闭 [2,6)，6 到达则无需等待）。"""
    client, _ = env
    make_exhibit(client, "x")
    # A->B 6s 到达 B=6，B->E 封闭 [0,6)：6 整点可进入
    b = body(
        [ex("x", "A", 1)],
        nodes=["A", "B", "E"],
        edges=[edge("A", "B", 6), edge("B", "E", 2, 1, closures=[[0, 6]])],
        exits=["E"],
    )
    out = plan(client, b)
    (r,) = out["results"]
    assert r["legs"][1]["enter_offset_seconds"] == 6
    assert r["legs"][1]["wait_seconds"] == 0


def test_closure_takes_detour_when_faster(env):
    """直达通道长时间封闭，绕行更快时搜索选择绕行路径。"""
    client, _ = env
    make_exhibit(client, "x")
    # A->E 直达 1s 但封闭 [0,100)；绕行 A->B(2)->E(2) 共 4s
    b = body(
        [ex("x", "A", 1)],
        nodes=["A", "B", "E"],
        edges=[
            edge("A", "E", 1, 1, closures=[[0, 100]]),
            edge("A", "B", 2), edge("B", "E", 2),
        ],
        exits=["E"],
    )
    out = plan(client, b)
    (r,) = out["results"]
    assert r["path"] == ["A", "B", "E"]
    assert r["completion_offset_seconds"] == 4
    assert r["wait_seconds"] == 0


def test_short_closure_waiting_beats_detour(env):
    """短暂封闭：等待后走直达（5s 到达）优于 8s 绕行。"""
    client, _ = env
    make_exhibit(client, "x")
    # A->E 2s，封闭 [0,3)：3 进入、5 到达；绕行 A->B(4)->E(4) 共 8s
    b = body(
        [ex("x", "A", 1)],
        nodes=["A", "B", "E"],
        edges=[
            edge("A", "E", 2, 1, closures=[[0, 3]]),
            edge("A", "B", 4), edge("B", "E", 4),
        ],
        exits=["E"],
    )
    out = plan(client, b)
    (r,) = out["results"]
    assert r["path"] == ["A", "E"]
    assert r["legs"][0]["enter_offset_seconds"] == 3
    assert r["completion_offset_seconds"] == 5


def test_entering_before_closure_and_traversing_through_is_allowed(env):
    """封闭只约束“进入”动作：在封闭开始前进入、通行跨越封闭时段允许。"""
    client, _ = env
    make_exhibit(client, "x")
    # A->B 通行 10s，封闭 [5,8)：t=0 进入允许，10 离开
    b = body(
        [ex("x", "A", 1)],
        nodes=["A", "B"],
        edges=[edge("A", "B", 10, 1, closures=[[5, 8]])],
        exits=["B"],
    )
    out = plan(client, b)
    (r,) = out["results"]
    assert r["legs"][0]["enter_offset_seconds"] == 0
    assert r["completion_offset_seconds"] == 10


def test_closure_intersecting_wait_from_capacity(env):
    """容量迫使等待到 6，但 6 落在封闭 [5,10) 内：继续等到封闭结束 10。

    通道 A->B 通行 4、容量 1、封闭 [5,10)。第一件 0 进入占用 [0,4)，
    4 离开；第二件最早可行进入：容量 4 释放，但 4 在封闭前？4 < 5 可进入，
    占用 [4,8) 跨越封闭（允许）。
    """
    client, _ = env
    make_exhibit(client, "a")
    make_exhibit(client, "b")
    b = body(
        [ex("a", "A", 2), ex("b", "A", 1)],
        nodes=["A", "B"],
        edges=[edge("A", "B", 4, 1, closures=[[5, 10]])],
        exits=["B"],
    )
    out = plan(client, b)
    by_id = {r["canonical_id"]: r for r in out["results"]}
    assert by_id["a"]["legs"][0]["enter_offset_seconds"] == 0
    # 第二件 4 可进入（4 不在 [5,10)），通行跨越封闭允许
    assert by_id["b"]["legs"][0]["enter_offset_seconds"] == 4
    assert by_id["b"]["legs"][0]["wait_seconds"] == 4


def test_capacity_release_inside_closure_pushes_to_closure_end(env):
    """容量释放时刻落在封闭内：取“容量释放”与“封闭结束”的较晚者。

    通道通行 6、容量 1、封闭 [10,20)：第一件 0 进入 [0,6)；
    第二件 6 进入 [6,12)（跨越封闭起点，允许）；第三件先被容量推到 12，
    12 落在封闭 [10,20) 内 → 再等到 20 进入。
    """
    client, _ = env
    for eid in ("a", "b", "c"):
        make_exhibit(client, eid)
    b = body(
        [ex("a", "A", 3), ex("b", "A", 2), ex("c", "A", 1)],
        nodes=["A", "B"],
        edges=[edge("A", "B", 6, 1, closures=[[10, 20]])],
        exits=["B"],
    )
    out = plan(client, b)
    enters = {r["canonical_id"]: r["legs"][0]["enter_offset_seconds"]
              for r in out["results"]}
    assert enters == {"a": 0, "b": 6, "c": 20}
    third = [r for r in out["results"] if r["canonical_id"] == "c"][0]
    assert third["wait_seconds"] == 20
    assert third["completion_offset_seconds"] == 26


def test_overlapping_closures_are_merged_and_waited_once(env):
    """相接/重叠的封闭时段合并：等待直接越过整段封闭。"""
    client, _ = env
    make_exhibit(client, "x")
    # 封闭 [0,6) 与 [6,10) 相接合并为 [0,10)：0 不可进入，等到 10
    b = body(
        [ex("x", "A", 1)],
        nodes=["A", "E"],
        edges=[edge("A", "E", 1, 1, closures=[[0, 6], [6, 10]])],
        exits=["E"],
    )
    out = plan(client, b)
    (r,) = out["results"]
    assert r["legs"][0]["enter_offset_seconds"] == 10


# ---------- 平局：完整路径字典序 ----------

def test_equal_arrival_paths_tie_break_lexicographic(env):
    """两条路径到达时刻相同：取完整节点序列字典序较小者（...B1... 先于 ...B2...）。"""
    client, _ = env
    make_exhibit(client, "x")
    b = body(
        [ex("x", "A", 1)],
        nodes=["A", "B1", "B2", "E"],
        edges=[
            edge("A", "B1", 2), edge("B1", "E", 3),
            edge("A", "B2", 2), edge("B2", "E", 3),
        ],
        exits=["E"],
    )
    out = plan(client, b)
    assert out["results"][0]["path"] == ["A", "B1", "E"]


def test_equal_arrival_tie_break_independent_of_edge_input_order(env):
    """通道输入顺序颠倒不影响字典序决胜结果。"""
    client, _ = env
    make_exhibit(client, "x")
    b = body(
        [ex("x", "A", 1)],
        nodes=["A", "B1", "B2", "E"],
        edges=[
            edge("A", "B2", 2), edge("B2", "E", 3),
            edge("A", "B1", 2), edge("B1", "E", 3),
        ],
        exits=["E"],
    )
    out = plan(client, b)
    assert out["results"][0]["path"] == ["A", "B1", "E"]


def test_tie_break_through_intermediate_node_with_equal_arrival(env):
    """同一中间节点存在同时刻到达的两种走法时，路径字典序标签继续向后传播。

    到达 M 的两条首段：A->X->M 与 A->Y->M 均 4s；M->E 5s。
    含 X 的完整路径字典序更小，必须胜出。
    """
    client, _ = env
    make_exhibit(client, "x")
    b = body(
        [ex("x", "A", 1)],
        nodes=["A", "X", "Y", "M", "E"],
        edges=[
            edge("A", "X", 2), edge("X", "M", 2),
            edge("A", "Y", 2), edge("Y", "M", 2),
            edge("M", "E", 5),
        ],
        exits=["E"],
    )
    out = plan(client, b)
    assert out["results"][0]["path"] == ["A", "X", "M", "E"]


def test_two_exits_equal_arrival_lex_smaller_exit_wins(env):
    client, _ = env
    make_exhibit(client, "x")
    b = body(
        [ex("x", "A", 1)],
        nodes=["A", "E1", "E2"],
        edges=[edge("A", "E1", 5), edge("A", "E2", 5)],
        exits=["E1", "E2"],
    )
    out = plan(client, b)
    assert out["results"][0]["exit_node"] == "E1"
    assert out["results"][0]["path"] == ["A", "E1"]


def test_earliest_exit_chosen_over_lexicographic_preference(env):
    """字典序只在到达时刻相同时决胜：更早到达的出口即使标识更大也胜出。"""
    client, _ = env
    make_exhibit(client, "x")
    b = body(
        [ex("x", "A", 1)],
        nodes=["A", "E1", "E2"],
        edges=[edge("A", "E1", 9), edge("A", "E2", 3)],
        exits=["E1", "E2"],
    )
    out = plan(client, b)
    assert out["results"][0]["exit_node"] == "E2"


# ---------- 编排顺序：优先级降序、规范 ID 升序 ----------

def test_priority_desc_then_canonical_asc_ordering(env):
    client, _ = env
    for eid in ("z", "m", "a", "b"):
        make_exhibit(client, eid)
    # 同优先级的 a、m 按规范 ID 升序；通道容量 1、通行 10，进入时刻体现编排顺序
    b = body(
        [ex("z", "A", 0), ex("m", "A", 5), ex("a", "A", 5), ex("b", "A", 9)],
        nodes=["A", "B"], edges=[edge("A", "B", 10, 1)], exits=["B"],
    )
    out = plan(client, b)
    order = [r["canonical_id"] for r in out["results"]]
    assert order == ["b", "a", "m", "z"]
    enters = {r["canonical_id"]: r["legs"][0]["enter_offset_seconds"]
              for r in out["results"]}
    assert enters == {"b": 0, "a": 10, "m": 20, "z": 30}


def test_response_echoes_requested_id_priority_and_start_node(env):
    client, _ = env
    make_exhibit(client, "CANON")
    out = plan(client, body(
        [{"exhibit_id": "CANON", "start_node": "A", "priority": 7}], **LINE,
    ))
    (r,) = out["results"]
    assert r["requested_id"] == "CANON"
    assert r["canonical_id"] == "CANON"
    assert r["priority"] == 7
    assert r["start_node"] == "A"


# ---------- 别名解析 ----------

def test_alias_id_resolves_to_canonical(env):
    client, _ = env
    make_exhibit(client, "src")
    make_exhibit(client, "tgt")
    client.post("/exhibits/src/readings", json={"readings": [
        {"id": "r1", "ts": iso(0), "lux": 1},
    ]})
    token = client.post("/merges/preview",
                        json={"source_id": "src", "target_id": "tgt"}).json()["token"]
    assert client.post("/merges/commit",
                       json={"token": token, "resolutions": []}).status_code == 200

    # 用已归并别名 src 发起编排：解析到 tgt
    out = plan(client, body([ex("src", "A", 1)], **LINE))
    (r,) = out["results"]
    assert r["requested_id"] == "src"
    assert r["canonical_id"] == "tgt"


def test_duplicate_canonical_via_alias_is_422(env):
    client, _ = env
    make_exhibit(client, "src")
    make_exhibit(client, "tgt")
    client.post("/exhibits/src/readings", json={"readings": [
        {"id": "r1", "ts": iso(0), "lux": 1},
    ]})
    token = client.post("/merges/preview",
                        json={"source_id": "src", "target_id": "tgt"}).json()["token"]
    client.post("/merges/commit", json={"token": token, "resolutions": []})

    # src（别名）与 tgt（规范）是同一档案：重复 → 422
    b = body([ex("src", "A", 1), ex("tgt", "A", 1)], **LINE)
    resp = client.post("/evacuation-plan", json=b)
    assert resp.status_code == 422
    assert "重复" in resp.json()["detail"]


def test_same_exhibit_id_listed_twice_is_422(env):
    client, _ = env
    make_exhibit(client, "x")
    b = body([ex("x", "A", 1), ex("x", "A", 2)], **LINE)
    assert client.post("/evacuation-plan", json=b).status_code == 422


def test_unknown_exhibit_is_404(env):
    client, _ = env
    b = body([ex("ghost", "A", 1)], **LINE)
    resp = client.post("/evacuation-plan", json=b)
    assert resp.status_code == 404
    assert "ghost" in resp.json()["detail"]


# ---------- 非法图与无出口路径 → 422 ----------

def test_duplicate_node_is_422(env):
    client, _ = env
    make_exhibit(client, "x")
    b = body(
        [ex("x", "A", 1)],
        nodes=["A", "A", "E"],
        edges=[edge("A", "E", 1)], exits=["E"],
    )
    assert client.post("/evacuation-plan", json=b).status_code == 422


def test_duplicate_exit_is_422(env):
    client, _ = env
    make_exhibit(client, "x")
    b = body([ex("x", "A", 1)], nodes=["A", "E"], edges=[edge("A", "E", 1)],
             exits=["E", "E"])
    assert client.post("/evacuation-plan", json=b).status_code == 422


def test_unknown_exit_node_is_422(env):
    client, _ = env
    make_exhibit(client, "x")
    b = body([ex("x", "A", 1)], nodes=["A"], edges=[], exits=["NOPE"])
    assert client.post("/evacuation-plan", json=b).status_code == 422


def test_unknown_edge_endpoint_is_422(env):
    client, _ = env
    make_exhibit(client, "x")
    b = body([ex("x", "A", 1)], nodes=["A", "E"],
             edges=[edge("A", "NOPE", 1)], exits=["E"])
    assert client.post("/evacuation-plan", json=b).status_code == 422


def test_duplicate_directed_edge_is_422(env):
    client, _ = env
    make_exhibit(client, "x")
    b = body(
        [ex("x", "A", 1)],
        nodes=["A", "E"],
        edges=[edge("A", "E", 1), edge("A", "E", 2)],
        exits=["E"],
    )
    assert client.post("/evacuation-plan", json=b).status_code == 422


def test_reverse_edge_is_allowed(env):
    """反向通道 A->E 与 E->A 是两条不同有向通道，合法。"""
    client, _ = env
    make_exhibit(client, "x")
    b = body(
        [ex("x", "A", 1)],
        nodes=["A", "E"],
        edges=[edge("A", "E", 3), edge("E", "A", 9)],
        exits=["E"],
    )
    out = plan(client, b)
    assert out["results"][0]["completion_offset_seconds"] == 3


def test_unknown_exhibit_start_node_is_422(env):
    client, _ = env
    make_exhibit(client, "x")
    b = body([ex("x", "NOPE", 1)], nodes=["A", "E"],
             edges=[edge("A", "E", 1)], exits=["E"])
    assert client.post("/evacuation-plan", json=b).status_code == 422


def test_no_path_to_exit_is_422(env):
    """起点存在、出口存在，但无任何有向路径可达出口 → 422。"""
    client, _ = env
    make_exhibit(client, "x")
    # 只有 B->A 的反向通道；A 无法到达 B
    b = body(
        [ex("x", "A", 1)],
        nodes=["A", "B"], edges=[edge("B", "A", 1)], exits=["B"],
    )
    resp = client.post("/evacuation-plan", json=b)
    assert resp.status_code == 422
    assert "出口" in resp.json()["detail"]


def test_all_paths_blocked_by_closure_is_still_routed_by_waiting(env):
    """封闭时段是有限的：无绕行时算法等待封闭结束，而不是报无路径。"""
    client, _ = env
    make_exhibit(client, "x")
    b = body(
        [ex("x", "A", 1)],
        nodes=["A", "E"],
        edges=[edge("A", "E", 2, 1, closures=[[0, 100]])],
        exits=["E"],
    )
    out = plan(client, b)
    assert out["results"][0]["completion_offset_seconds"] == 102


def test_invalid_closure_interval_is_422(env):
    client, _ = env
    make_exhibit(client, "x")
    b = body(
        [ex("x", "A", 1)],
        nodes=["A", "E"],
        edges=[edge("A", "E", 2, 1, closures=[[10, 10]])],
        exits=["E"],
    )
    assert client.post("/evacuation-plan", json=b).status_code == 422


def test_non_positive_travel_or_capacity_is_422(env):
    client, _ = env
    make_exhibit(client, "x")
    b1 = body([ex("x", "A", 1)], nodes=["A", "E"],
              edges=[edge("A", "E", 0)], exits=["E"])
    assert client.post("/evacuation-plan", json=b1).status_code == 422
    b2 = body([ex("x", "A", 1)], nodes=["A", "E"],
              edges=[{**edge("A", "E", 1), "capacity": 0}], exits=["E"])
    assert client.post("/evacuation-plan", json=b2).status_code == 422


def test_naive_start_at_treated_as_utc(env):
    client, _ = env
    make_exhibit(client, "x")
    b = body([ex("x", "A", 1)], start_at="2026-09-26T08:00:00", **LINE)
    out = plan(client, b)
    assert out["start_at"] == START


# ---------- 多展品综合：优先级 + 容量 + 封闭 ----------

def test_multi_exhibit_priority_changes_capacity_allocation(env):
    """高优先级展品先占用通道：低优先级即使路径短也要在其后排队。"""
    client, _ = env
    make_exhibit(client, "hi")
    make_exhibit(client, "lo")
    # hi 从远起点 Far 出发：Far->A 3s，再 A->B 10s（容量 1）
    # lo 直接在 A：若先编排便 0 进入；但 hi 优先级高，3 到达后立即占用 [3,13)，
    # lo 只能 13 进入
    b = body(
        [ex("hi", "Far", 10), ex("lo", "A", 1)],
        nodes=["Far", "A", "B"],
        edges=[edge("Far", "A", 3), edge("A", "B", 10, 1)],
        exits=["B"],
    )
    out = plan(client, b)
    by_id = {r["canonical_id"]: r for r in out["results"]}
    assert by_id["hi"]["path"] == ["Far", "A", "B"]
    assert by_id["hi"]["completion_offset_seconds"] == 13
    assert by_id["lo"]["legs"][0]["enter_offset_seconds"] == 13
    assert by_id["lo"]["legs"][0]["wait_seconds"] == 13
    assert by_id["lo"]["completion_offset_seconds"] == 23
    assert out["total_completion_offset_seconds"] == 23


def test_multistage_bottleneck_queue_propagates(env):
    """两级通道容量不同：A->B 容量 2、B->E 容量 1，四件同起点展品的排队级联。

    A->B(2) 与 B->E(2) 均 2 秒：
      e0: A->B@0，B->E@2，4 到达；
      e1: A->B@0，B->E 容量 1 排队到 4，等 2，6 到达；
      e2: A->B 容量 2 排队到 2（等 2），4 到达 B，B->E 排队到 6，8 到达；
      e3: A->B@2，B->E@8，等 4，10 到达。
    """
    client, _ = env
    for i in range(4):
        make_exhibit(client, f"e{i}")
    b = body(
        [ex(f"e{i}", "A", 0) for i in range(4)],
        nodes=["A", "B", "E"],
        edges=[edge("A", "B", 2, 2), edge("B", "E", 2, 1)],
        exits=["E"],
    )
    out = plan(client, b)
    rows = [(r["canonical_id"],
             [(lg["enter_offset_seconds"], lg["wait_seconds"]) for lg in r["legs"]],
             r["completion_offset_seconds"]) for r in out["results"]]
    # 规范 ID 升序编排（同优先级），结果按同序返回
    assert rows == [
        ("e0", [(0, 0), (2, 0)], 4),
        ("e1", [(0, 0), (4, 2)], 6),
        ("e2", [(2, 2), (6, 2)], 8),
        ("e3", [(2, 2), (8, 4)], 10),
    ]
    assert out["total_completion_offset_seconds"] == 10


def test_wait_seconds_summed_per_exhibit(env):
    client, _ = env
    make_exhibit(client, "x")
    # A->B 通行 4、封闭 [0,2)：等到 2，等 2s；B->E 容量 1（无竞争）通行 3
    b = body(
        [ex("x", "A", 1)],
        nodes=["A", "B", "E"],
        edges=[
            edge("A", "B", 4, 1, closures=[[0, 2]]),
            edge("B", "E", 3),
        ],
        exits=["E"],
    )
    out = plan(client, b)
    (r,) = out["results"]
    assert r["legs"][0]["wait_seconds"] == 2
    assert r["legs"][1]["wait_seconds"] == 0
    assert r["wait_seconds"] == 2
    assert r["completion_offset_seconds"] == 9


# ---------- 隔离性：只读，不写任何业务表 ----------

def test_evacuation_is_read_only(env):
    client, db_file = env
    make_exhibit(client, "a")
    make_exhibit(client, "b")
    client.post("/exhibits/a/readings", json={"readings": [
        {"id": "r1", "ts": iso(0), "lux": 100},
    ]})
    chain_before = client.get("/exhibits/a/receipts").json()
    calendar_before = client.get("/exhibits/a/calendar").status_code

    b = body(
        [ex("a", "A", 2), ex("b", "A", 1)],
        nodes=["A", "B", "E"],
        edges=[edge("A", "B", 5), edge("B", "E", 3)],
        exits=["E"],
    )
    plan(client, b)
    plan(client, b)  # 重复调用：纯计算，无副作用、无固化

    # 读数、收据链不变
    readings_after = client.get("/exhibits/a/readings").json()["readings"]
    assert [r["id"] for r in readings_after] == ["r1"]
    chain_after = client.get("/exhibits/a/receipts").json()
    assert chain_after["receipts"] == chain_before["receipts"]
    verify = client.get("/exhibits/a/receipts/verify").json()
    assert verify["ok"] is True and verify["total"] == 1
    # 日历、盘点轮次均无变化/新增
    assert client.get("/exhibits/a/calendar").status_code == calendar_before == 404

    import sqlite3
    conn = sqlite3.connect(str(db_file))
    try:
        tables = {
            r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        }
        assert "evacuation_plans" not in tables
        assert conn.execute("SELECT COUNT(*) FROM readings").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM ingest_receipts").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM exhibit_calendars").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM inventory_rounds").fetchone()[0] == 0
    finally:
        conn.close()
