"""展品应急撤离编排：时变最早到达搜索（容量约束 + 半开封闭时段）。

纯函数模块，不接触数据库：通道图、出口与展品起点全部由调用方给出，
别名解析与档案查找在路由层完成。编排是**只读计算**——不写照度读数、
不写日历、不写盘点轮次，也不生成或改写存证收据链。

时间模型
--------
- 所有时刻均为相对“计划起点”的**非负整数秒**（通行秒数、封闭时段均为整数）。
- 通道占用采用**半开区间** ``[进入, 离开)``：展品在 ``t`` 进入通道、
  ``t + travel_seconds`` 离开；恰好在他人离开时刻进入**不冲突**，
  因为离开时刻不计入任何占用区间。
- 通道在封闭时段 ``[cs, ce)`` 内**不可进入**；在 ``cs`` 之前进入、
  通行过程跨过 ``cs`` 是允许的（封闭约束的是“进入”动作，不是通道内停留）；
  在 ``ce`` 整点即可进入。

逐件编排
--------
展品按**优先级降序、规范 ID 升序**逐件编排；每件展品固定在已编排展品的
占用计划之上做一次**时变最早到达搜索**（Dijkstra 标签设定，边代价 FIFO），
选出最早到达的出口与路径；到达时间相同则按**完整路径（节点序列）字典序**
决胜。确定路径后把本件展品的占用区间提交进各通道，再编排下一件。

等待规则
--------
到达某通道起点后若不能立即进入，则等待到**首个可行进入时刻**：
容量按候选窗口内**同一时刻的实际并发**判断（只有加入本件会使某一瞬间
达到容量才等待；多条占用错峰、重叠条数虽多但瞬时并发未满时不等待），
并避开封闭时段；每次因容量或封闭推进候选时刻后都重新检查两类约束，
不会跳过更早的可行空档。
"""
from __future__ import annotations

import heapq
from dataclasses import dataclass, field

# ---------- 图结构 ----------


class EvacuationGraphError(ValueError):
    """图非法：重复通道、端点未知、出口未知等（映射为 422）。"""


@dataclass(frozen=True)
class Edge:
    """一条有向通道。"""

    u: str
    v: str
    travel: int          # 整数通行秒数（≥1）
    capacity: int        # 并发容量（≥1）
    closures: tuple[tuple[int, int], ...]  # 已排序、合并后的半开封闭时段


@dataclass
class EvacuationGraph:
    nodes: frozenset[str]
    exits: frozenset[str]
    adjacency: dict[str, list[Edge]]

    @classmethod
    def build(
        cls,
        nodes: list[str],
        raw_edges: list[tuple[str, str, int, int, list[tuple[int, int]]]],
        exits: list[str],
    ) -> EvacuationGraph:
        """由原始输入构图；``raw_edges`` 元素为
        ``(起点, 终点, 通行秒数, 容量, 封闭时段列表)``。
        """
        node_set = frozenset(nodes)
        exit_set = frozenset(exits)
        missing_exits = sorted(exit_set - node_set)
        if missing_exits:
            raise EvacuationGraphError(f"出口不在节点列表中: {missing_exits}")

        adjacency: dict[str, list[Edge]] = {n: [] for n in nodes}
        seen: set[tuple[str, str]] = set()
        for u, v, travel, capacity, raw_closures in raw_edges:
            if u not in node_set:
                raise EvacuationGraphError(f"通道起点不在节点列表中: {u}")
            if v not in node_set:
                raise EvacuationGraphError(f"通道终点不在节点列表中: {v}")
            if (u, v) in seen:
                raise EvacuationGraphError(f"有向通道重复定义: {u} -> {v}")
            seen.add((u, v))
            adjacency[u].append(Edge(
                u=u, v=v, travel=travel, capacity=capacity,
                closures=tuple(merge_intervals(sorted(raw_closures))),
            ))
        # 邻接表按终点排序：Dijkstra 同代价松弛顺序也由此确定，
        # 配合路径字典序标签，结果与输入通道顺序无关。
        for outs in adjacency.values():
            outs.sort(key=lambda e: (e.v, e.travel))
        return cls(nodes=node_set, exits=exit_set, adjacency=adjacency)

    def can_reach_exit(self, start: str) -> bool:
        """忽略容量与封闭（只看有向连通性）：起点是否能到达任一出口。"""
        if start in self.exits:
            return True
        seen_nodes = {start}
        stack = [start]
        while stack:
            u = stack.pop()
            for e in self.adjacency.get(u, ()):
                if e.v not in seen_nodes:
                    if e.v in self.exits:
                        return True
                    seen_nodes.add(e.v)
                    stack.append(e.v)
        return False


