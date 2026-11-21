"""端到端测试：用字表/字库上传、交印出单、原单复用、缺字、保存失败、并发次序。

运行：
    PYTHONPATH=. pytest -q
"""
from __future__ import annotations

import io
import os
import tempfile
import threading

import pytest

# 每个测试会话使用独立临时库
_tmp = tempfile.TemporaryDirectory()
os.environ["MATRIX_DB_PATH"] = os.path.join(_tmp.name, "test.db")

from fastapi.testclient import TestClient  # noqa: E402

from app import service  # noqa: E402
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


def _ident(glyph: str, lib: str, edition: str, no: str) -> dict:
    return {
        "glyph_char": glyph,
        "library_name": lib,
        "edition": edition,
        "matrix_no": no,
    }


# ---------- 基础：优先级选库 ----------


def test_first_library_wins(client):
    r = _upload_lib(client, [
        ("宋韵楼", "初刻", "SR-1", "天"),
        ("宋韵楼", "初刻", "SR-2", "地"),
        ("金石斋", "重刊", "JS-1", "天"),  # 同字次优先，不应被选
        ("金石斋", "重刊", "JS-2", "人"),
    ])
    assert r.status_code == 200, r.text
    _upload_vol(client, "册甲", "天地人")
    r = client.post("/api/volumes/册甲/submit")
    assert r.status_code == 200, r.text
    d = r.json()
    after = {(x["glyph_char"], x["library_name"], x["matrix_no"]) for x in d["volume_after"]}
    assert ("天", "宋韵楼", "SR-1") in after
    assert ("地", "宋韵楼", "SR-2") in after
    assert ("人", "金石斋", "JS-2") in after
    assert all(x["library_name"] != "金石斋" or x["glyph_char"] != "天" for x in d["volume_after"])
    # 首单：池从 0 增到 3，无减项
    assert len(d["added"]) == 3 and d["removed"] == []


# ---------- 核心场景：两册先后弃用同一字模 ----------


def test_two_volumes_drop_same_matrix_sequentially(client):
    _upload_lib(client, [
        ("宋韵楼", "初刻", "SR-1", "天"),
        ("宋韵楼", "初刻", "SR-2", "地"),
        ("宋韵楼", "初刻", "SR-3", "人"),
    ])
    _upload_vol(client, "册甲", "天地人")
    _upload_vol(client, "册乙", "天地")

    # 册甲先交印：共需池 = 天/地/人
    o1 = client.post("/api/volumes/册甲/submit").json()
    assert {x["glyph_char"] for x in o1["pool_after"]} == {"天", "地", "人"}

    # 册乙交印：与册甲共需并集仍是 天/地/人，故无增减
    o2 = client.post("/api/volumes/册乙/submit").json()
    assert o2["added"] == [] and o2["removed"] == []
    assert {x["glyph_char"] for x in o2["pool_after"]} == {"天", "地", "人"}

    # 册甲改稿，弃用“人”：第一次弃用——池里还有乙吗？乙不用“人”，故立即减
    _upload_vol(client, "册甲", "天地")
    o3 = client.post("/api/volumes/册甲/submit").json()
    assert [x["glyph_char"] for x in o3["removed"]] == ["人"]
    assert o3["added"] == []
    assert {x["glyph_char"] for x in o3["pool_after"]} == {"天", "地"}

    # 构造“两册共用、先后弃用”：册乙改稿加入“人”，两册再共需
    _upload_vol(client, "册乙", "天地人")
    o4 = client.post("/api/volumes/册乙/submit").json()
    assert [x["glyph_char"] for x in o4["added"]] == ["人"]

    # 现在甲（天地）、乙（天地人）共用“人”只在乙；让两册都含“人”后依次弃用：
    _upload_vol(client, "册甲", "天地人")
    client.post("/api/volumes/册甲/submit")
    state = client.get("/api/state").json()
    assert {x["glyph_char"] for x in state["shared_pool"]} == {"天", "地", "人"}

    # 甲先弃“人”：乙仍需，池不变 → 无减单
    _upload_vol(client, "册甲", "天地")
    o5 = client.post("/api/volumes/册甲/submit").json()
    assert o5["removed"] == [] and o5["added"] == []
    state = client.get("/api/state").json()
    assert {x["glyph_char"] for x in state["shared_pool"]} == {"天", "地", "人"}
    # 甲自身清单确实已更新
    jia = next(v for v in state["volumes"] if v["volume"] == "册甲")
    assert {x["glyph_char"] for x in jia["active_matrices"]} == {"天", "地"}

    # 乙再弃“人”：最后一个使用者撤下 → 减单含“人”
    _upload_vol(client, "册乙", "天地")
    o6 = client.post("/api/volumes/册乙/submit").json()
    assert [x["glyph_char"] for x in o6["removed"]] == ["人"]
    assert {x["glyph_char"] for x in o6["pool_after"]} == {"天", "地"}

    # 逐单后校验：每单 pool_after 都与当时各册 active 并集一致
    for order in (o1, o2, o3, o4, o5, o6):
        assert len(order["pool_after"]) == len({
            (x["library_name"], x["edition"], x["matrix_no"]) for x in order["pool_after"]
        })
    final = client.get("/api/state").json()
    assert {x["glyph_char"] for x in final["shared_pool"]} == {"天", "地"}


