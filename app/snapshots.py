"""SQLite 在线快照：事务一致副本的创建、原子发布、下载与只读校验。

设计要点：
- 创建时用 :meth:`sqlite3.Connection.backup` 在线热备线上库，得到**事务一致**
  的数据库副本；副本先写临时文件（``*.db.tmp``），备份完成后把目标库切回
  ``journal_mode=DELETE`` 并关闭连接，再流式计算 SHA-256、读取页数与字节数，
  生成清单（``*.json``，内容含快照编号、创建时间、页数、字节数、SHA-256）。
- **原子发布**：db 与清单都先写临时文件再 ``os.replace`` 重命名；清单是发布
  标记——只有 db 与清单同时存在且清单可解析的快照才会出现在列表中，任何失败
  或未完成文件（含崩溃残留的 ``*.tmp``）一律不会出现。
- **并发创建**：进程内非阻塞锁，同一时刻只允许一个创建请求执行，其余立即
  返回 409。
- **校验**完全在**独立的只读连接**（``file:`` URI + ``mode=ro`` +
  ``query_only``）上对快照文件进行：复核文件 SHA-256、字节数、页数，并执行
  ``PRAGMA integrity_check`` 与 ``PRAGMA foreign_key_check``，逐项返回结果；
  校验不打开也不修改线上数据库，也不修改快照文件。
- 备份目标关闭前切回 rollback journal 模式，保证每个快照是**单一自包含文件**
  （无 -wal/-shm 旁路文件），可直接下载并用只读连接打开。
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote

from fastapi import HTTPException

from .db import connect
from .schemas import SnapshotCheckOut, SnapshotOut, SnapshotVerifyOut

# 同一进程内同一时刻只允许一个快照创建请求；非阻塞获取，抢不到即 409。
_create_lock = threading.Lock()

_HASH_CHUNK = 1024 * 1024
# 备份每批复制的页数与批间休眠：分批让出锁，避免长时间阻塞线上写入。
_BACKUP_PAGES = 200
_BACKUP_SLEEP = 0.05

_MANIFEST_RE = re.compile(r"^(\d+)\.json$")


# ---------- 路径与环境 ----------

def snapshot_dir() -> Path:
    """快照目录：环境变量 SNAPSHOT_DIR，默认 ./snapshots。"""
    return Path(os.environ.get("SNAPSHOT_DIR", "snapshots"))


def _db_path(seq: int) -> Path:
    return snapshot_dir() / f"{seq}.db"


def _manifest_path(seq: int) -> Path:
    return snapshot_dir() / f"{seq}.json"


def _ensure_dir() -> None:
    snapshot_dir().mkdir(parents=True, exist_ok=True)


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


# ---------- 清单 ----------

def _read_manifest(seq: int) -> dict | None:
    """读取并校验清单；要求字段齐全、编号一致、对应 db 文件存在，否则视为未发布。"""
    path = _manifest_path(seq)
    if not path.is_file() or not _db_path(seq).is_file():
        return None
    try:
        meta = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    if not isinstance(meta, dict):
        return None
    if meta.get("seq") != seq:
        return None
    if not isinstance(meta.get("sha256"), str) or len(meta["sha256"]) != 64:
        return None
    if not isinstance(meta.get("created_at"), str) or not meta["created_at"]:
        return None
    for key in ("pages", "bytes"):
        if not isinstance(meta.get(key), int) or isinstance(meta[key], bool) or meta[key] < 0:
            return None
    return meta


def _snapshot_out(meta: dict) -> SnapshotOut:
    seq = int(meta["seq"])
    return SnapshotOut(
        seq=seq,
        created_at=meta["created_at"],
        sha256=meta["sha256"],
        pages=meta["pages"],
        bytes=meta["bytes"],
        download_url=f"/snapshots/{seq}/download",
    )


# ---------- 创建 ----------

def _copy_backup(source: sqlite3.Connection, dest: sqlite3.Connection) -> None:
    """在线热备：独立函数便于测试模拟耗时 / 失败。

    sqlite3 的 backup 以只读方式在事务一致点逐步复制全部页，
    复制期间线上库可继续读取与写入。
    """
    source.backup(dest, pages=_BACKUP_PAGES, sleep=_BACKUP_SLEEP)


def _sha256_file(path: Path) -> tuple[str, int]:
    """流式计算文件 SHA-256，同时返回字节数。"""
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as f:
        while True:
            chunk = f.read(_HASH_CHUNK)
            if not chunk:
                break
            digest.update(chunk)
            size += len(chunk)
    return digest.hexdigest(), size


def _fsync_dir(path: Path) -> None:
    try:
        fd = os.open(str(path), os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def cleanup_staging() -> None:
    """清理快照目录中的临时文件（启动时与创建前调用，处理上次崩溃的残留）。

    只有 *.tmp 临时文件会被删除；已发布的 db / 清单绝不触碰。
    """
    directory = snapshot_dir()
    if not directory.is_dir():
        return
    for entry in directory.iterdir():
        if entry.is_file() and entry.name.endswith(".tmp"):
            try:
                entry.unlink()
            except OSError:
                pass


def _next_seq() -> int:
    """编号 = 已发布清单的最大编号 + 1（仅由持锁线程调用）。"""
    max_seq = 0
    directory = snapshot_dir()
    if directory.is_dir():
        for entry in directory.iterdir():
            m = _MANIFEST_RE.match(entry.name)
            if m is not None and _read_manifest(int(m.group(1))) is not None:
                max_seq = max(max_seq, int(m.group(1)))
    return max_seq + 1


def create_snapshot() -> SnapshotOut:
    """创建一份事务一致的在线快照；已有创建任务进行中时抛 409。"""
    if not _create_lock.acquire(blocking=False):
        raise HTTPException(status_code=409, detail="已有快照创建任务正在执行，请稍后重试")

    tmp_db = tmp_manifest = None
    try:
        _ensure_dir()
        # 清掉崩溃残留的临时文件，避免干扰本次编号与发布。
        cleanup_staging()

        seq = _next_seq()
        tmp_db = snapshot_dir() / f"{seq}.db.tmp"
        tmp_manifest = snapshot_dir() / f"{seq}.json.tmp"

        # 来源：线上库的独立连接；目标：临时文件。backup 负责事务一致性，
        # 无需显式事务，复制期间线上读写不受影响。
        source = connect()
        dest = sqlite3.connect(str(tmp_db))
        try:
            _copy_backup(source, dest)
            # 目标库默认继承来源的 WAL 模式；切回 DELETE 使快照成为单一
            # 自包含文件（关闭后无 -wal/-shm 残留），只读校验才能直接打开。
            mode = dest.execute("PRAGMA journal_mode=DELETE").fetchone()[0]
            if str(mode).lower() != "delete":
                raise sqlite3.DatabaseError(
                    f"快照临时库无法切换为单文件 journal 模式（实际 {mode}）"
                )
            pages = int(dest.execute("PRAGMA page_count").fetchone()[0])
        finally:
            dest.close()
            source.close()

        # 关闭后可能残留的 WAL 旁路文件（正常情况下已随 DELETE 模式清除）。
        for suffix in ("-wal", "-shm"):
            sidecar = snapshot_dir() / f"{seq}.db{suffix}"
            if sidecar.is_file():
                sidecar.unlink()

        sha, size = _sha256_file(tmp_db)
        meta = {
            "seq": seq,
            "created_at": _utc_now_iso(),
            "sha256": sha,
            "pages": pages,
            "bytes": size,
        }

        # 清单先落临时文件并 fsync，再依次原子重命名 db 与清单：
        # 清单重命名完成是对外可见的“发布点”。
        tmp_manifest.write_text(
            json.dumps(meta, ensure_ascii=False, sort_keys=True), encoding="utf-8"
        )
        with tmp_manifest.open("rb") as f:
            os.fsync(f.fileno())
        with tmp_db.open("rb") as f:
            os.fsync(f.fileno())
        os.replace(tmp_db, _db_path(seq))
        os.replace(tmp_manifest, _manifest_path(seq))
        _fsync_dir(snapshot_dir())
        tmp_db = tmp_manifest = None
        return _snapshot_out(meta)
    except HTTPException:
        raise
    except Exception as exc:  # 任何失败都清理临时文件，失败快照绝不进入列表
        raise HTTPException(status_code=500, detail=f"快照创建失败: {exc}") from exc
    finally:
        for tmp in (tmp_db, tmp_manifest):
            if tmp is not None:
                try:
                    tmp.unlink()
                except OSError:
                    pass
        _create_lock.release()


# ---------- 列表 / 单个 ----------

def list_snapshots() -> list[SnapshotOut]:
    """按编号升序列出已发布快照；未完成 / 损坏文件一律不出现。"""
    directory = snapshot_dir()
    if not directory.is_dir():
        return []
    out: list[SnapshotOut] = []
    for entry in directory.iterdir():
        m = _MANIFEST_RE.match(entry.name)
        if m is None:
            continue
        meta = _read_manifest(int(m.group(1)))
        if meta is not None:
            out.append(_snapshot_out(meta))
    out.sort(key=lambda s: s.seq)
    return out


def get_snapshot(seq: int) -> SnapshotOut | None:
    meta = _read_manifest(seq)
    return _snapshot_out(meta) if meta is not None else None


def snapshot_db_path(seq: int) -> Path | None:
    """已发布快照的 db 路径；不存在或未发布返回 None。"""
    if _read_manifest(seq) is None:
        return None
    return _db_path(seq)


# ---------- 只读校验 ----------

def _check(name: str, ok: bool, **detail: object) -> SnapshotCheckOut:
    return SnapshotCheckOut(name=name, ok=ok, detail=detail)


def verify_snapshot(seq: int) -> SnapshotVerifyOut | None:
    """在独立只读连接上逐项校验已发布快照；未发布返回 None（路由转 404）。

    校验项：文件 SHA-256、字节数、页数（PRAGMA page_count）、
    PRAGMA integrity_check、PRAGMA foreign_key_check。
    全程只读，不触碰线上数据库。
    """
    meta = _read_manifest(seq)
    if meta is None:
        return None
    db_file = _db_path(seq)
    expected_hash = meta["sha256"]
    expected_bytes = meta["bytes"]
    expected_pages = meta["pages"]

    checks: list[SnapshotCheckOut] = []

    # 1) 文件哈希与字节数（逐块读取，不加载整库）
    actual_hash, actual_bytes = _sha256_file(db_file)
    checks.append(_check(
        "sha256", actual_hash == expected_hash,
        expected=expected_hash, actual=actual_hash,
        algorithm="SHA-256",
    ))
    checks.append(_check(
        "bytes", actual_bytes == expected_bytes,
        expected=expected_bytes, actual=actual_bytes,
    ))

    # 2) 独立只读连接：URI mode=ro + query_only，结构上不可能写任何库文件
    uri = f"file:{quote(str(db_file.resolve()))}?mode=ro"
    conn = sqlite3.connect(uri, uri=True, check_same_thread=False)
    try:
        conn.execute("PRAGMA query_only = ON")
        actual_pages = int(conn.execute("PRAGMA page_count").fetchone()[0])
        checks.append(_check(
            "page_count", actual_pages == expected_pages,
            expected=expected_pages, actual=actual_pages,
        ))

        integrity_rows = [row[0] for row in conn.execute("PRAGMA integrity_check")]
        integrity_ok = integrity_rows == ["ok"]
        checks.append(_check(
            "integrity_check", integrity_ok,
            result=integrity_rows if not integrity_ok else ["ok"],
        ))

        # PRAGMA foreign_key_check 每行：(子表, 行 rowid, 父表, 外键序号)
        fk_rows = [tuple(row) for row in conn.execute("PRAGMA foreign_key_check")]
        checks.append(_check(
            "foreign_key_check", len(fk_rows) == 0,
            violations=[
                {"table": r[0], "rowid": r[1], "parent": r[2], "fkid": r[3]}
                for r in fk_rows
            ],
        ))
    finally:
        conn.close()

    return SnapshotVerifyOut(
        seq=seq,
        ok=all(c.ok for c in checks),
        snapshot=_snapshot_out(meta),
        checks=checks,
    )