def merge_intervals(intervals: list[tuple[int, int]]) -> list[tuple[int, int]]:
    """合并重叠或首尾相接的整数半开区间；输入须已按起点排序。"""
    merged: list[tuple[int, int]] = []
    for s, e in intervals:
        if merged and s <= merged[-1][1]:
            if e > merged[-1][1]:
                merged[-1] = (merged[-1][0], e)
        else:
            merged.append((s, e))
    return merged


# ---------- 通道占用与最早可行进入时刻 ----------

@dataclass
class CorridorState:
    """一条通道在逐件编排过程中的占用状态。"""

    edge: Edge
    # 已确定编排展品的占用区间 [enter, leave)，按进入时刻排序
    occupied: list[tuple[int, int]] = field(default_factory=list)

    def earliest_entry(self, ready: int) -> int:
        """展品在 ``ready`` 时刻到达通道起点时，最早可进入时刻。

        候选进入时刻 ``t ≥ ready`` 必须同时满足：

        1. **容量**：加入本件占用 ``[t, t+travel)`` 后，窗口内**任一时刻**
           同时在通道内的已编排展品数（实际瞬时并发）小于容量。容量按
           **同一时刻的实际并发**计数，而不是按与本窗口重叠的占用条数：
           多条占用彼此错峰时，重叠条数可能超过容量、瞬时并发却未满，
           此时仍应允许进入。占用按半开区间计数，恰好在 ``t`` 离开的区间
           不计数，在 ``t+travel`` 进入的区间也不计数；
        2. **封闭**：``t`` 不落在任何封闭半开区间 ``[cs, ce)`` 内
           （``t == ce`` 可以进入；进入后跨过封闭开始时刻允许）。

        不可行时把 ``t`` 推进到“挡住它的最早约束的下一可行时刻”：
        容量冲突 → 与窗口重叠的占用区间中最早的离开时刻（窗口内造成饱和
        的那批占用在此时刻之前全部仍未离开，故其间不存在可行整数时刻，
        推进不会跳过更早空档）；封闭冲突 → 该封闭时段结束时刻。两个候选
        取较晚者。每次推进都越过至少一个有限右端点，故迭代必然终止。
        """
        t = ready
        travel = self.edge.travel
        while True:
            leave = t + travel
            # 扫描线求窗口 [t, leave) 内的实际瞬时并发峰值：
            # 与 [t, leave) 半开重叠的占用先裁剪到窗口内，再在每个区间
            # 起点 +1、终点 -1；占用在 t 整点已离开（e == t）不计，下一件
            # 在 leave 整点进入（s == leave）也不计。
            events: dict[int, int] = {}
            earliest_release: int | None = None
            for s, e in self.occupied:
                if s < leave and e > t:
                    if earliest_release is None or e < earliest_release:
                        earliest_release = e
                    events[max(s, t)] = events.get(max(s, t), 0) + 1
                    events[min(e, leave)] = events.get(min(e, leave), 0) - 1

            next_t: int | None = None
            if events:
                concurrent = 0
                peak = 0
                for ts in sorted(events):
                    concurrent += events[ts]
                    if concurrent > peak:
                        peak = concurrent
                if peak >= self.edge.capacity:
                    # 最早一条重叠占用离开之前，造成饱和的那批占用无一释放，
                    # 峰值不会降到容量以下，故至少要等到最早的离开时刻。
                    next_t = earliest_release

            # 封闭检查：进入时刻不得落在 [cs, ce) 内
            for cs, ce in self.edge.closures:
                if cs <= t < ce:
                    if next_t is None or ce > next_t:
                        next_t = ce
                    break  # 区间已合并、互不重叠，至多命中一个
                if cs > t:
                    break  # 已按起点排序，之后的时段更晚，不可能命中

            if next_t is None:
                return t
            t = next_t

    def commit(self, enter: int) -> None:
        """把已确定的占用 [enter, enter+travel) 提交到通道状态。"""
        interval = (enter, enter + self.edge.travel)
        self.occupied.append(interval)
        self.occupied.sort()


# ---------- 时变最早到达搜索 ----------

@dataclass(frozen=True)
class Route:
    """一件展品的编排结果（相对秒）。"""

    path: tuple[str, ...]
    # 每段：(通道, 进入时刻, 离开时刻, 进入前等待秒数)
    legs: tuple[tuple[Edge, int, int, int], ...]
    arrival: int  # 到达出口时刻（相对秒）


