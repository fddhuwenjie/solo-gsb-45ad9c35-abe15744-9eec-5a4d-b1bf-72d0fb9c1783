"""重复展品档案归并：预演、版本令牌、单事务提交、永久别名。

设计要点：
- 来源 ID 归并后写入 ``exhibit_aliases`` 永久指向目标规范 ID；
  来源名下的旧别名在提交事务内改指向目标，表中始终只有一跳，
  不可能形成别名环。
- 预演令牌固化两边元数据与读数集合的 SHA-256 版本；提交在
  ``BEGIN IMMEDIATE`` 事务内重算哈希，预演后任一侧被写入/修改/删除
  都会导致哈希不一致 → 409，事务回滚，不产生部分变更。
- 同读数 ID 且时间、照度一致的碰撞自动去重；内容不同的碰撞必须逐条
  裁决（keep_target / keep_source / rename_source），未解决不得提交。
- 提交成功后把结果固化在令牌行上，重复使用同一令牌回放原结果。
- 归并不触碰照度上报存证收据链：来源与目标的既有链原样保留、提交前
  分别校验（断链或内容不符即 409 拒绝归并），归并后的新上报继续接入
  目标原链，旧哈希绝不重写。
- 来源优先级配置随归并合并：仅来源有才整体搬迁到目标；双方逐版本一致
  保留目标的一份；双方都有且不一致 → 409 拒绝归并，冲突绝不静默覆盖。
  配置指纹纳入预演版本令牌，预演后任一侧发布新版本同样使令牌失效。
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from datetime import datetime, timezone

from fastapi import HTTPException

from .arbitration import (
    load_priority_versions,
    versions_equal,
    versions_fingerprint,
)
from .receipts import load_chain, verify_chain_rows
from .schemas import (
    MergeCollision,
    MergeCommitOut,
    MergeMetaDiff,
    MergePreviewOut,
    MergeResolution,
    MergeResolutionResult,
)

# ---------- 工具 ----------

def to_iso(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, tz=timezone.utc).isoformat().replace("+00:00", "Z")


def _handover_fingerprint(conn: sqlite3.Connection, canonical_id: str) -> list:
    """档案跨轮区域交接链的指纹：整链逐环（含轮次摘要与区域）按序号排列。

    交接链只增不改，指纹纳入归并预演的元数据版本：预演后任一侧新增
    交接都会使令牌失效（提交 409），保证“单侧有链改挂 / 双侧有链拒绝”
    的判定基于与预演一致的状态。直接查表（而非导入 handovers 模块），
    避免顶层导入环。
    """
    rows = conn.execute(
        "SELECT seq, source_round_id, target_round_id, source_area, target_area, "
        "source_round_digest, target_round_digest FROM area_handovers "
        "WHERE canonical_id = ? ORDER BY seq ASC",
        (canonical_id,),
    ).fetchall()
    return [
        [r["seq"], r["source_round_id"], r["target_round_id"], r["source_area"],
         r["target_area"], r["source_round_digest"], r["target_round_digest"]]
        for r in rows
    ]


def _meta_hash(conn: sqlite3.Connection, row: sqlite3.Row) -> str:
    # 来源优先级配置同样属于档案元数据版本：预演后发布新版本（或配置被合并）
    # 都会使令牌失效，归并提交 409。跨轮交接链同理：预演后新增交接也使令牌失效。
    payload = {
        "id": row["id"],
        "name": row["name"],
        "dose_limit_luxh": float.hex(row["dose_limit_luxh"]),
        "max_interval_seconds": float.hex(row["max_interval_seconds"]),
        "created_at": row["created_at"],
        "source_priority": versions_fingerprint(
            load_priority_versions(conn, row["id"])
        ),
        "area_handovers": _handover_fingerprint(conn, row["id"]),
    }
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, ensure_ascii=False).encode("utf-8")
    ).hexdigest()


def _readings_hash(rows: list[sqlite3.Row]) -> str:    # 作废标记与来源标识同样是读数集合版本的一部分：预演后发生批次更正（作废）
    # 或读数来源变化也使令牌失效。旧库迁移前列可能缺失，用 row.keys() 兜底。
    cols = rows[0].keys() if rows else ()
    payload = [
        (
            r["reading_id"], float.hex(r["ts"]), float.hex(r["lux"]),
            r["voided_at"] if "voided_at" in cols else None,
            r["voided_by"] if "voided_by" in cols else None,
            r["source_id"] if "source_id" in cols else None,
        )
        for r in rows
    ]
    payload.sort()
    return hashlib.sha256(
        json.dumps(payload, ensure_ascii=False).encode("utf-8")
    ).hexdigest()


# ---------- 别名解析 ----------

def resolve_canonical(conn: sqlite3.Connection, exhibit_id: str) -> str | None:
    """把请求 ID（可能是别名）解析为规范档案 ID；不存在返回 None。

    正常情况下别名表只有一跳；这里仍带跳数上限沿链解析，
    任何企图重指回既有别名（可能成环）的操作在提交时即被拒绝。
    """
    cur = exhibit_id
    seen: set[str] = set()
    while cur not in seen:
        seen.add(cur)
        row = conn.execute(
            "SELECT canonical_id FROM exhibit_aliases WHERE alias_id = ?",
            (cur,),
        ).fetchone()
        if row is None:
            break
        cur = row["canonical_id"]
    exists = conn.execute(
        "SELECT 1 FROM exhibits WHERE id = ?", (cur,)
    ).fetchone()
    return cur if exists is not None else None


def aliases_of(conn: sqlite3.Connection, canonical_id: str) -> list[str]:
    rows = conn.execute(
        "SELECT alias_id FROM exhibit_aliases WHERE canonical_id = ? ORDER BY alias_id",
        (canonical_id,),
    ).fetchall()
    return [r["alias_id"] for r in rows]


def _get_canonical_row(conn: sqlite3.Connection, exhibit_id: str) -> sqlite3.Row:
    canonical = resolve_canonical(conn, exhibit_id)
    if canonical is None:
        raise HTTPException(status_code=404, detail=f"展品不存在: {exhibit_id}")
    return conn.execute("SELECT * FROM exhibits WHERE id = ?", (canonical,)).fetchone()


def get_canonical_or_404(conn: sqlite3.Connection, exhibit_id: str) -> sqlite3.Row:
    return _get_canonical_row(conn, exhibit_id)


# ---------- 预演 ----------

def _meta_diff_field(field: str, source: object, target: object) -> MergeMetaDiff | None:
    if source != target:
        return MergeMetaDiff(field=field, source=source, target=target)
    return None


def _versions_brief(versions: list) -> list[dict]:
    """来源优先级版本的可读摘要（预演 metadata 差异用）。"""
    return [
        {
            "version": v.version,
            "effective_at": to_iso(v.effective_at),
            "priorities": list(v.priorities),
        }
        for v in versions
    ]


def create_preview(conn: sqlite3.Connection, requested_source: str,
                   requested_target: str) -> MergePreviewOut:
    if requested_source == requested_target:
        raise HTTPException(status_code=422, detail="source_id 与 target_id 不能相同")

    src = get_canonical_or_404(conn, requested_source)
    tgt = get_canonical_or_404(conn, requested_target)
    source_id, target_id = src["id"], tgt["id"]
    if source_id == target_id:
        # 两个 ID 是同一档案（其中一个可能是别名）：没有可归并的两个档案
        raise HTTPException(
            status_code=422,
            detail=f"{requested_source} 与 {requested_target} 已指向同一档案 {source_id}，无需归并",
        )

    src_rows = conn.execute(
        "SELECT * FROM readings WHERE exhibit_id = ? ORDER BY reading_id", (source_id,)
    ).fetchall()
    tgt_rows = conn.execute(
        "SELECT * FROM readings WHERE exhibit_id = ? ORDER BY reading_id", (target_id,)
    ).fetchall()

    src_map = {r["reading_id"]: r for r in src_rows}
    tgt_map = {r["reading_id"]: r for r in tgt_rows}

    collisions: list[MergeCollision] = []
    auto_dup = 0
    conflicts = 0
    for rid in sorted(src_map.keys() & tgt_map.keys()):
        s, t = src_map[rid], tgt_map[rid]
        if s["ts"] == t["ts"] and s["lux"] == t["lux"]:
            kind = "duplicate"
            auto_dup += 1
        else:
            kind = "conflict"
            conflicts += 1
        collisions.append(MergeCollision(
            reading_id=rid,
            kind=kind,
            source_ts=to_iso(s["ts"]),
            source_lux=s["lux"],
            target_ts=to_iso(t["ts"]),
            target_lux=t["lux"],
        ))

    metadata: dict[str, MergeMetaDiff] = {}
    for field, sv, tv in (
        ("name", src["name"], tgt["name"]),
        ("dose_limit_luxh", src["dose_limit_luxh"], tgt["dose_limit_luxh"]),
        ("max_interval_seconds", src["max_interval_seconds"], tgt["max_interval_seconds"]),
    ):
        diff = _meta_diff_field(field, sv, tv)
        if diff is not None:
            metadata[field] = diff

    # 来源优先级配置：双方都有且不一致时冲突不可静默覆盖（提交将 409）；
    # 仅来源有才在提交时自动搬迁，双方一致则保留目标的一份。
    src_versions = load_priority_versions(conn, source_id)
    tgt_versions = load_priority_versions(conn, target_id)
    priority_conflict = bool(
        src_versions and tgt_versions and not versions_equal(src_versions, tgt_versions)
    )
    if src_versions or tgt_versions:
        if not versions_equal(src_versions, tgt_versions):
            metadata["source_priority"] = MergeMetaDiff(
                field="source_priority",
                source=_versions_brief(src_versions),
                target=_versions_brief(tgt_versions),
            )

    # 跨轮区域交接链：双侧都有链时拒绝归并（提交将 409），单侧有链时
    # 提交把整链改挂到目标，归并后用目标 ID 或来源别名都能查到同一条链。
    # 惰性导入 handovers：该模块反向导入本模块，顶层互导会形成环。
    from .handovers import handover_counts

    ho_counts = handover_counts(conn, source_id, target_id)
    src_ho_count, tgt_ho_count = ho_counts[source_id], ho_counts[target_id]
    handover_conflict = src_ho_count > 0 and tgt_ho_count > 0

    source_only = len(src_map) - len(collisions)

    token = uuid.uuid4().hex
    conn.execute(
        """INSERT INTO merge_previews
           (token, source_id, target_id, source_meta_hash, target_meta_hash,
            source_read_hash, target_read_hash, conflict_count)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            token, source_id, target_id,
            _meta_hash(conn, src), _meta_hash(conn, tgt),
            _readings_hash(list(src_rows)), _readings_hash(list(tgt_rows)),
            conflicts,
        ),
    )
    conn.commit()

    return MergePreviewOut(
        token=token,
        requested_source_id=requested_source,
        requested_target_id=requested_target,
        source_id=source_id,
        target_id=target_id,
        metadata=metadata,
        source_reading_count=len(src_rows),
        target_reading_count=len(tgt_rows),
        collisions=collisions,
        auto_duplicate_count=auto_dup,
        conflict_count=conflicts,
        source_only_count=source_only,
        source_priority_conflict=priority_conflict,
        source_handover_count=src_ho_count,
        target_handover_count=tgt_ho_count,
        area_handover_conflict=handover_conflict,
        ready_to_commit=conflicts == 0 and not priority_conflict and not handover_conflict,
    )