# ---------- 原单复用：幂等键 / 指纹 / 按单号重取 ----------


def test_idempotency_key_reuses_same_order(client):
    _upload_lib(client, [("宋韵楼", "初刻", "SR-1", "天")])
    _upload_vol(client, "册甲", "天")
    h = {"Idempotency-Key": "job-001"}
    d1 = client.post("/api/volumes/册甲/submit", headers=h).json()
    d2 = client.post("/api/volumes/册甲/submit", headers=h).json()
    assert d1["order_no"] == d2["order_no"]
    assert d1["reused"] is False and d2["reused"] is True
    # 按单号原样重取，内容一致
    d3 = client.get(f"/api/orders/{d1['order_no']}").json()
    assert d3["added"] == d1["added"] and d3["removed"] == d1["removed"]
    assert d3["volume_after"] == d1["volume_after"]


def test_duplicate_confirm_without_key_reuses_order(client):
    _upload_lib(client, [("宋韵楼", "初刻", "SR-1", "天"), ("宋韵楼", "初刻", "SR-2", "地")])
    _upload_vol(client, "册甲", "天地")
    d1 = client.post("/api/volumes/册甲/submit").json()
    # 不做任何改动，重复确认同一笔
    d2 = client.post("/api/volumes/册甲/submit").json()
    assert d1["order_no"] == d2["order_no"] and d2["reused"] is True
    # 改稿后再交印，应为新单
    _upload_vol(client, "册甲", "天")
    d3 = client.post("/api/volumes/册甲/submit").json()
    assert d3["order_no"] != d1["order_no"] and d3["reused"] is False
    assert [x["glyph_char"] for x in d3["removed"]] == ["地"]


def test_order_csv_download(client):
    _upload_lib(client, [("宋韵楼", "初刻", "SR-1", "天")])
    _upload_vol(client, "册甲", "天")
    d = client.post("/api/volumes/册甲/submit").json()
    r = client.get(f"/api/orders/{d['order_no']}/download")
    assert r.status_code == 200
    assert "text/csv" in r.headers["content-type"]
    text = r.content.decode("utf-8-sig")
    assert d["order_no"] in text and "增" in text and "天" in text


# ---------- 缺字：沿用原清单 ----------


