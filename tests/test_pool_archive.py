"""共需存档瘦身：新单只存增减、每 32 单一快照、按单号还原、旧库直开兼容。

运行：
    PYTHONPATH=. pytest -q
"""
from __future__ import annotations

import io
import json
import os
import sqlite3
import tempfile

import pytest

# 每个测试会话使用独立临时库
_tmp = tempfile.TemporaryDirectory()
os.environ["MATRIX_DB_PATH"] = os.path.join(_tmp.name, "test.db")

from fastapi.testclient import TestClient  # noqa: E402

from app import database, service  # noqa: E402
from app.main import app  # noqa: E402


@pytest.fixture()
def client():
    service.ensure_db()
    service.reset_all()
    with TestClient(app) as c:
        yield c


def _csv(text: str) -> bytes:
    return text.encode("utf-8-sig")


def _upload_lib(client, rows: list[tuple[str, str, str, str]]):
    buf = io.StringIO()
    buf.write("字库,版次,字模号,字\n")
    for r in rows:
        buf.write(",".join(r) + "\n")
    return client.post(
        "/api/libraries",
        files={"file": ("libraries.csv", _csv(buf.getvalue()), "text/csv")},
    )


def _upload_vol(client, name: str, chars: str):
    body = "字\n" + "\n".join(chars) + "\n"
    return client.post(
        f"/api/volumes/{name}/chars",
        files={"file": (f"{name}.csv", _csv(body), "text/csv")},
    )


def _ident(glyph: str, lib: str = "宋韵楼", edition: str = "初刻", no: str = "SR-1") -> dict:
    return {
        "library_name": lib,
        "edition": edition,
        "matrix_no": no,
        "glyph_char": glyph,
    }


def _archive_counts() -> tuple[int, int]:
    conn = database.connect()
    try:
        d = conn.execute("SELECT COUNT(*) FROM pool_deltas").fetchone()[0]
        s = conn.execute("SELECT COUNT(*) FROM pool_snapshots").fetchone()[0]
        return d, s
    finally:
        conn.close()


def _stored_payload(order_no: str) -> dict:
    conn = database.connect()
    try:
        row = conn.execute(
            "SELECT payload_json FROM orders WHERE order_no=?", (order_no,)
        ).fetchone()
        return json.loads(row["payload_json"])
    finally:
        conn.close()


# ---------- 新单存档：只存增减，不再整存两份完整共需清单 ----------


def test_new_order_stores_only_deltas(client):
    _upload_lib(client, [("宋韵楼", "初刻", "SR-1", "天"), ("宋韵楼", "初刻", "SR-2", "地")])
    _upload_vol(client, "册甲", "天地")
    d = client.post("/api/volumes/册甲/submit").json()

    stored = _stored_payload(d["order_no"])
    # 存档中不再有两份完整共需清单，只保留实际增减与瘦身标记
    assert "pool_before" not in stored and "pool_after" not in stored
    assert stored["pool_storage"] == "delta"
    assert stored["added"] == d["added"] and stored["removed"] == d["removed"]
    assert stored["volume_before"] == d["volume_before"]
    assert stored["volume_after"] == d["volume_after"]

    # 恢复所需数据随单据一同生效：一条增减记录 + 首张新单的一份完整快照
    assert _archive_counts() == (1, 1)
    conn = database.connect()
    try:
        delta = conn.execute("SELECT * FROM pool_deltas WHERE seq=?", (d["seq"],)).fetchone()
        assert json.loads(delta["added_json"]) == d["added"]
        assert json.loads(delta["removed_json"]) == []
        snap = conn.execute("SELECT * FROM pool_snapshots WHERE seq=?", (d["seq"],)).fetchone()
        assert json.loads(snap["pool_json"]) == []  # 首单生效前共需为空
    finally:
        conn.close()