# ---------- 提交 ----------

class _PreviewChanged(Exception):
    """预演后档案被写入/修改/删除，提交必须 409。"""


class _ChainBroken(Exception):
    """来源或目标的存证收据链校验失败（断链/内容不符），归并必须 409 拒绝。"""


class _PriorityConflict(Exception):
    """双方来源优先级配置不一致，归并必须 409 拒绝（冲突不静默覆盖）。"""


class _HandoverChainConflict(Exception):
    """双方都存在跨轮区域交接链，归并必须 409 拒绝，不改动任何交接记录。"""


class _CommitValidation(Exception):
    """裁决不合法或不完整，422。"""


def _validate_resolutions(
    resolutions: list[MergeResolution],
    conflict_ids: set[str],
    target_existing_ids: set[str],
    source_only_ids: set[str],
) -> None:
    # 改名后的新 ID 不能与目标现有读数、即将搬迁的来源独有读数或其他新 ID 撞车
    reserved = target_existing_ids | source_only_ids
    seen: set[str] = set()
    new_ids: set[str] = set()
    for r in resolutions:
        if r.reading_id not in conflict_ids:
            raise _CommitValidation(
                f"读数 {r.reading_id} 不是待裁决的内容碰撞（冲突 ID："
                f"{sorted(conflict_ids) or '无'}）"
            )
        if r.reading_id in seen:
            raise _CommitValidation(f"读数 {r.reading_id} 的裁决重复出现")
        seen.add(r.reading_id)
        if r.action == "rename_source":
            new_id = r.new_id
            assert new_id is not None  # 由 pydantic 模型保证
            if new_id in reserved:
                raise _CommitValidation(
                    f"读数 {r.reading_id} 指定的新 ID {new_id} 与归并后已有的读数 ID 冲突"
                )
            if new_id in new_ids:
                raise _CommitValidation(f"新读数 ID {new_id} 被多次指定")
            new_ids.add(new_id)
    missing = sorted(conflict_ids - seen)
    if missing:
        raise _CommitValidation(f"仍有 {len(missing)} 条碰撞未裁决，未解决不得提交: {missing}")


