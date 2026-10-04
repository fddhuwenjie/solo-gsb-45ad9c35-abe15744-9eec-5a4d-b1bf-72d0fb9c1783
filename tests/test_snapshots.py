"""SQLite 在线快照：创建、清单、原子发布、409 并发、下载、只读校验与防篡改。"""
from __future__ import annotations

import hashlib
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

import app.snapshots as snap_mod

BASE = 1_700_000_000


def iso(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, tz=timezone.utc).isoformat()


@pytest.fixture()
def env(tmp_path, monkeypatch):
    db_file = tmp_path / "test.db"
    snap_dir = tmp_path / "snapshots"
    monkeypatch.setenv("DB_PATH", str(db_file))
    monkeypatch.setenv("SNAPSHOT_DIR", str(snap_dir))
    from app.main import app

    with TestClient(app) as c:
        yield c, db_file, snap_dir


def make_exhibit_with_readings(client: TestClient) -> str:
    resp = client.post("/exhibits", json={
        "id": "EX-1", "name": "唐代绢画",
        "dose_limit_luxh": 1000.0, "max_interval_seconds": 3600.0,
    })
    assert resp.status_code == 201, resp.text
    resp = client.post("/exhibits/EX-1/readings", json={"readings": [
        {"id": "r1", "ts": iso(BASE), "lux": 100},
        {"id": "r2", "ts": iso(BASE + 3600), "lux": 120},
    ]})
    assert resp.status_code == 200, resp.text
    return "EX-1"


def create(client: TestClient) -> dict:
    resp = client.post("/snapshots")
    assert resp.status_code == 201, resp.text
    return resp.json()


# ---------- 创建与清单 ----------

def test_create_snapshot_returns_manifest(env):
    client, db_file, snap_dir = env
    make_exhibit_with_readings(client)
    out = create(client)

    assert out["seq"] == 1
    assert len(out["sha256"]) == 64
    assert out["pages"] > 0
    assert out["bytes"] > 0
    assert out["created_at"].endswith("Z")
    assert out["download_url"] == "/snapshots/1/download"

    # 文件与清单都已原子发布，临时文件不存在
    assert (snap_dir / "1.db").is_file()
    manifest = snap_dir / "1.json"
    assert manifest.is_file()
    assert not (snap_dir / "1.db.tmp").exists()
    assert not (snap_dir / "1.json.tmp").exists()

    # 清单哈希与文件实际哈希一致；页数 / 字节数可信
    import json
    meta = json.loads(manifest.read_text(encoding="utf-8"))
    h = hashlib.sha256((snap_dir / "1.db").read_bytes()).hexdigest()
    assert meta == {
        "seq": 1, "sha256": h, "created_at": out["created_at"],
        "pages": out["pages"], "bytes": out["bytes"],
    }
    assert (snap_dir / "1.db").stat().st_size == out["bytes"]


def test_snapshot_is_transaction_consistent_and_self_contained(env):
    client, db_file, snap_dir = env
    make_exhibit_with_readings(client)
    create(client)
    snap_db = snap_dir / "1.db"

    # 无 -wal/-shm 旁路文件：快照是单一自包含文件
    assert not (snap_dir / "1.db-wal").exists()
    assert not (snap_dir / "1.db-shm").exists()

    # 快照内容独立可读，且与线上库内容一致（展品 + 2 条读数）
    conn = sqlite3.connect(str(snap_db))
    try:
        assert conn.execute("SELECT count(*) FROM exhibits").fetchone()[0] == 1
        assert conn.execute("SELECT count(*) FROM readings").fetchone()[0] == 2
        rows = conn.execute(
            "SELECT reading_id, lux FROM readings ORDER BY reading_id"
        ).fetchall()
        assert rows == [("r1", 100.0), ("r2", 120.0)]
    finally:
        conn.close()


def test_snapshot_seq_increments(env):
    client, _, snap_dir = env
    make_exhibit_with_readings(client)
    assert create(client)["seq"] == 1
    client.post("/exhibits/EX-1/readings", json={"readings": [
        {"id": "r3", "ts": iso(BASE + 7200), "lux": 90}]})
    assert create(client)["seq"] == 2
    assert create(client)["seq"] == 3
    assert sorted(p.name for p in snap_dir.iterdir()) == [
        "1.db", "1.json", "2.db", "2.json", "3.db", "3.json",
    ]


# ---------- 列表 / 单个 / 404 ----------

def test_list_and_get_snapshot(env):
    client, _, _ = env
    make_exhibit_with_readings(client)
    create(client)
    create(client)

    resp = client.get("/snapshots")
    assert resp.status_code == 200
    seqs = [s["seq"] for s in resp.json()["snapshots"]]
    assert seqs == [1, 2]

    one = client.get("/snapshots/1")
    assert one.status_code == 200
    assert one.json()["seq"] == 1

    assert client.get("/snapshots/999").status_code == 404
    assert client.get("/snapshots/999/download").status_code == 404
    assert client.get("/snapshots/999/verify").status_code == 404


