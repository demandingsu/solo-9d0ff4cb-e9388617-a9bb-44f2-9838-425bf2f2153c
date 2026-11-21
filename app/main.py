"""FastAPI 路由与页面。"""
from __future__ import annotations

import csv
import io
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, File, Header, UploadFile
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles

from . import service
from .errors import DomainError
from .parsers import parse_library_csv, parse_volume_csv

WEB_DIR = Path(__file__).resolve().parent.parent / "web"
SAMPLES_DIR = Path(__file__).resolve().parent.parent / "samples"


@asynccontextmanager
async def lifespan(_app: FastAPI):
    service.ensure_db()
    yield


app = FastAPI(
    title="古籍制版字模工位管理",
    description=(
        "上传分册用字表与按优先顺序排列的字库清单；每字取第一个收录它的字库，"
        "按字库、版次、字模号识别字模；交印出增减单，支持并发生效次序与原单复用。"
    ),
    version="1.0.0",
    lifespan=lifespan,
)


@app.exception_handler(DomainError)
async def _domain_handler(_request, exc: DomainError):
    return JSONResponse(
        status_code=exc.status_code,
        content={"error": exc.message, "type": exc.__class__.__name__},
    )


@app.get("/health")
def health():
    return {"status": "ok"}


# ---------- 上传 ----------


@app.post("/api/libraries", summary="上传（替换）按优先顺序排列的字库清单")
async def upload_libraries(file: UploadFile = File(...)):
    data = await file.read()
    rows = parse_library_csv(data)
    return service.replace_libraries(rows)


@app.post("/api/volumes/{volume_name}/chars", summary="上传（替换）某册用字表")
async def upload_volume_chars(volume_name: str, file: UploadFile = File(...)):
    data = await file.read()
    chars = parse_volume_csv(data)
    return service.replace_volume_chars(volume_name, chars)


# ---------- 交印 ----------


@app.post("/api/volumes/{volume_name}/submit", summary="交印一册并生成增减单")
async def submit(
    volume_name: str,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    x_fail_commit: str | None = Header(default=None, alias="X-Fail-Commit"),
):
    fail = (x_fail_commit or "").lower() in ("1", "true", "yes")
    return service.submit_volume(volume_name, idempotency_key, fail_commit=fail)


# ---------- 单据 ----------


@app.get("/api/orders", summary="按生效次序列出全部交印单")
def orders(volume: str | None = None):
    return service.list_orders(volume)


@app.get("/api/orders/{order_no}", summary="按单号原样重取增减单（JSON）")
def order_detail(order_no: str):
    return service.get_order(order_no)


def _order_csv(payload: dict) -> str:
    buf = io.StringIO()
    buf.write("﻿")
    w = csv.writer(buf)
    w.writerow([f"增减单号 {payload['order_no']}（册：{payload['volume']}，生效时间：{payload['effective_at']}）"])
    w.writerow(["类型", "字库", "版次", "字模号", "字"])
    for x in payload["added"]:
        w.writerow(["增", x["library_name"], x["edition"], x["matrix_no"], x["glyph_char"]])
    for x in payload["removed"]:
        w.writerow(["减", x["library_name"], x["edition"], x["matrix_no"], x["glyph_char"]])
    w.writerow([])
    w.writerow([f"交印前共需 {len(payload['pool_before'])} 个，交印后共需 {len(payload['pool_after'])} 个"])
    return buf.getvalue()


@app.get("/api/orders/{order_no}/download", summary="下载增减单 CSV")
def order_download(order_no: str):
    payload = service.get_order(order_no)
    csv_text = _order_csv(payload)
    return Response(
        content=csv_text.encode("utf-8"),
        media_type="text/csv; charset=utf-8",
        headers={
            "Content-Disposition": f'attachment; filename="{order_no}.csv"'
        },
    )


# ---------- 状态 ----------


@app.get("/api/state", summary="查看当前各册用字、已选字模与共需并集")
def state():
    return service.get_state()


@app.post("/api/reset", summary="清空全部数据（演示用）")
def reset():
    service.reset_all()
    return {"status": "reset"}


# ---------- 网页 ----------


@app.get("/", include_in_schema=False)
def index():
    return FileResponse(WEB_DIR / "index.html")


# 静态示例文件
app.mount("/samples", StaticFiles(directory=str(SAMPLES_DIR)), name="samples")
