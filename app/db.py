"""SQLite 访问层：连接管理与建表。

数据库路径由环境变量 DB_PATH 指定，默认 ./lightdose.db。
"""
from __future__ import annotations

import os
import sqlite3
from collections.abc import Iterator

SCHEMA = """
CREATE TABLE IF NOT EXISTS exhibits (
    id                   TEXT PRIMARY KEY,
    name                 TEXT NOT NULL,
    dose_limit_luxh      REAL NOT NULL CHECK (dose_limit_luxh > 0),
    max_interval_seconds REAL NOT NULL CHECK (max_interval_seconds > 0),
    created_at           TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

-- 读数以 (exhibit_id, reading_id) 为主键，reading_id 即客户端幂等键。
-- source_id 为可选的来源标识（传感器/采集器）；同一采样时刻不同来源的读数
-- 以不同 reading_id 全部留存，由来源优先级版本在查询时仲裁出权威值。
-- 批次更正只在 readings 行上置作废标记（软删除）：作废后读数立刻退出
-- 剂量/诊断/列表口径，但行保留作为存证；voided_by 指向作废它的更正记录。
CREATE TABLE IF NOT EXISTS readings (
    exhibit_id   TEXT NOT NULL REFERENCES exhibits(id) ON DELETE CASCADE,
    reading_id   TEXT NOT NULL,
    ts           REAL NOT NULL,           -- 采样时刻，Unix 秒（UTC）
    lux          REAL NOT NULL CHECK (lux >= 0),
    source_id    TEXT,                    -- 来源标识；NULL 表示旧式上报（未声明来源）
    received_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    voided_at    TEXT,                    -- 作废时刻（UTC ISO）；NULL 表示读数仍生效
    voided_by    TEXT,                    -- 作废它的 reading_corrections.request_key
    PRIMARY KEY (exhibit_id, reading_id)
);

CREATE INDEX IF NOT EXISTS idx_readings_exhibit_ts ON readings (exhibit_id, ts);

-- 归并后来源 ID 成为目标（规范）ID 的永久别名；所有写入与查询先解析别名。
-- 归并提交时会把来源名下的旧别名一并改指向目标，因此该表始终只有一跳，
-- 结构上不可能形成别名环。
CREATE TABLE IF NOT EXISTS exhibit_aliases (
    alias_id     TEXT PRIMARY KEY,
    canonical_id TEXT NOT NULL REFERENCES exhibits(id) ON DELETE RESTRICT,
    created_at   TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

-- 归并预演令牌：绑定预演时刻两边的元数据与读数集合版本（SHA-256）。
-- 提交时重算四个哈希，任一不一致即 409；提交成功后固化 result_json，
-- 同一令牌重复提交直接回放首次结果。
CREATE TABLE IF NOT EXISTS merge_previews (
    token            TEXT PRIMARY KEY,
    source_id        TEXT NOT NULL,   -- 预演时解析出的规范来源 ID
    target_id        TEXT NOT NULL,   -- 预演时解析出的规范目标 ID
    source_meta_hash TEXT NOT NULL,
    target_meta_hash TEXT NOT NULL,
    source_read_hash TEXT NOT NULL,
    target_read_hash TEXT NOT NULL,
    conflict_count   INTEGER NOT NULL,
    created_at       TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    committed_at     TEXT,
    result_json      TEXT
);

-- 别名永久且不可复用：禁止用已归并的别名 ID 重新建档
CREATE TRIGGER IF NOT EXISTS trg_exhibit_no_alias_id
BEFORE INSERT ON exhibits
WHEN EXISTS (SELECT 1 FROM exhibit_aliases WHERE alias_id = NEW.id)
BEGIN
    SELECT RAISE(ABORT, 'exhibit id is a permanent merge alias');
END;

-- 展陈开放日历：每件展品最多一份，随档案删除级联清除；
-- 归并删除来源档案时其日历一并级联删除，目标档案日历保持不变。
CREATE TABLE IF NOT EXISTS exhibit_calendars (
    exhibit_id TEXT PRIMARY KEY REFERENCES exhibits(id) ON DELETE CASCADE,
    timezone   TEXT NOT NULL,            -- IANA 时区名，如 Asia/Shanghai
    updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

-- 每周开放时段：weekday 取 ISO 8601（1=周一 … 7=周日）；
-- 时刻为本地当日秒偏移 [0, 86400)；end_sec <= start_sec 表示跨午夜到次日。
CREATE TABLE IF NOT EXISTS calendar_weekly_windows (
    exhibit_id TEXT NOT NULL REFERENCES exhibit_calendars(exhibit_id) ON DELETE CASCADE,
    weekday    INTEGER NOT NULL CHECK (weekday BETWEEN 1 AND 7),
    start_sec  INTEGER NOT NULL CHECK (start_sec >= 0 AND start_sec < 86400),
    end_sec    INTEGER NOT NULL CHECK (end_sec >= 0 AND end_sec < 86400),
    PRIMARY KEY (exhibit_id, weekday, start_sec, end_sec)
);

-- 例外日：按本地日期整体替换当日每周规则；例外日可以没有任何窗口（当日闭馆），
-- 因此日期本身需要单独一行存在。
CREATE TABLE IF NOT EXISTS calendar_exception_days (
    exhibit_id TEXT NOT NULL REFERENCES exhibit_calendars(exhibit_id) ON DELETE CASCADE,
    date       TEXT NOT NULL,            -- 本地日期 YYYY-MM-DD
    PRIMARY KEY (exhibit_id, date)
);

CREATE TABLE IF NOT EXISTS calendar_exception_windows (
    exhibit_id TEXT NOT NULL,
    date       TEXT NOT NULL,
    start_sec  INTEGER NOT NULL CHECK (start_sec >= 0 AND start_sec < 86400),
    end_sec    INTEGER NOT NULL CHECK (end_sec >= 0 AND end_sec < 86400),
    PRIMARY KEY (exhibit_id, date, start_sec, end_sec),
    FOREIGN KEY (exhibit_id, date) REFERENCES calendar_exception_days(exhibit_id, date) ON DELETE CASCADE
);

-- 照度上报存证收据链：每次批量上报在写入读数的同一事务内生成一张收据。
-- 链以写入时的规范档案 ID 为标识（chain_id），seq 从 1 连续递增；
-- prev_hash 衔接上一张收据的链哈希（首条为 64 个 '0'），
-- chain_hash = SHA-256(prev_hash + content_hash)。
-- 档案归并只删除档案与读数，收据链必须原样保留供事后独立核查，
-- 因此本表故意不设外键、不级联删除，已有行绝不 UPDATE（旧哈希不可重写）。
CREATE TABLE IF NOT EXISTS ingest_receipts (
    chain_id     TEXT NOT NULL,            -- 链标识：写入时的规范档案 ID
    seq          INTEGER NOT NULL CHECK (seq >= 1),
    receipt_id   TEXT NOT NULL UNIQUE,
    prev_hash    TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    chain_hash   TEXT NOT NULL,
    payload_json TEXT NOT NULL,            -- 收据内容（请求 + 逐条结果）的 JSON
    created_at   TEXT NOT NULL,
    PRIMARY KEY (chain_id, seq)
);

-- RFID 馆藏盘点轮次：round_id 与规范化请求摘要（request_digest）一一绑定。
-- 首次对账在写事务内固化结果 JSON；相同请求重放回放原结果，不同请求 409；
-- round_id 主键 + BEGIN IMMEDIATE 保证并发提交只能固化一份结果。
-- 对账只读展品/别名表、不写读数、不接触收据链，本表是对账唯一写入点。
CREATE TABLE IF NOT EXISTS inventory_rounds (
    round_id       TEXT PRIMARY KEY,
    request_digest TEXT NOT NULL,          -- 规范化请求摘要 SHA-256（时间 UTC、别名解析后）
    result_json    TEXT NOT NULL,          -- InventoryRoundOut 的 JSON（首次固化结果）
    created_at     TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

-- 照度上报批次更正：作废某张存证收据中实际插入的读数。
-- 更正与读数作废标记在同一个 BEGIN IMMEDIATE 事务内落库；request_key 为
-- 幂等键：相同 request_key 重放首次结果，改目标（规范档案/收据/原因）→ 409。
-- 与 ingest_receipts 一样故意不设外键、不级联删除：归并删除档案后更正记录
-- 连同收据链原样保留供事后核查；收据内容、序号与链哈希不受更正影响。
-- (chain_id, seq) 上的部分唯一索引保证一张收据至多被成功更正一次。
CREATE TABLE IF NOT EXISTS reading_corrections (
    request_key       TEXT PRIMARY KEY,
    canonical_id      TEXT NOT NULL,       -- 更正时解析出的规范档案 ID（= 收据链标识）
    requested_id      TEXT NOT NULL,       -- 请求中使用的展品 ID（可能是别名）
    receipt_id        TEXT NOT NULL,       -- 被更正的存证收据 receipt_id
    chain_id          TEXT NOT NULL,       -- 被更正收据的链标识
    seq               INTEGER NOT NULL,
    reason            TEXT NOT NULL,       -- 作废原因
    voided_count      INTEGER NOT NULL,    -- 实际作废读数条数
    voided_json       TEXT NOT NULL,       -- 作废读数明细（id/ts/lux，按请求顺序去重）
    result_json       TEXT NOT NULL,       -- ReadingCorrectionOut（首次结果，供重放）
    created_at        TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_reading_corrections_receipt
ON reading_corrections (chain_id, seq);

-- 照度异常处置单：创建时在单个事务内按未作废权威时间线重算诊断事件与
-- calendar=display 剂量，连同收据链末端哈希、未作废读数指纹、来源优先级
-- 配置指纹与日历指纹一起固化快照（snapshot_json），digest 为其规范序列化
-- 的 SHA-256。快照只增不改：snapshot_json 与 digest 落库后绝不 UPDATE，
-- 结案只写结案列（closed_at/decision/close_note/evidence_json/closed_digest）。
-- request_key 为创建幂等键：同键同目标重放首次结果，换目标 → 409。
-- 与 ingest_receipts 一样故意不设外键、不级联删除：档案归并后处置单
-- 连同其快照原样保留供事后核查。
CREATE TABLE IF NOT EXISTS illumination_tickets (
    ticket_id               TEXT PRIMARY KEY,
    request_key             TEXT NOT NULL UNIQUE,  -- 创建幂等键
    canonical_id            TEXT NOT NULL,         -- 创建时解析出的规范档案 ID
    requested_id            TEXT NOT NULL,         -- 请求中使用的展品 ID（可能是别名）
    start_s                 REAL NOT NULL,         -- 处置窗口起点，Unix 秒（UTC）
    end_s                   REAL NOT NULL,         -- 处置窗口终点，Unix 秒（UTC）
    max_rate_lux_per_second REAL NOT NULL,         -- 诊断阈值：突变速率上限（lux/秒）
    stuck_tolerance_lux     REAL NOT NULL,         -- 诊断阈值：卡死容差（lux）
    min_stuck_seconds       REAL NOT NULL,         -- 诊断阈值：卡死最短持续（秒）
    digest                  TEXT NOT NULL,         -- 固化快照摘要 SHA-256（不可变）
    snapshot_json           TEXT NOT NULL,         -- 固化快照（事件+剂量+指纹+链尾）
    status                  TEXT NOT NULL DEFAULT 'open'
                            CHECK (status IN ('open', 'resolved', 'accepted')),
    created_at              TEXT NOT NULL,
    closed_at               TEXT,                  -- 结案时刻（UTC ISO）
    decision                TEXT,                  -- 结案决定：resolved | accepted
    close_note              TEXT,                  -- 结案说明；accepted 时即保留事件的理由
    evidence_json           TEXT,                  -- resolved 的收据/更正 request_key 引用
    closed_digest           TEXT                   -- 结案时重算核对的摘要
);

-- 展厅开放批次合规核验凭证：request_key 为幂等键，与（规范化输入摘要 +
-- 当时证据摘要）绑定。仅在全部展品核验通过时固化（任一失败不写入）；
-- 相同 request_key 重放须规范化输入与当时摘要都一致，否则 409。
-- 凭证只增不改：落库后绝不 UPDATE；与 ingest_receipts 一样故意不设外键、
-- 不级联删除，档案归并后凭证连同其证据摘要原样保留供事后核查。
CREATE TABLE IF NOT EXISTS compliance_credentials (
    request_key    TEXT PRIMARY KEY,       -- 核验幂等键
    request_digest TEXT NOT NULL,          -- 规范化输入摘要 SHA-256（时间 UTC、别名解析后）
    digest         TEXT NOT NULL,          -- 凭证摘要：规范化输入 + 各项证据摘要的 SHA-256
    credential_id  TEXT NOT NULL UNIQUE,   -- 凭证 ID
    result_json    TEXT NOT NULL,          -- 固化的合规凭证（ComplianceCheckOut 合规形态）
    created_at     TEXT NOT NULL           -- 固化时刻（UTC ISO）
);

-- 跨盘点轮次的馆藏区域交接：每件规范档案一条只增不改的交接链，seq 从 1
-- 连续递增。一笔交接记录证据（两轮的固化 request_digest）与写入在同一个
-- BEGIN IMMEDIATE 事务内完成；(canonical_id, source_round_id) /
-- (canonical_id, target_round_id) 唯一索引 + 写锁保证并发交接最多一笔生效、
-- 同一轮不能被两笔交接占用，失败事务不留半笔记录。行只 INSERT 绝不 UPDATE：
-- 旧轮次不能回填覆盖当前去向。归并仅在单侧有链时把链整链改挂到目标规范 ID
-- （两侧都有链则拒绝归并），因此本表保留对 exhibits / inventory_rounds 的
-- 外键且不级联删除。
CREATE TABLE IF NOT EXISTS area_handovers (
    canonical_id        TEXT NOT NULL REFERENCES exhibits(id) ON DELETE RESTRICT,
    seq                 INTEGER NOT NULL CHECK (seq >= 1),
    source_round_id     TEXT NOT NULL,
    target_round_id     TEXT NOT NULL,
    source_area         TEXT NOT NULL,
    target_area         TEXT NOT NULL,
    source_round_digest TEXT NOT NULL,   -- 交接核验时来源轮的固化请求摘要（证据，不可变）
    target_round_digest TEXT NOT NULL,   -- 交接核验时接收轮的固化请求摘要（证据，不可变）
    created_at          TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    PRIMARY KEY (canonical_id, seq),
    CHECK (source_round_id <> target_round_id)
);

-- 一轮盘点在一条链中至多作为一笔交接的来源/接收方：链必须按轮次时间续接，
-- 并发交接竞争同一续接位置时由唯一索引兜底，只有一笔能落库。
CREATE UNIQUE INDEX IF NOT EXISTS idx_area_handovers_source
ON area_handovers (canonical_id, source_round_id);
CREATE UNIQUE INDEX IF NOT EXISTS idx_area_handovers_target
ON area_handovers (canonical_id, target_round_id);

-- 多来源读数仲裁：来源优先级版本。每版携带生效时刻 effective_at 与来源
-- 优先级列表（JSON 数组，靠前优先级更高；未列入的来源并列最低）。
-- 版本只增不改：配置更新必须携带 expected_version（当前最新版本号），
-- 过期返回 409；对采样时刻 t 仲裁时取 effective_at <= t 的最新版本，
-- 因此调整优先级只从生效时刻起影响重算，历史收据与原始读数不改写。
-- 档案归并时双方配置按版本内容比对：一致或仅来源有才合并（搬迁），
-- 双方都有且不一致则拒绝归并（冲突不静默覆盖）；随档案删除级联清除。
CREATE TABLE IF NOT EXISTS source_priority_versions (
    exhibit_id   TEXT NOT NULL REFERENCES exhibits(id) ON DELETE CASCADE,
    version      INTEGER NOT NULL CHECK (version >= 1),
    effective_at REAL NOT NULL,           -- 生效时刻，Unix 秒（UTC）
    priorities   TEXT NOT NULL,           -- JSON 数组：source_id 按优先级从高到低
    created_at   TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    PRIMARY KEY (exhibit_id, version)
);
"""