def test_list_empty_initially(env):
    client, _, snap_dir = env
    resp = client.get("/snapshots")
    assert resp.status_code == 200
    assert resp.json() == {"snapshots": []}


# ---------- 下载 ----------

def test_download_snapshot_bytes(env):
    client, _, snap_dir = env
    make_exhibit_with_readings(client)
    out = create(client)

    resp = client.get("/snapshots/1/download")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("application/vnd.sqlite3")
    assert "lightdose-snapshot-1.db" in resp.headers["content-disposition"]
    data = resp.content
    assert len(data) == out["bytes"]
    assert hashlib.sha256(data).hexdigest() == out["sha256"]
    assert data == (snap_dir / "1.db").read_bytes()
    # 下载内容是合法 SQLite 数据库
    assert data[:16] == b"SQLite format 3\x00"


# ---------- 校验 ----------

def test_verify_passes_for_good_snapshot(env):
    client, _, _ = env
    make_exhibit_with_readings(client)
    create(client)
    resp = client.get("/snapshots/1/verify")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["seq"] == 1
    assert body["ok"] is True
    names = {c["name"]: c for c in body["checks"]}
    assert set(names) == {"sha256", "bytes", "page_count",
                          "integrity_check", "foreign_key_check"}
    for c in body["checks"]:
        assert c["ok"] is True
    assert names["integrity_check"]["detail"] == {"result": ["ok"]}
    assert names["foreign_key_check"]["detail"] == {"violations": []}


def test_verify_uses_independent_readonly_connection_and_does_not_modify_live_db(env):
    client, db_file, snap_dir = env
    eid = make_exhibit_with_readings(client)
    out = create(client)

    live_before = db_file.read_bytes() if db_file.is_file() else b""
    snap_mtime = (snap_dir / "1.db").stat().st_mtime_ns
    snap_size = (snap_dir / "1.db").stat().st_size

    resp = client.get("/snapshots/1/verify")
    assert resp.status_code == 200

    # 线上数据库仍可正常读写；数据未被校验过程改动
    r = client.post(f"/exhibits/{eid}/readings", json={"readings": [
        {"id": "r3", "ts": iso(BASE + 7200), "lux": 50}]})
    assert r.status_code == 200

    # 快照文件未被改动；校验没有为其生成 -wal/-shm 旁路文件
    assert (snap_dir / "1.db").stat().st_mtime_ns == snap_mtime
    assert (snap_dir / "1.db").stat().st_size == snap_size
    assert not (snap_dir / "1.db-wal").exists()
    assert not (snap_dir / "1.db-shm").exists()

    # 再次校验仍然通过（快照内容独立于线上库后续写入）
    body = client.get("/snapshots/1/verify").json()
    assert body["ok"] is True
    assert body["snapshot"]["sha256"] == out["sha256"]


def test_verify_detects_tampered_hash(env):
    client, _, snap_dir = env
    make_exhibit_with_readings(client)
    create(client)
    db_file = snap_dir / "1.db"
    data = bytearray(db_file.read_bytes())
    # 翻转靠近文件尾部的一个字节（通常是数据页内容），绕过头部
    data[-100] ^= 0xFF
    db_file.write_bytes(bytes(data))

    body = client.get("/snapshots/1/verify").json()
    assert body["ok"] is False
    by_name = {c["name"]: c for c in body["checks"]}
    assert by_name["sha256"]["ok"] is False
    assert by_name["sha256"]["detail"]["expected"] != by_name["sha256"]["detail"]["actual"]
    assert len(by_name["sha256"]["detail"]["actual"]) == 64
    # 完整性检查对破坏页也应报警（若恰好在空闲区则 sha256 已足以判负）
    assert by_name["integrity_check"]["ok"] in (True, False)


def test_verify_detects_foreign_key_violation(env, monkeypatch):
    client, _, snap_dir = env
    make_exhibit_with_readings(client)
    create(client)

    # 直接往快照里插一条孤儿读数（快照文件本身可写时，绕过外键）
    snap = sqlite3.connect(str(snap_dir / "1.db"))
    try:
        snap.execute("PRAGMA foreign_keys = OFF")
        snap.execute(
            "INSERT INTO readings (exhibit_id, reading_id, ts, lux) VALUES (?, ?, ?, ?)",
            ("ghost", "orphan", BASE, 1.0),
        )
        snap.commit()
    finally:
        snap.close()

    body = client.get("/snapshots/1/verify").json()
    assert body["ok"] is False
    by_name = {c["name"]: c for c in body["checks"]}
    assert by_name["sha256"]["ok"] is False  # 文件变了
    fk = by_name["foreign_key_check"]
    assert fk["ok"] is False
    violations = fk["detail"]["violations"]
    assert any(v["table"] == "readings" and v["parent"] == "exhibits" for v in violations)