def test_refetch_reconstructs_full_pools(client):
    _upload_lib(client, [
        ("宋韵楼", "初刻", "SR-1", "天"),
        ("宋韵楼", "初刻", "SR-2", "地"),
        ("宋韵楼", "初刻", "SR-3", "人"),
    ])
    _upload_vol(client, "册甲", "天地人")
    _upload_vol(client, "册乙", "天地")

    responses = []
    responses.append(client.post("/api/volumes/册甲/submit").json())
    responses.append(client.post("/api/volumes/册乙/submit").json())
    _upload_vol(client, "册甲", "天地")
    responses.append(client.post("/api/volumes/册甲/submit").json())

    # 按单号重取：还原出的完整共需清单与交印响应一致
    for d in responses:
        got = client.get(f"/api/orders/{d['order_no']}").json()
        assert got["pool_before"] == d["pool_before"]
        assert got["pool_after"] == d["pool_after"]
        assert got["added"] == d["added"] and got["removed"] == d["removed"]
        assert got["volume_before"] == d["volume_before"]
        assert got["volume_after"] == d["volume_after"]
        assert "pool_storage" not in got  # 对外 JSON 沿用原格式，无瘦身标记

    # 末单还原结果与当前工位共需一致
    state = client.get("/api/state").json()
    last = client.get(f"/api/orders/{responses[-1]['order_no']}").json()
    assert last["pool_after"] == state["shared_pool"]

    # CSV 下载沿用原格式（含共需汇总行）
    r = client.get(f"/api/orders/{responses[-1]['order_no']}/download")
    text = r.content.decode("utf-8-sig")
    assert f"交印前共需 {len(responses[-1]['pool_before'])} 个" in text
    assert f"交印后共需 {len(responses[-1]['pool_after'])} 个" in text


# ---------- 任意连续 32 张新单最多一份完整快照；还原最多读 32 张单 ----------


def test_snapshot_every_32_orders_and_bounded_reads(client):
    _upload_lib(client, [("宋韵楼", "初刻", "SR-1", "天"), ("宋韵楼", "初刻", "SR-2", "地")])
    total = 34
    responses = []
    for i in range(total):
        _upload_vol(client, "册甲", "天地" if i % 2 else "天")
        r = client.post("/api/volumes/册甲/submit")
        assert r.status_code == 200, r.text
        responses.append(r.json())

    # 34 张新单：快照落在第 1、33 张（任意连续 32 张内至多一份）
    conn = database.connect()
    try:
        snaps = conn.execute("SELECT seq FROM pool_snapshots ORDER BY seq").fetchall()
        assert [s["seq"] for s in snaps] == [1, 33]
        deltas = conn.execute("SELECT COUNT(*) AS c FROM pool_deltas").fetchone()["c"]
        assert deltas == total
    finally:
        conn.close()

    # 任意一单与其之前最近快照的距离 <= 31，即还原最多读取 32 张单的增减记录
    snap_seqs = [1, 33]
    for d in responses:
        nearest = max(s for s in snap_seqs if s <= d["seq"])
        assert d["seq"] - nearest + 1 <= 32

    # 逐单重取，还原结果与交印响应完全一致
    for d in responses:
        got = client.get(f"/api/orders/{d['order_no']}").json()
        assert got["pool_before"] == d["pool_before"]
        assert got["pool_after"] == d["pool_after"]

    # 第 33 张单的快照内容 == 第 32 张单生效后的共需清单
    conn = database.connect()
    try:
        snap = conn.execute("SELECT pool_json FROM pool_snapshots WHERE seq=33").fetchone()
        assert json.loads(snap["pool_json"]) == responses[31]["pool_after"]
    finally:
        conn.close()


# ---------- 复用原单不新增存档记录；失败一起回滚；清空后从空清单接续 ----------