def _route_from_chain(chain: list[tuple[Edge, int]], start: str) -> Route:
    """根据 (通道, 进入时刻) 链构造 Route，离开与等待由进入时刻直接推出。"""
    legs: list[tuple[Edge, int, int, int]] = []
    ready = 0
    for edge, enter in chain:
        legs.append((edge, enter, enter + edge.travel, enter - ready))
        ready = enter + edge.travel
    path = (start,) + tuple(e.v for e, _ in chain)
    return Route(path=path, legs=tuple(legs), arrival=ready)


def search_route(
    graph: EvacuationGraph,
    states: dict[tuple[str, str], CorridorState],
    start: str,
) -> Route | None:
    """时变最早到达搜索：从 ``start`` 到任一出口的最早路线。

    边代价 FIFO：晚到通道起点不会得到更早的可行进入时刻
    （``earliest_entry`` 关于 ready 单调不减），因此 Dijkstra 标签设定成立。

    决胜：堆项为 ``(到达时刻, 完整路径元组, 通道进入链)``；到达时刻相同时
    完整路径字典序较小者先被弹出，先弹出的出口标签即最优出口。对同一节点，
    仅在到达时刻严格更早时松弛；到达时刻相同但路径字典序更小的标签也用于
    刷新（到达时刻相同的不同前驱路径可能影响后续路径字典序决胜）。
    """
    if start in graph.exits:
        # 起点即出口：零段路径，立即完成
        return Route(path=(start,), legs=(), arrival=0)

    # node -> (到达时刻, 路径元组, 进入链)；只保留对 (arrival, path) 更优的标签
    best: dict[str, tuple[int, tuple[str, ...], list[tuple[Edge, int]]]] = {}
    heap: list[tuple[int, tuple[str, ...], list[tuple[Edge, int]]]] = [
        (0, (start,), [])
    ]
    best[start] = (0, (start,), [])

    while heap:
        arrival_u, path_u, chain_u = heapq.heappop(heap)
        u = path_u[-1]
        cur = best.get(u)
        # 过期标签：同节点已有 (到达时刻, 路径) 更优的标签
        if cur is not None and (arrival_u, path_u) > (cur[0], cur[1]):
            continue
        if u in graph.exits:
            return _route_from_chain(chain_u, start)

        for edge in graph.adjacency.get(u, ()):
            state = states[(edge.u, edge.v)]
            enter = state.earliest_entry(arrival_u)
            arrive_v = enter + edge.travel
            cand_path = path_u + (edge.v,)
            cand_chain = chain_u + [(edge, enter)]
            old = best.get(edge.v)
            if old is None or (arrive_v, cand_path) < (old[0], old[1]):
                best[edge.v] = (arrive_v, cand_path, cand_chain)
                heapq.heappush(heap, (arrive_v, cand_path, cand_chain))

    return None


# ---------- 逐件编排 ----------

@dataclass(frozen=True)
class PlannedExhibit:
    requested_id: str
    canonical_id: str
    start_node: str
    priority: int
    route: Route


def plan_evacuation(
    graph: EvacuationGraph,
    exhibits: list[tuple[str, str, str, int]],
) -> list[PlannedExhibit]:
    """逐件编排全部展品。

    ``exhibits`` 元素为 ``(请求 ID, 规范 ID, 起点节点, 优先级)``；
    调用方须已完成别名解析、去重与图合法性校验。
    按优先级降序、规范 ID 升序逐件搜索并提交占用；
    起点无任何有向路径可达出口抛 :class:`EvacuationGraphError`。
    """
    # 每条通道一个可变占用状态，跨展品累积
    states: dict[tuple[str, str], CorridorState] = {}
    for outs in graph.adjacency.values():
        for edge in outs:
            states[(edge.u, edge.v)] = CorridorState(edge=edge)

    planned: list[PlannedExhibit] = []
    for requested_id, canonical_id, start_node, priority in sorted(
        exhibits, key=lambda x: (-x[3], x[1])
    ):
        if not graph.can_reach_exit(start_node):
            raise EvacuationGraphError(
                f"展品 {requested_id}（规范档案 {canonical_id}）"
                f"的起点 {start_node} 不存在可达任何出口的有向路径"
            )
        route = search_route(graph, states, start_node)
        assert route is not None  # 连通性已由 can_reach_exit 保证
        for edge, enter, _leave, _wait in route.legs:
            states[(edge.u, edge.v)].commit(enter)
        planned.append(PlannedExhibit(
            requested_id=requested_id,
            canonical_id=canonical_id,
            start_node=start_node,
            priority=priority,
            route=route,
        ))
    return planned