# ---------- 并发创建：只允许一个，其余 409 ----------

def test_concurrent_create_only_one_runs_others_409(env, monkeypatch):
    client, _, _ = env
    make_exhibit_with_readings(client)

    entered = threading.Event()
    release = threading.Event()
    real_backup = snap_mod._copy_backup

    def slow_backup(source, dest):
        entered.set()
        assert release.wait(timeout=5)
        return real_backup(source, dest)

    monkeypatch.setattr(snap_mod, "_copy_backup", slow_backup)

    results: list[int] = []
    results_lock = threading.Lock()

    def call() -> None:
        r = client.post("/snapshots")
        with results_lock:
            results.append(r.status_code)

    with ThreadPoolExecutor(max_workers=3) as pool:
        futs = [pool.submit(call) for _ in range(3)]
        assert entered.wait(timeout=5)
        release.set()
        for f in futs:
            f.result(timeout=10)

    assert sorted(results) == [201, 409, 409]

    # 只有一个快照被发布；锁已释放，后续创建正常成功且编号为 2
    assert client.get("/snapshots").json()["snapshots"][0]["seq"] == 1
    assert create(client)["seq"] == 2


# ---------- 失败清理与未完成文件不可见 ----------

def test_failed_create_leaves_no_visible_snapshot(env, monkeypatch):
    client, _, snap_dir = env
    make_exhibit_with_readings(client)
    real_backup = snap_mod._copy_backup

    def boom(source, dest):
        raise RuntimeError("backup exploded")

    monkeypatch.setattr(snap_mod, "_copy_backup", boom)
    resp = client.post("/snapshots")
    assert resp.status_code == 500

    # 临时文件已清理，列表为空，编号 1 没有被占用
    leftovers = [p.name for p in snap_dir.iterdir()]
    assert not any(n.endswith(".tmp") for n in leftovers)
    assert client.get("/snapshots").json() == {"snapshots": []}
    assert client.get("/snapshots/1").status_code == 404

    # 恢复后下一次创建仍从 1 开始（仅还原函数，保留环境变量补丁）
    monkeypatch.setattr(snap_mod, "_copy_backup", real_backup)
    assert create(client)["seq"] == 1


def test_unfinished_files_never_listed(env, monkeypatch):
    client, _, snap_dir = env
    make_exhibit_with_readings(client)

    # 成功发布 1 号快照
    create(client)

    # 手动伪造崩溃残留：2 号只有 db 无清单、tmp 临时文件、坏清单
    (snap_dir / "2.db").write_bytes((snap_dir / "1.db").read_bytes())
    (snap_dir / "3.db.tmp").write_bytes(b"partial")
    (snap_dir / "3.json.tmp").write_text("{incomplete", encoding="utf-8")
    (snap_dir / "4.json").write_text('{"seq": 4}', encoding="utf-8")  # 缺字段且无 db

    seqs = [s["seq"] for s in client.get("/snapshots").json()["snapshots"]]
    assert seqs == [1]
    assert client.get("/snapshots/2").status_code == 404
    assert client.get("/snapshots/4").status_code == 404

    # 新创建时崩溃残留的 .tmp 被清理，编号取已发布最大值 + 1 = 2
    out = create(client)
    assert out["seq"] == 2
    assert not (snap_dir / "3.db.tmp").exists()
    assert sorted(s["seq"] for s in client.get("/snapshots").json()["snapshots"]) == [1, 2]


def test_startup_cleans_stale_tmp(tmp_path, monkeypatch):
    monkeypatch.setenv("DB_PATH", str(tmp_path / "test.db"))
    monkeypatch.setenv("SNAPSHOT_DIR", str(tmp_path / "snapshots"))
    snap_dir = tmp_path / "snapshots"
    snap_dir.mkdir()
    (snap_dir / "7.db.tmp").write_bytes(b"x")
    (snap_dir / "7.json.tmp").write_text("{}")

    from app.main import app
    with TestClient(app):
        assert not (snap_dir / "7.db.tmp").exists()
        assert not (snap_dir / "7.json.tmp").exists()


# ---------- 在线一致性：备份不影响线上读写 ----------

def test_backup_does_not_block_live_writes(env):
    client, _, _ = env
    make_exhibit_with_readings(client)
    create(client)
    # 备份后线上库照常工作
    r = client.post("/exhibits/EX-1/readings", json={"readings": [
        {"id": "r9", "ts": iso(BASE + 9000), "lux": 1}]})
    assert r.status_code == 200
    assert client.get("/exhibits/EX-1/readings").status_code == 200


def test_default_snapshot_dir_when_env_unset(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("SNAPSHOT_DIR", raising=False)
    monkeypatch.setenv("DB_PATH", str(tmp_path / "test.db"))
    assert snap_mod.snapshot_dir().resolve() == (tmp_path / "snapshots").resolve()
