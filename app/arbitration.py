"""多来源读数仲裁：来源优先级版本与权威时间线构建。

设计要点：
- 读数可带 ``source_id``；同一展品同一采样时刻的不同来源读数全部留存
  （读数 ID 仍是幂等键，``source_id`` 只是读数属性，NULL 表示旧式上报）。
- 来源优先级按**版本**维护：每版携带生效时刻 ``effective_at`` 与来源列表
  （列表顺序即优先级，靠前更高；未列入的来源——含未声明来源的 NULL——
  并列最低）。版本只增不改，配置更新必须携带 ``expected_version``
  （当前最新版本号），过期返回 409。
- 仲裁**逐采样时刻独立**进行：对时刻 t，取 ``effective_at <= t`` 的最新
  版本（version 最大者；没有生效版本时全部读数平权），在生效版本下取
  优先级最高的来源集合：
  * 集合内照度唯一 → 该值为该时刻的**权威值**；
  * 集合内照度不同（最高级并列）→ 该时刻**保持冲突**。冲突时刻只保留在
    权威时间线查询中：不进入剂量积分（两侧权威点绝不跨过它直接相连，
    普通剂量与 ``calendar=display`` 开放时段都在此断开），也不作为诊断
    输入（诊断连续链在两侧重置，且不生成 ``timestamp_conflict``）。
- 优先级调整只从生效时刻起影响重算：历史收据与原始读数行绝不改写，
  仲裁在查询时实时进行。
- 展品没有任何优先级版本时，仲裁退化为原有平权口径（同时刻同值合并、
  异值冲突），旧行为完全不变。
"""
from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass


@dataclass
class PriorityVersion:
    """一版来源优先级配置。"""

    version: int
    effective_at: float      # 生效时刻，Unix 秒（UTC）
    priorities: list[str]    # source_id 按优先级从高到低
    created_at: str


@dataclass
class Member:
    """参与某时刻仲裁的一条读数。"""

    reading_id: str
    source_id: str | None
    lux: float


@dataclass
class ArbitrationPoint:
    """仲裁成功的时刻：胜出集合照度唯一，该值为权威值。"""

    ts: float
    lux: float
    members: list[Member]    # 胜出集合（同照度），可能含多条读数
    version: int | None      # 该时刻生效的优先级版本号；无生效版本为 None


@dataclass
class ArbitrationConflict:
    """保持冲突的时刻：最高优先级并列且照度不同。

    只保留给权威时间线查询；既不作为剂量积分端点（时间线在此时刻断开，
    两侧权威点不直接相连），也不作为诊断输入（诊断链在两侧重置且不生成
    ``timestamp_conflict``）。
    """

    ts: float
    members: list[Member]    # 并列最高优先级的读数集合（照度不唯一）
    version: int | None

    @property
    def lux_span(self) -> float:
        luxs = [m.lux for m in self.members]
        return max(luxs) - min(luxs)


# ---------- 配置读取 ----------

def load_priority_versions(
    conn: sqlite3.Connection, exhibit_id: str
) -> list[PriorityVersion]:
    """按版本号升序读取展品的全部来源优先级版本（可能为空）。"""
    rows = conn.execute(
        "SELECT version, effective_at, priorities, created_at "
        "FROM source_priority_versions WHERE exhibit_id = ? ORDER BY version",
        (exhibit_id,),
    ).fetchall()
    return [
        PriorityVersion(
            version=r["version"],
            effective_at=r["effective_at"],
            priorities=json.loads(r["priorities"]),
            created_at=r["created_at"],
        )
        for r in rows
    ]


def version_for_ts(
    versions: list[PriorityVersion], ts: float
) -> PriorityVersion | None:
    """时刻 ts 生效的版本：effective_at <= ts 中版本号最大者；无则 None。"""
    best: PriorityVersion | None = None
    for v in versions:
        if v.effective_at <= ts and (best is None or v.version > best.version):
            best = v
    return best


def _rank(priorities: list[str], source_id: str | None) -> int:
    """来源在优先级列表中的名次；未列入（含 NULL）并列最低。"""
    if source_id is None:
        return len(priorities)
    try:
        return priorities.index(source_id)
    except ValueError:
        return len(priorities)


# ---------- 仲裁 ----------

def arbitrate(
    readings: list[tuple[str, float, float, str | None]],  # (reading_id, ts, lux, source_id)
    versions: list[PriorityVersion],
) -> tuple[list[ArbitrationPoint], list[ArbitrationConflict]]:
    """逐采样时刻仲裁，返回（权威点列表, 冲突时刻列表），均按 ts 升序。

    ``versions`` 为空时退化为平权口径：同时刻同值合并为点、异值成冲突，
    与多来源功能上线前的行为完全一致。
    """
    ordered = sorted(readings, key=lambda r: (r[1], r[0]))

    # 按时刻分组（浮点 ts 直接相等比较，与诊断模块的分组口径一致）
    groups: list[tuple[float, list[Member]]] = []
    for rid, ts, lux, source_id in ordered:
        if groups and groups[-1][0] == ts:
            groups[-1][1].append(Member(rid, source_id, lux))
        else:
            groups.append((ts, [Member(rid, source_id, lux)]))

    points: list[ArbitrationPoint] = []
    conflicts: list[ArbitrationConflict] = []
    for ts, members in groups:
        version = version_for_ts(versions, ts)
        if version is None:
            winners = members
        else:
            best_rank = min(_rank(version.priorities, m.source_id) for m in members)
            winners = [
                m for m in members
                if _rank(version.priorities, m.source_id) == best_rank
            ]
        version_no = version.version if version is not None else None
        lux_values = {m.lux for m in winners}
        if len(lux_values) == 1:
            points.append(ArbitrationPoint(
                ts=ts, lux=winners[0].lux, members=winners, version=version_no,
            ))
        else:
            conflicts.append(ArbitrationConflict(
                ts=ts, members=winners, version=version_no,
            ))
    return points, conflicts


def diagnosis_feed(points: list[ArbitrationPoint]) -> list[tuple[str, float, float]]:
    """把仲裁成功的权威点展开为诊断模块的输入读数。

    权威点按胜出集合逐条展开（同值，诊断分组会重新合并为一个点）。
    保持冲突的时刻**不进入诊断输入**：它们由 ``diagnosis_breaks`` 作为
    纯断点交给诊断模块——连续链在两侧重置，但不产生 ``timestamp_conflict``
    （仲裁冲突只在权威时间线查询中保留）。被仲裁丢弃的低优先级读数同样
    不进入诊断时间线。
    """
    feed: list[tuple[str, float, float]] = []
    for p in points:
        for m in p.members:
            feed.append((m.reading_id, p.ts, p.lux))
    return feed


def diagnosis_breaks(conflicts: list[ArbitrationConflict]) -> list[float]:
    """仲裁保持冲突的时刻（升序）：诊断链在这些时刻两侧静默重置。"""
    return [c.ts for c in conflicts]


# ---------- 归并辅助 ----------

def versions_fingerprint(versions: list[PriorityVersion]) -> list:
    """版本序列的稳定指纹（归并版本令牌与配置一致性比较共用）。"""
    return [
        (v.version, float.hex(v.effective_at), list(v.priorities))
        for v in versions
    ]


def versions_equal(
    a: list[PriorityVersion], b: list[PriorityVersion]
) -> bool:
    """两档案的优先级配置是否逐版本一致（版本号、生效时刻、优先级列表）。"""
    return versions_fingerprint(a) == versions_fingerprint(b)