def test_reuse_adds_no_archive_rows(client):
    _upload_lib(client, [("宋韵楼", "初刻", "SR-1", "天")])
    _upload_vol(client, "册甲", "天")
    h = {"Idempotency-Key": "job-arch-1"}
    d1 = client.post("/api/volumes/册甲/submit", headers=h).json()
    assert _archive_counts() == (1, 1)

    # 幂等键复用：不新增增减/快照记录，响应仍含完整共需清单
    d2 = client.post("/api/volumes/册甲/submit", headers=h).json()
    assert d2["reused"] is True and d2["order_no"] == d1["order_no"]
    assert d2["pool_before"] == d1["pool_before"] and d2["pool_after"] == d1["pool_after"]
    assert _archive_counts() == (1, 1)

    # 无键重复确认复用：同样不新增存档记录
    d3 = client.post("/api/volumes/册甲/submit").json()
    assert d3["reused"] is True and d3["order_no"] == d1["order_no"]
    assert _archive_counts() == (1, 1)


def test_fail_commit_rolls_back_archive_rows(client):
    _upload_lib(client, [("宋韵楼", "初刻", "SR-1", "天"), ("宋韵楼", "初刻", "SR-2", "地")])
    _upload_vol(client, "册甲", "天")
    client.post("/api/volumes/册甲/submit")
    assert _archive_counts() == (1, 1)

    _upload_vol(client, "册甲", "天地")
    r = client.post("/api/volumes/册甲/submit", headers={"X-Fail-Commit": "1"})
    assert r.status_code == 500
    # 单据与恢复所需数据一起回滚
    assert _archive_counts() == (1, 1)
    assert len(client.get("/api/orders").json()) == 1


def test_reset_clears_archive_and_restarts_from_empty(client):
    _upload_lib(client, [("宋韵楼", "初刻", "SR-1", "天")])
    _upload_vol(client, "册甲", "天")
    client.post("/api/volumes/册甲/submit")
    assert _archive_counts() == (1, 1)

    client.post("/api/reset")
    assert _archive_counts() == (0, 0)

    # 清空工位后从空清单接续：新首单快照为空池，重取还原正确
    _upload_lib(client, [("宋韵楼", "初刻", "SR-1", "天")])
    _upload_vol(client, "册甲", "天")
    d = client.post("/api/volumes/册甲/submit").json()
    assert d["pool_before"] == [] and len(d["pool_after"]) == 1
    got = client.get(f"/api/orders/{d['order_no']}").json()
    assert got["pool_before"] == [] and got["pool_after"] == d["pool_after"]
    assert _archive_counts() == (1, 1)


# ---------- 旧 SQLite 库直接打开：旧单原样重取，首张新单接续末张旧单 ----------