# 旧库（更正/多来源功能上线前创建）补列：幂等 ALTER，列已存在则跳过。
_LEGACY_MIGRATIONS = (
    "ALTER TABLE readings ADD COLUMN voided_at TEXT",
    "ALTER TABLE readings ADD COLUMN voided_by TEXT",
    "ALTER TABLE readings ADD COLUMN source_id TEXT",
)


def db_path() -> str:
    return os.environ.get("DB_PATH", "lightdose.db")


def connect() -> sqlite3.Connection:
    # check_same_thread=False：FastAPI 的线程池不保证依赖的创建与关闭落在
    # 同一线程；连接本身只在单个请求生命周期内顺序使用，并发安全由
    # SQLite 自身的锁（WAL + BEGIN IMMEDIATE）保证。
    conn = sqlite3.connect(db_path(), timeout=30, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


def init_db() -> None:
    with connect() as conn:
        conn.executescript(SCHEMA)
        existing_cols = {
            r["name"] for r in conn.execute("PRAGMA table_info(readings)").fetchall()
        }
        for stmt in _LEGACY_MIGRATIONS:
            col = stmt.split("ADD COLUMN", 1)[1].strip().split()[0]
            if col not in existing_cols:
                conn.execute(stmt)


def get_conn() -> Iterator[sqlite3.Connection]:
    """FastAPI 依赖：每请求一个连接。"""
    conn = connect()
    try:
        yield conn
    finally:
        conn.close()
