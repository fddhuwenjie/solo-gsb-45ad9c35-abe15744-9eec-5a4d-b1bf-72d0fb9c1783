"""照度上报存证：批量上报收据的规范序列化、SHA-256 链哈希与整链校验。

设计要点：
- 每次批量上报在**写入读数的同一事务**内生成一张收据；事务失败整体回滚，
  失败事务不会留下收据（也不会留下读数）。
- 收据内容（请求读数 + 每条 inserted/duplicate/conflict 结果）按**稳定规范
  序列化**：JSON 键排序、紧凑分隔符、浮点一律 ``float.hex``，随后取 SHA-256
  得到 ``content_hash``；再以 ``SHA-256(prev_hash + content_hash)`` 计算
  ``chain_hash`` 衔接上一张收据，首条收据的 ``prev_hash`` 为 64 个 ``0``。
- 链以写入时的**规范档案 ID** 为标识，``seq`` 从 1 连续递增；上报事务用
  ``BEGIN IMMEDIATE`` 串行化，并发上报也只形成唯一连续顺序。
- 收据**只增不改**：档案归并只删除档案与读数，来源与目标的既有收据链原样
  保留（本表无外键、不级联、不 UPDATE），归并后的新上报继续接入目标原链，
  旧哈希绝不重写。
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone

# 首条收据的 prev_hash（创世衔接值）
GENESIS_PREV_HASH = "0" * 64


# ---------- 稳定规范序列化 ----------

def _canon(obj: object) -> object:
    """把内容递归转换为可稳定哈希的形式：浮点用 float.hex，其余保持 JSON 原生类型。"""
    if isinstance(obj, float):
        return obj.hex()
    if isinstance(obj, dict):
        return {k: _canon(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_canon(v) for v in obj]
    return obj


def canonical_bytes(content: dict) -> bytes:
    """收据内容的规范字节序列：键排序、紧凑分隔符、UTF-8 编码。"""
    return json.dumps(
        _canon(content), sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")


def content_hash_of(content: dict) -> str:
    """内容哈希：SHA-256(规范序列化内容)。"""
    return hashlib.sha256(canonical_bytes(content)).hexdigest()


def chain_hash_of(prev_hash: str, content_hash: str) -> str:
    """链哈希：SHA-256(上一张收据链哈希 + 当前内容哈希)（均为十六进制字符串）。"""
    return hashlib.sha256(f"{prev_hash}{content_hash}".encode("ascii")).hexdigest()


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


# ---------- 收据生成（须在上报写事务内调用） ----------

def mint_receipt(
    conn: sqlite3.Connection,
    *,
    chain_id: str,
    requested_id: str,
    readings: list[dict],
    results: list[dict],
    inserted: int,
    duplicates: int,
    conflicts: int,
) -> sqlite3.Row:
    """在当前写事务内为一次批量上报生成收据并落库，返回插入的行。

    调用方必须已持有写事务（``BEGIN IMMEDIATE``）：此时读取的链尾不会被
    并发上报抢占，序号分配才唯一且连续；事务回滚时收据随读数一起消失。
    """
    tail = conn.execute(
        "SELECT seq, chain_hash FROM ingest_receipts WHERE chain_id = ? "
        "ORDER BY seq DESC LIMIT 1",
        (chain_id,),
    ).fetchone()
    seq = 1 if tail is None else tail["seq"] + 1
    prev_hash = GENESIS_PREV_HASH if tail is None else tail["chain_hash"]

    content = {
        "chain_id": chain_id,
        "seq": seq,
        "receipt_id": uuid.uuid4().hex,
        "requested_id": requested_id,
        "created_at": _utc_now_iso(),
        "request": {"readings": readings},
        "outcome": {
            "inserted": inserted,
            "duplicates": duplicates,
            "conflicts": conflicts,
            "results": results,
        },
    }
    c_hash = content_hash_of(content)
    c_chain = chain_hash_of(prev_hash, c_hash)
    conn.execute(
        """INSERT INTO ingest_receipts
           (chain_id, seq, receipt_id, prev_hash, content_hash, chain_hash,
            payload_json, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            chain_id, seq, content["receipt_id"], prev_hash, c_hash, c_chain,
            json.dumps(content, ensure_ascii=False, sort_keys=True),
            content["created_at"],
        ),
    )
    return conn.execute(
        "SELECT * FROM ingest_receipts WHERE chain_id = ? AND seq = ?",
        (chain_id, seq),
    ).fetchone()


# ---------- 整链校验 ----------

@dataclass
class ChainVerification:
    ok: bool
    checked: int                 # 已检查且通过的收据数量
    total: int                   # 链上收据总数
    first_failure: dict | None   # {"seq", "reason", "detail"}；通过时为 None


def load_chain(conn: sqlite3.Connection, chain_id: str) -> list[sqlite3.Row]:
    """按 seq 升序取出整条链。"""
    return conn.execute(
        "SELECT * FROM ingest_receipts WHERE chain_id = ? ORDER BY seq ASC",
        (chain_id,),
    ).fetchall()


def verify_chain_rows(rows: list[sqlite3.Row]) -> ChainVerification:
    """校验整链：序号连续、prev_hash 衔接、内容哈希与链哈希均可重算复现。

    返回首个断链或内容不符的位置；全部通过时 ``checked == total``。
    """
    checked = 0
    prev_hash = GENESIS_PREV_HASH
    total = len(rows)
    for expected_seq, row in enumerate(rows, start=1):
        if row["seq"] != expected_seq:
            return ChainVerification(False, checked, total, {
                "seq": expected_seq,
                "reason": "seq_gap",
                "detail": f"序号不连续：期望第 {expected_seq} 张，实际为第 {row['seq']} 张",
            })
        if row["prev_hash"] != prev_hash:
            return ChainVerification(False, checked, total, {
                "seq": expected_seq,
                "reason": "prev_hash_mismatch",
                "detail": "prev_hash 与上一张收据的链哈希不衔接（断链）",
            })
        try:
            content = json.loads(row["payload_json"])
        except json.JSONDecodeError:
            content = None
        if not isinstance(content, dict):
            return ChainVerification(False, checked, total, {
                "seq": expected_seq,
                "reason": "content_hash_mismatch",
                "detail": "收据内容不是合法的 JSON 对象",
            })
        actual_content_hash = content_hash_of(content)
        if actual_content_hash != row["content_hash"]:
            return ChainVerification(False, checked, total, {
                "seq": expected_seq,
                "reason": "content_hash_mismatch",
                "detail": "收据内容与登记的内容哈希不符（内容被篡改）",
            })
        if chain_hash_of(row["prev_hash"], actual_content_hash) != row["chain_hash"]:
            return ChainVerification(False, checked, total, {
                "seq": expected_seq,
                "reason": "chain_hash_mismatch",
                "detail": "链哈希与 prev_hash + 内容哈希的重算结果不符",
            })
        prev_hash = row["chain_hash"]
        checked += 1
    return ChainVerification(True, checked, total, None)