def _build_old_db(path: str) -> dict:
    """构造一个旧版本（整单快照、无 pool_deltas/pool_snapshots）的数据库。"""
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    try:
        conn.executescript(
            """
            CREATE TABLE libraries (
                library_id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL, edition TEXT NOT NULL,
                priority INTEGER NOT NULL, current INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL DEFAULT (datetime('now')),
                UNIQUE(name, edition)
            );
            CREATE TABLE glyphs (
                glyph_id INTEGER PRIMARY KEY AUTOINCREMENT,
                library_id INTEGER NOT NULL REFERENCES libraries(library_id),
                matrix_no TEXT NOT NULL, glyph_char TEXT NOT NULL,
                UNIQUE(library_id, matrix_no)
            );
            CREATE TABLE volumes (
                volume_id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL UNIQUE,
                created_at TEXT NOT NULL DEFAULT (datetime('now'))
            );
            CREATE TABLE volume_chars (
                volume_id INTEGER NOT NULL, glyph_char TEXT NOT NULL,
                source_order INTEGER NOT NULL,
                PRIMARY KEY (volume_id, glyph_char)
            );
            CREATE TABLE active_matrices (
                volume_id INTEGER NOT NULL, glyph_id INTEGER NOT NULL,
                PRIMARY KEY (volume_id, glyph_id)
            );
            CREATE TABLE submit_seq (seq INTEGER NOT NULL);
            CREATE TABLE orders (
                order_no TEXT PRIMARY KEY, seq INTEGER NOT NULL UNIQUE,
                volume_id INTEGER NOT NULL, volume_name TEXT NOT NULL,
                idem_key TEXT, fingerprint TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT (datetime('now'))
            );
            """
        )
        conn.execute("INSERT INTO submit_seq(seq) VALUES (1)")
        lid = conn.execute(
            "INSERT INTO libraries(name, edition, priority, current) VALUES ('宋韵楼','初刻',1,1)"
        ).lastrowid
        gids = {}
        for no, ch in (("SR-1", "天"), ("SR-2", "地"), ("SR-3", "人")):
            gids[ch] = conn.execute(
                "INSERT INTO glyphs(library_id, matrix_no, glyph_char) VALUES (?,?,?)",
                (lid, no, ch),
            ).lastrowid
        vid = conn.execute("INSERT INTO volumes(name) VALUES ('册甲')").lastrowid
        for i, ch in enumerate(("天", "地")):
            conn.execute(
                "INSERT INTO volume_chars(volume_id, glyph_char, source_order) VALUES (?,?,?)",
                (vid, ch, i),
            )
            conn.execute(
                "INSERT INTO active_matrices(volume_id, glyph_id) VALUES (?,?)",
                (vid, gids[ch]),
            )
        old_payload = {
            "order_no": "MO20260101-000001",
            "seq": 1,
            "volume": "册甲",
            "idem_key": None,
            "reused": False,
            "fingerprint": "old-fingerprint",
            "effective_at": "2026-01-01T09:00:00+08:00",
            "pool_before": [],
            "pool_after": [_ident("天", no="SR-1"), _ident("地", no="SR-2")],
            "added": [_ident("天", no="SR-1"), _ident("地", no="SR-2")],
            "removed": [],
            "volume_before": [],
            "volume_after": [_ident("天", no="SR-1"), _ident("地", no="SR-2")],
        }
        conn.execute(
            "INSERT INTO orders(order_no, seq, volume_id, volume_name, idem_key, "
            "fingerprint, payload_json) VALUES (?,?,?,?,?,?,?)",
            ("MO20260101-000001", 1, vid, "册甲", None, "old-fingerprint",
             json.dumps(old_payload, ensure_ascii=False)),
        )
        conn.commit()
        return old_payload
    finally:
        conn.close()


def test_old_database_opens_and_continues(tmp_path, monkeypatch):
    old_db = tmp_path / "old.db"
    old_payload = _build_old_db(str(old_db))
    monkeypatch.setattr(database, "DB_PATH", str(old_db))

    # 旧库直接打开：新表自动补齐，旧单存档字节不动
    service.ensure_db()
    got_old = service.get_order("MO20260101-000001")
    assert got_old == old_payload
    conn = database.connect()
    try:
        raw = conn.execute(
            "SELECT payload_json FROM orders WHERE order_no='MO20260101-000001'"
        ).fetchone()[0]
        assert json.loads(raw) == old_payload
    finally:
        conn.close()

    # 首张新单接续末张旧单生效后的共需清单
    service.replace_volume_chars("册甲", ["天", "地", "人"])
    d = service.submit_volume("册甲")
    assert d["seq"] == 2
    assert d["pool_before"] == old_payload["pool_after"]
    assert {x["glyph_char"] for x in d["pool_after"]} == {"天", "地", "人"}
    assert [x["glyph_char"] for x in d["added"]] == ["人"]

    # 首张新单存了一份完整快照（即末张旧单生效后的共需清单）+ 自身增减记录
    assert _archive_counts() == (1, 1)
    conn = database.connect()
    try:
        snap = conn.execute("SELECT pool_json FROM pool_snapshots WHERE seq=2").fetchone()
        assert json.loads(snap["pool_json"]) == old_payload["pool_after"]
    finally:
        conn.close()

    # 新单按单号重取还原一致；旧单仍原样重取
    assert service.get_order(d["order_no"])["pool_after"] == d["pool_after"]
    assert service.get_order("MO20260101-000001") == old_payload