def commit_merge(conn: sqlite3.Connection, token: str,
                 resolutions: list[MergeResolution]) -> MergeCommitOut:
    """在单个 SQLite 事务中执行归并；同令牌重复提交回放首次结果。"""
    preview = conn.execute(
        "SELECT * FROM merge_previews WHERE token = ?", (token,)
    ).fetchone()
    if preview is None:
        raise HTTPException(status_code=404, detail=f"预演令牌不存在或已失效: {token}")

    # 已提交过：回放首次结果（令牌同时保证版本检查不会再跑）
    if preview["committed_at"] is not None:
        assert preview["result_json"] is not None
        data = json.loads(preview["result_json"])
        return MergeCommitOut(**data, already_committed=True)

    source_id, target_id = preview["source_id"], preview["target_id"]

    # 立即拿写锁，确保“校验 + 搬迁 + 删除来源”原子完成；
    # 预演哈希也在锁内重算，杜绝预演后的并发写入。
    try:
        conn.execute("BEGIN IMMEDIATE")
    except sqlite3.Error as e:  # 极端情况下前序语句残留事务
        raise HTTPException(status_code=500, detail=f"无法开启归并事务: {e}")

    try:
        # 锁内重读：可能另一个请求已用同令牌完成提交
        preview = conn.execute(
            "SELECT * FROM merge_previews WHERE token = ?", (token,)
        ).fetchone()
        if preview is None:
            raise HTTPException(status_code=404, detail=f"预演令牌不存在或已失效: {token}")
        if preview["committed_at"] is not None:
            assert preview["result_json"] is not None
            conn.commit()  # 此时尚未写入，COMMIT 仅释放 IMMEDIATE 锁
            return MergeCommitOut(**json.loads(preview["result_json"]), already_committed=True)

        src = conn.execute("SELECT * FROM exhibits WHERE id = ?", (source_id,)).fetchone()
        tgt = conn.execute("SELECT * FROM exhibits WHERE id = ?", (target_id,)).fetchone()
        if src is None or tgt is None:
            raise _PreviewChanged("预演后来源或目标档案已被删除")

        src_rows = conn.execute(
            "SELECT * FROM readings WHERE exhibit_id = ?", (source_id,)
        ).fetchall()
        tgt_rows = conn.execute(
            "SELECT * FROM readings WHERE exhibit_id = ?", (target_id,)
        ).fetchall()
        if (
            _meta_hash(conn, src) != preview["source_meta_hash"]
            or _meta_hash(conn, tgt) != preview["target_meta_hash"]
            or _readings_hash(list(src_rows)) != preview["source_read_hash"]
            or _readings_hash(list(tgt_rows)) != preview["target_read_hash"]
        ):
            raise _PreviewChanged("预演后来源或目标档案发生写入或修改，预演结果已过期")

        # 归并前分别校验来源与目标的既有存证收据链：任一断链或内容不符即
        # 拒绝归并，事务回滚、不产生部分变更。两条链在归并后原样保留，
        # 旧哈希绝不重写，归并后的新上报继续接入目标原链。
        for cid in (source_id, target_id):
            verdict = verify_chain_rows(load_chain(conn, cid))
            if not verdict.ok:
                failure = verdict.first_failure or {}
                raise _ChainBroken(
                    f"档案 {cid} 的存证收据链校验失败：第 {failure.get('seq')} 张"
                    f"（{failure.get('reason')}），归并被拒绝"
                )

        # 来源优先级配置合并：仅来源有才整体搬迁到目标（版本号保持）；
        # 双方一致保留目标的一份；双方都有且不一致 → 409 拒绝归并，
        # 冲突绝不静默覆盖。来源档案删除时其版本随外键级联清除。
        src_versions = load_priority_versions(conn, source_id)
        tgt_versions = load_priority_versions(conn, target_id)
        if src_versions and tgt_versions:
            if not versions_equal(src_versions, tgt_versions):
                raise _PriorityConflict(
                    "来源与目标的来源优先级配置不一致（双方均已配置版本），"
                    "归并被拒绝：配置冲突不会静默覆盖，请先把两边配置调整为一致"
                    "或清空其中一方后重新预演"
                )
        elif src_versions:
            for v in src_versions:
                conn.execute(
                    "INSERT INTO source_priority_versions "
                    "(exhibit_id, version, effective_at, priorities, created_at) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (target_id, v.version, v.effective_at,
                     json.dumps(v.priorities, ensure_ascii=False), v.created_at),
                )

        # 跨轮区域交接链：双方都有链 → 409 拒绝归并且不改动任何记录
        # （两条链无法在不丢失“当前去向”语义的前提下合并）。单侧有链时，
        # 待来源删除、别名落库后把来源链整链改挂到目标（见下方 rebind）。
        from .handovers import handover_counts, rebind_handover_chain

        ho_counts = handover_counts(conn, source_id, target_id)
        if ho_counts[source_id] > 0 and ho_counts[target_id] > 0:
            raise _HandoverChainConflict(
                f"来源档案 {source_id} 与目标档案 {target_id} 都存在跨轮区域交接链"
                f"（分别为 {ho_counts[source_id]} 笔与 {ho_counts[target_id]} 笔），"
                "归并被拒绝且不改动任何交接记录：请先确认以哪条链为准"
            )

        src_map = {r["reading_id"]: r for r in src_rows}
        tgt_map = {r["reading_id"]: r for r in tgt_rows}
        shared = src_map.keys() & tgt_map.keys()
        conflict_ids = {
            rid for rid in shared
            if src_map[rid]["ts"] != tgt_map[rid]["ts"] or src_map[rid]["lux"] != tgt_map[rid]["lux"]
        }
        dup_ids = shared - conflict_ids
        source_only_ids = set(src_map) - shared

        _validate_resolutions(
            resolutions,
            conflict_ids=conflict_ids,
            # rename 的新 ID 不得与目标侧任何现存读数或即将搬迁的来源独有读数冲突
            target_existing_ids=set(tgt_map),
            source_only_ids=source_only_ids,
        )
        res_by_id = {r.reading_id: r for r in resolutions}

        # 1) 逐条搬迁来源读数
        moved = 0
        applied: list[MergeResolutionResult] = []
        for rid in sorted(src_map):
            s = src_map[rid]
            if rid in dup_ids:
                # 完全一致：保留目标，丢弃来源副本（自动去重）
                continue
            if rid in conflict_ids:
                decision = res_by_id[rid]
                if decision.action == "keep_target":
                    applied.append(MergeResolutionResult(reading_id=rid, action="keep_target"))
                    continue
                if decision.action == "keep_source":
                    conn.execute(
                        "UPDATE readings SET ts = ?, lux = ?, source_id = ? "
                        "WHERE exhibit_id = ? AND reading_id = ?",
                        (s["ts"], s["lux"], s["source_id"], target_id, rid),
                    )
                    applied.append(MergeResolutionResult(reading_id=rid, action="keep_source"))
                    continue
                # rename_source：以新 ID 插入来源内容
                new_id = decision.new_id
                assert new_id is not None
                conn.execute(
                    "INSERT INTO readings (exhibit_id, reading_id, ts, lux, source_id, "
                    "received_at, voided_at, voided_by) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (target_id, new_id, s["ts"], s["lux"], s["source_id"], s["received_at"],
                     s["voided_at"], s["voided_by"]),
                )
                applied.append(
                    MergeResolutionResult(reading_id=rid, action="rename_source", new_id=new_id)
                )
                continue
            # 仅来源存在：整体搬迁，保留读数 ID、来源标识、原始接收时刻与作废标记
            conn.execute(
                "INSERT INTO readings (exhibit_id, reading_id, ts, lux, source_id, "
                "received_at, voided_at, voided_by) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (target_id, rid, s["ts"], s["lux"], s["source_id"], s["received_at"],
                 s["voided_at"], s["voided_by"]),
            )
            moved += 1

        # 2) 来源名下的旧别名改指向目标（保持一跳）；
        #    拒绝把任何已有别名链重指回别名（那只会从既有别名出发，不可能成环，
        #    但 canonical 列必须指向真实档案，这里统一约束为目标 ID）。
        conn.execute(
            "UPDATE exhibit_aliases SET canonical_id = ? WHERE canonical_id = ?",
            (target_id, source_id),
        )
        # 3) 来源 ID 成为目标的永久别名。alias_id 若已存在，
        #    只可能指向第三方档案（主键冲突）——这种情况理论上不可能，
        #    因为 source 本身是规范 ID；用 INSERT OR FAIL 快速失败。
        conn.execute(
            "INSERT INTO exhibit_aliases (alias_id, canonical_id) VALUES (?, ?)",
            (source_id, target_id),
        )
        # 3b) 单侧已有交接链：整链改挂到目标（序号与证据行不变）。
        #     必须在删除来源档案之前完成——交接链对 exhibits 是
        #     ON DELETE RESTRICT，且改挂后不再有行指向即将删除的来源。
        carried_handovers = rebind_handover_chain(conn, source_id, target_id)
        # 4) 删除来源档案读数与来源档案本身（须在别名落库之后）
        conn.execute("DELETE FROM readings WHERE exhibit_id = ?", (source_id,))
        conn.execute("DELETE FROM exhibits WHERE id = ?", (source_id,))

        final_count = conn.execute(
            "SELECT COUNT(*) AS n FROM readings WHERE exhibit_id = ?", (target_id,)
        ).fetchone()["n"]
        aliases = aliases_of(conn, target_id)
        # 双方既有收据链原样保留（归并不触碰收据表），列出链标识供分别核查
        receipt_chains = [
            cid for cid in (source_id, target_id)
            if conn.execute(
                "SELECT 1 FROM ingest_receipts WHERE chain_id = ? LIMIT 1", (cid,)
            ).fetchone() is not None
        ]

        result = MergeCommitOut(
            token=token,
            source_id=source_id,
            target_id=target_id,
            name=tgt["name"],
            dose_limit_luxh=tgt["dose_limit_luxh"],
            max_interval_seconds=tgt["max_interval_seconds"],
            moved_count=moved,
            auto_deduplicated_count=len(dup_ids),
            resolutions_applied=applied,
            final_reading_count=final_count,
            aliases=aliases,
            receipt_chains=receipt_chains,
            handover_chain_carried=carried_handovers > 0,
            already_committed=False,
        )
        # 5) 固化结果供同令牌重放；先写结果再置 committed_at
        conn.execute(
            "UPDATE merge_previews SET result_json = ?, committed_at = "
            "strftime('%Y-%m-%dT%H:%M:%fZ', 'now') WHERE token = ?",
            (json.dumps(result.model_dump(exclude={"already_committed"}),
                        ensure_ascii=False), token),
        )
        conn.commit()
        return result

    except _PreviewChanged as e:
        conn.rollback()
        raise HTTPException(status_code=409, detail=f"归并预演已失效：{e}")
    except _ChainBroken as e:
        conn.rollback()
        raise HTTPException(status_code=409, detail=str(e))
    except _PriorityConflict as e:
        conn.rollback()
        raise HTTPException(status_code=409, detail=str(e))
    except _HandoverChainConflict as e:
        conn.rollback()
        raise HTTPException(status_code=409, detail=str(e))
    except _CommitValidation as e:
        conn.rollback()
        raise HTTPException(status_code=422, detail=str(e))
    except sqlite3.IntegrityError as e:
        conn.rollback()
        # 搬迁/改名与目标读数主键撞车等：数据层兜底，不产生部分变更
        raise HTTPException(status_code=422, detail=f"归并被拒绝（读数 ID 冲突或别名冲突）: {e}")
    except HTTPException:
        conn.rollback()
        raise
    except Exception:
        conn.rollback()
        raise
