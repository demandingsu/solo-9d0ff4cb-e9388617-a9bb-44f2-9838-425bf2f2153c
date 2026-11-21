"""核心业务逻辑：字库装载、逐字取字库、交印出单。

规则要点
--------
1. 每个字取“按优先顺序第一个收录它的字库”，以 (字库, 版次, 字模号) 识别字模。
2. 交印一册只更新该册选用的字模，其余册沿用各自上次交印的清单。
3. 比对交印前后“各册共需的字模（并集）”生成增减单。
4. 交印事务串行化（BEGIN IMMEDIATE + 库内应用级互斥锁），逐单生效，
   每单提交前后都校验并集与各册清单一致。
5. 重复确认同一笔交印复用原单，按单号可原样重取。
6. 缺字或保存失败时回滚，仍沿用原清单。
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
from datetime import datetime, timezone

from .database import connect, init_db
from .errors import DomainError, MissingGlyphError, SaveError
from .parsers import LibraryRow

# 进程内互斥：保证多人同时交印按生效次序逐单出单。
# 数据库层 BEGIN IMMEDIATE + busy_timeout 则在多进程/多连接下兜底。
_submit_lock = threading.Lock()

IDENTITY_KEYS = ("library_name", "edition", "matrix_no", "glyph_char")


def _now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def _identity(row: sqlite3.Row | dict) -> dict:
    return {k: row[k] for k in IDENTITY_KEYS}


def _ident_key(ident: dict) -> tuple:
    return tuple(ident[k] for k in IDENTITY_KEYS)


def _sorted_idents(idents: list[dict]) -> list[dict]:
    return sorted(idents, key=_ident_key)


def replace_libraries(rows: list[LibraryRow]) -> dict:
    """全量替换当前生效字库清单（按文件顺序定优先次序）。"""
    conn = connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        # 1. 校验同一 (字库, 版次) 的版次一致——(字库,版次) 相同天然一致；
        #    这里校验同一字库名若出现多个版次，允许但分别视作不同字库条目。
        # 2. 旧库标记为非当前（保留仍被各册引用的字模行，保证历史清单可追溯）。
        conn.execute("UPDATE libraries SET current=0")

        lib_id: dict[tuple[str, str], int] = {}
        priority = 0
        for r in rows:
            key = (r.library, r.edition)
            if key not in lib_id:
                priority += 1
                cur = conn.execute(
                    "SELECT library_id FROM libraries WHERE name=? AND edition=?",
                    key,
                ).fetchone()
                if cur is None:
                    lid = conn.execute(
                        "INSERT INTO libraries(name, edition, priority, current) "
                        "VALUES (?,?,?,1)",
                        (r.library, r.edition, priority),
                    ).lastrowid
                else:
                    lid = cur["library_id"]
                    conn.execute(
                        "UPDATE libraries SET priority=?, current=1 WHERE library_id=?",
                        (priority, lid),
                    )
                lib_id[key] = lid
            conn.execute(
                "INSERT OR IGNORE INTO glyphs(library_id, matrix_no, glyph_char) "
                "VALUES (?,?,?)",
                (lib_id[key], r.matrix_no, r.glyph_char),
            )

        # 非当前库中已无人引用的字模/库记录可清理，这里保留库行、保留字模行，
        # 仅作 current=0 标记，避免误删历史快照引用。
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    return {
        "library_count": len({(r.library, r.edition) for r in rows}),
        "glyph_count": len(rows),
        "replaced_at": _now_iso(),
    }


def _get_or_create_volume(conn: sqlite3.Connection, name: str) -> int:
    row = conn.execute("SELECT volume_id FROM volumes WHERE name=?", (name,)).fetchone()
    if row:
        return row["volume_id"]
    cur = conn.execute("INSERT INTO volumes(name) VALUES (?)", (name,))
    return cur.lastrowid


def replace_volume_chars(volume_name: str, chars: list[str]) -> dict:
    """全量替换某册用字表（不动已交印的字模清单，交印时才重算）。"""
    conn = connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        volume_id = _get_or_create_volume(conn, volume_name)
        conn.execute("DELETE FROM volume_chars WHERE volume_id=?", (volume_id,))
        conn.executemany(
            "INSERT INTO volume_chars(volume_id, glyph_char, source_order) VALUES (?,?,?)",
            [(volume_id, ch, i) for i, ch in enumerate(chars)],
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    return {"volume": volume_name, "char_count": len(chars), "updated_at": _now_iso()}


def _resolve_char(conn: sqlite3.Connection, ch: str) -> dict | None:
    """按字库优先顺序取第一个收录该字的字模。"""
    row = conn.execute(
        """
        SELECT l.name AS library_name, l.edition, g.matrix_no, g.glyph_char,
               g.glyph_id, l.library_id
        FROM glyphs g
        JOIN libraries l ON l.library_id = g.library_id
        WHERE g.glyph_char = ? AND l.current = 1
        ORDER BY l.priority ASC, g.matrix_no ASC
        LIMIT 1
        """,
        (ch,),
    ).fetchone()
    if row is None:
        return None
    return dict(row)


def _load_active_identities(conn: sqlite3.Connection, volume_id: int) -> list[dict]:
    rows = conn.execute(
        """
        SELECT l.name AS library_name, l.edition, g.matrix_no, g.glyph_char
        FROM active_matrices a
        JOIN glyphs g ON g.glyph_id = a.glyph_id
        JOIN libraries l ON l.library_id = g.library_id
        WHERE a.volume_id = ?
        ORDER BY l.name, l.edition, g.matrix_no
        """,
        (volume_id,),
    ).fetchall()
    return [_identity(r) for r in rows]


def _load_pool(conn: sqlite3.Connection, exclude_volume: int | None = None) -> list[dict]:
    """各册当前共需字模并集。"""
    sql = """
        SELECT DISTINCT l.name AS library_name, l.edition, g.matrix_no, g.glyph_char
        FROM active_matrices a
        JOIN glyphs g ON g.glyph_id = a.glyph_id
        JOIN libraries l ON l.library_id = g.library_id
    """
    params: tuple = ()
    if exclude_volume is not None:
        sql += " WHERE a.volume_id != ?"
        params = (exclude_volume,)
    sql += " ORDER BY l.name, l.edition, g.matrix_no"
    rows = conn.execute(sql, params).fetchall()
    return [_identity(r) for r in rows]


def _fingerprint(volume_id: int, chars: list[str], target: list[dict]) -> str:
    """同一笔交印的判重指纹：同册、同用字、同解析目标字模。"""
    raw = json.dumps(
        {
            "volume_id": volume_id,
            "chars": chars,
            "target": _sorted_idents(target),
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _assert_pool_consistent(conn: sqlite3.Connection, stage: str) -> None:
    """校验：各册 active 并集 == 按各册用字解析出的需求并集（已交印册）。"""
    actual = {_ident_key(x) for x in _load_pool(conn)}
    expected: set[tuple] = set()
    vols = conn.execute("SELECT volume_id FROM volumes").fetchall()
    for v in vols:
        vid = v["volume_id"]
        active = conn.execute(
            "SELECT COUNT(*) AS c FROM active_matrices WHERE volume_id=?", (vid,)
        ).fetchone()["c"]
        if active == 0:
            continue  # 尚未交印过的册不参与共需
        for ident in _load_active_identities(conn, vid):
            expected.add(_ident_key(ident))
    if actual != expected:
        raise SaveError(f"{stage} 共需字模一致性校验失败")


def submit_volume(
    volume_name: str,
    idem_key: str | None = None,
    fail_commit: bool = False,
) -> dict:
    """交印一册，返回增减单（原单复用则 reused=True）。"""
    with _submit_lock:
        return _submit_tx(volume_name, idem_key, fail_commit)


def _submit_tx(volume_name: str, idem_key: str | None, fail_commit: bool) -> dict:
    conn = connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        vol = conn.execute(
            "SELECT volume_id FROM volumes WHERE name=?", (volume_name,)
        ).fetchone()
        if vol is None:
            raise DomainError(f"册“{volume_name}”不存在，请先上传用字表", 404)
        volume_id = vol["volume_id"]

        # 1. 幂等键命中：原样复用
        if idem_key:
            old = conn.execute(
                "SELECT payload_json FROM orders WHERE idem_key=?", (idem_key,)
            ).fetchone()
            if old is not None:
                payload = json.loads(old["payload_json"])
                payload["reused"] = True
                conn.rollback()
                return payload

        chars = [
            r["glyph_char"]
            for r in conn.execute(
                "SELECT glyph_char FROM volume_chars WHERE volume_id=? ORDER BY source_order",
                (volume_id,),
            ).fetchall()
        ]
        if not chars:
            raise DomainError(f"册“{volume_name}”的用字表为空", 404)

        # 2. 逐字取第一个收录它的字库；有缺字则整体失败，沿用原清单
        resolved: list[dict] = []
        missing: list[str] = []
        for ch in chars:
            found = _resolve_char(conn, ch)
            if found is None:
                missing.append(ch)
            else:
                resolved.append(found)
        if missing:
            raise MissingGlyphError(missing)

        # 按字模去重（不同字不会同号，去重仅为防御并保证确定性顺序）
        unique: dict[int, dict] = {}
        for item in resolved:
            unique.setdefault(item["glyph_id"], item)
        after_idents = _sorted_idents([_identity(x) for x in unique.values()])
        before_idents = _load_active_identities(conn, volume_id)
        before_keys = {_ident_key(x) for x in before_idents}
        target_keys = {_ident_key(x) for x in after_idents}

        # 3. 无幂等键时的重复确认：同册、同用字、解析目标与当前已交印清单
        #    完全相同（即没有任何变化）时，复用该册最近一笔原单
        fp = _fingerprint(volume_id, chars, after_idents)
        if not idem_key and before_keys == target_keys:
            old = conn.execute(
                "SELECT payload_json FROM orders "
                "WHERE volume_id=? ORDER BY seq DESC LIMIT 1",
                (volume_id,),
            ).fetchone()
            if old is not None:
                payload = json.loads(old["payload_json"])
                payload["reused"] = True
                conn.rollback()
                return payload

        # 4. 交印前共需并集（含本册旧清单）
        pool_before = _load_pool(conn)
        before_keys = {_ident_key(x) for x in pool_before}

        # 5. 只更新本册
        conn.execute("DELETE FROM active_matrices WHERE volume_id=?", (volume_id,))
        conn.executemany(
            "INSERT INTO active_matrices(volume_id, glyph_id) VALUES (?,?)",
            [(volume_id, gid) for gid in unique],
        )

        # 6. 交印后共需并集，逐字模比对生成增减单
        pool_after = _load_pool(conn)
        after_keys = {_ident_key(x) for x in pool_after}
        added = _sorted_idents([x for x in pool_after if _ident_key(x) not in before_keys])
        removed = _sorted_idents([x for x in pool_before if _ident_key(x) not in after_keys])

        # 7. 逐单校验：提交前后共需并集都必须与各册 active 清单一致
        _assert_pool_consistent(conn, "交印后")

        # 8. 单号与落库
        row = conn.execute("SELECT seq FROM submit_seq").fetchone()
        next_seq = row["seq"] + 1
        conn.execute("UPDATE submit_seq SET seq=?", (next_seq,))
        order_no = f"MO{datetime.now().strftime('%Y%m%d')}-{next_seq:06d}"

        payload = {
            "order_no": order_no,
            "seq": next_seq,
            "volume": volume_name,
            "idem_key": idem_key,
            "reused": False,
            "fingerprint": fp,
            "effective_at": _now_iso(),
            "pool_before": pool_before,
            "pool_after": pool_after,
            "added": added,
            "removed": removed,
            "volume_before": before_idents,
            "volume_after": after_idents,
        }
        conn.execute(
            "INSERT INTO orders(order_no, seq, volume_id, volume_name, idem_key, "
            "fingerprint, payload_json) VALUES (?,?,?,?,?,?,?)",
            (
                order_no,
                next_seq,
                volume_id,
                volume_name,
                idem_key,
                fp,
                json.dumps(payload, ensure_ascii=False),
            ),
        )

        if fail_commit:
            # 测试钩子：模拟保存失败
            raise SaveError("模拟保存失败（测试）")
        conn.commit()
        return payload
    except DomainError:
        conn.rollback()
        raise
    except sqlite3.OperationalError as exc:
        conn.rollback()
        raise SaveError(f"数据库保存失败，已回滚，仍沿用原清单: {exc}")
    except Exception:
        conn.rollback()
        raise SaveError()
    finally:
        conn.close()


def get_order(order_no: str) -> dict:
    conn = connect()
    try:
        row = conn.execute(
            "SELECT payload_json FROM orders WHERE order_no=?", (order_no,)
        ).fetchone()
        if row is None:
            raise DomainError(f"单号 {order_no} 不存在", 404)
        payload = json.loads(row["payload_json"])
        payload["reused"] = False
        return payload
    finally:
        conn.close()


def list_orders(volume_name: str | None = None) -> list[dict]:
    conn = connect()
    try:
        if volume_name:
            rows = conn.execute(
                "SELECT order_no, seq, volume_name, created_at, payload_json "
                "FROM orders WHERE volume_name=? ORDER BY seq",
                (volume_name,),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT order_no, seq, volume_name, created_at, payload_json "
                "FROM orders ORDER BY seq"
            ).fetchall()
        result = []
        for r in rows:
            p = json.loads(r["payload_json"])
            result.append(
                {
                    "order_no": r["order_no"],
                    "seq": r["seq"],
                    "volume": r["volume_name"],
                    "created_at": r["created_at"],
                    "added_count": len(p["added"]),
                    "removed_count": len(p["removed"]),
                }
            )
        return result
    finally:
        conn.close()


def get_state() -> dict:
    """当前工位全貌：字库、各册用字与已交印字模、共需并集。"""
    conn = connect()
    try:
        libs = [
            dict(r)
            for r in conn.execute(
                "SELECT name, edition, priority, current FROM libraries "
                "WHERE current=1 ORDER BY priority"
            ).fetchall()
        ]
        volumes = []
        for v in conn.execute("SELECT volume_id, name FROM volumes ORDER BY volume_id"):
            chars = [
                r["glyph_char"]
                for r in conn.execute(
                    "SELECT glyph_char FROM volume_chars WHERE volume_id=? "
                    "ORDER BY source_order",
                    (v["volume_id"],),
                ).fetchall()
            ]
            active = _load_active_identities(conn, v["volume_id"])
            volumes.append(
                {"volume": v["name"], "chars": chars, "active_matrices": active}
            )
        pool = _load_pool(conn)
        return {"libraries": libs, "volumes": volumes, "shared_pool": pool}
    finally:
        conn.close()


def reset_all() -> None:
    """清空全部业务数据（开发/演示用）。"""
    conn = connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        for table in (
            "orders",
            "active_matrices",
            "volume_chars",
            "volumes",
            "glyphs",
            "libraries",
        ):
            conn.execute(f"DELETE FROM {table}")
        conn.execute("UPDATE submit_seq SET seq=0")
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def ensure_db() -> None:
    """建表并初始化序号（连接时会自动创建数据目录）。"""
    init_db()