def test_missing_glyph_keeps_old_list(client):
    _upload_lib(client, [("宋韵楼", "初刻", "SR-1", "天")])
    _upload_vol(client, "册甲", "天")
    d1 = client.post("/api/volumes/册甲/submit").json()

    _upload_vol(client, "册甲", "天人")  # “人”无字库收录
    r = client.post("/api/volumes/册甲/submit")
    assert r.status_code == 422
    err = r.json()
    assert err["type"] == "MissingGlyphError" and "人" in err["error"]

    state = client.get("/api/state").json()
    jia = next(v for v in state["volumes"] if v["volume"] == "册甲")
    # 仍沿用上次交印清单（只有“天”）
    assert [x["glyph_char"] for x in jia["active_matrices"]] == ["天"]
    # 没有产生新单
    orders = client.get("/api/orders").json()
    assert [o["order_no"] for o in orders] == [d1["order_no"]]


# ---------- 保存失败：回滚并沿用原清单 ----------


def test_save_failure_rolls_back(client):
    _upload_lib(client, [
        ("宋韵楼", "初刻", "SR-1", "天"),
        ("宋韵楼", "初刻", "SR-2", "地"),
    ])
    _upload_vol(client, "册甲", "天地")
    d1 = client.post("/api/volumes/册甲/submit").json()

    _upload_vol(client, "册甲", "天")
    r = client.post("/api/volumes/册甲/submit", headers={"X-Fail-Commit": "1"})
    assert r.status_code == 500
    assert r.json()["type"] == "SaveError"

    state = client.get("/api/state").json()
    jia = next(v for v in state["volumes"] if v["volume"] == "册甲")
    assert {x["glyph_char"] for x in jia["active_matrices"]} == {"天", "地"}
    orders = client.get("/api/orders")
    assert [o["order_no"] for o in orders.json()] == [d1["order_no"]]


# ---------- 并发：多人同时交印按生效次序逐单一致 ----------


def test_concurrent_submits_serialize_and_stay_consistent(client):
    _upload_lib(client, [
        ("宋韵楼", "初刻", "SR-1", "天"),
        ("宋韵楼", "初刻", "SR-2", "地"),
        ("宋韵楼", "初刻", "SR-3", "人"),
    ])
    _upload_vol(client, "册甲", "天")
    _upload_vol(client, "册乙", "地")
    _upload_vol(client, "册丙", "人")

    results: list[dict] = []
    errors: list[Exception] = []
    barrier = threading.Barrier(3)

    def worker(vol: str):
        try:
            barrier.wait()
            with TestClient(app) as local:
                resp = local.post(f"/api/volumes/{vol}/submit")
                assert resp.status_code == 200, resp.text
                results.append(resp.json())
        except Exception as exc:  # pragma: no cover - 测试失败路径
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(v,)) for v in ("册甲", "册乙", "册丙")]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors, errors

    # 三单全部生效，序号连续、单号唯一
    results.sort(key=lambda d: d["seq"])
    assert [d["seq"] for d in results] == [1, 2, 3]
    assert len({d["order_no"] for d in results}) == 3
    # 逐单累积：第 n 单 pool_after 大小 == n（三字互不相同）
    for i, d in enumerate(results, start=1):
        assert len(d["pool_after"]) == i
        assert len(d["added"]) == 1 and d["removed"] == []

    state = client.get("/api/state").json()
    assert {x["glyph_char"] for x in state["shared_pool"]} == {"天", "地", "人"}
    listing = client.get("/api/orders").json()
    assert [o["seq"] for o in listing] == [1, 2, 3]


# ---------- 其他 ----------


def test_unknown_volume_and_order(client):
    r = client.post("/api/volumes/不存在/submit")
    assert r.status_code == 404
    r = client.get("/api/orders/NOPE-9")
    assert r.status_code == 404


def test_bad_csv_headers(client):
    r = client.post(
        "/api/libraries",
        files={"file": ("x.csv", _csv("foo,bar\n1,2\n"), "text/csv")},
    )
    assert r.status_code == 400 and "缺少必需列" in r.json()["error"]
