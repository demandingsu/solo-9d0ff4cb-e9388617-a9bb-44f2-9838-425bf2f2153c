"""用字表 / 字库清单 CSV 解析。"""
from __future__ import annotations

import csv
import io
from dataclasses import dataclass

from .errors import DomainError

# 允许的列名（含常见别名）
LIBRARY_ALIASES = {
    "library": ("字库", "字库名", "字库名称", "library", "library_name"),
    "edition": ("版次", "edition", "version"),
    "matrix_no": ("字模号", "字模编号", "matrix_no", "matrix", "code"),
    "glyph_char": ("字", "用字", "字符", "glyph", "char", "character"),
}
VOLUME_ALIASES = {
    "glyph_char": ("字", "用字", "字符", "glyph", "char", "character"),
}


def decode_bytes(data: bytes) -> str:
    """优先 UTF-8（含 BOM），回退 GBK；全空按空文件处理。"""
    for enc in ("utf-8-sig", "gb18030"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    raise DomainError("文件编码无法识别，请使用 UTF-8 或 GBK 编码的 CSV")


def _normalize(header: str) -> str:
    return header.strip().lstrip("﻿").lower()


def _map_headers(fieldnames, aliases: dict[str, tuple[str, ...]]) -> dict[str, int]:
    normalized = [_normalize(h or "") for h in fieldnames]
    mapping: dict[str, int] = {}
    for key, names in aliases.items():
        for name in names:
            needle = name.lower()
            if needle in normalized:
                mapping[key] = normalized.index(needle)
                break
    return mapping


@dataclass
class LibraryRow:
    library: str
    edition: str
    matrix_no: str
    glyph_char: str


@dataclass
class VolumeRow:
    glyph_char: str


def parse_library_csv(data: bytes) -> list[LibraryRow]:
    text = decode_bytes(data)
    reader = csv.reader(io.StringIO(text))
    try:
        header = next(reader)
    except StopIteration:
        raise DomainError("字库清单为空")
    mapping = _map_headers(header, LIBRARY_ALIASES)
    missing = [k for k in ("library", "edition", "matrix_no", "glyph_char") if k not in mapping]
    if missing:
        raise DomainError(
            "字库清单缺少必需列: " + ", ".join(missing)
            + "；表头需含 字库/版次/字模号/字"
        )
    rows: list[LibraryRow] = []
    seen: set[tuple[str, str, str]] = set()
    for line_no, raw in enumerate(reader, start=2):
        if not raw or all(not c.strip() for c in raw):
            continue
        try:
            row = LibraryRow(
                library=raw[mapping["library"]].strip(),
                edition=raw[mapping["edition"]].strip(),
                matrix_no=raw[mapping["matrix_no"]].strip(),
                glyph_char=raw[mapping["glyph_char"]].strip(),
            )
        except IndexError:
            raise DomainError(f"字库清单第 {line_no} 行列数不足")
        if not (row.library and row.edition and row.matrix_no and row.glyph_char):
            raise DomainError(f"字库清单第 {line_no} 行存在空值")
        if len(row.glyph_char) != 1:
            raise DomainError(f"字库清单第 {line_no} 行的“字”必须是单个字符: {row.glyph_char!r}")
        key = (row.library, row.edition, row.matrix_no)
        if key in seen:
            raise DomainError(
                f"字库清单第 {line_no} 行字模号重复: {row.library}/{row.edition}/{row.matrix_no}"
            )
        seen.add(key)
        rows.append(row)
    if not rows:
        raise DomainError("字库清单没有数据行")
    return rows


def parse_volume_csv(data: bytes) -> list[str]:
    """返回按文件顺序去重后的用字列表。"""
    text = decode_bytes(data)
    reader = csv.reader(io.StringIO(text))
    try:
        header = next(reader)
    except StopIteration:
        raise DomainError("用字表为空")
    mapping = _map_headers(header, VOLUME_ALIASES)
    if "glyph_char" not in mapping:
        # 兼容无表头、每行一个字的写法
        chars: list[str] = []
        for raw in [header] + list(reader):
            token = (raw[0] if raw else "").strip()
            if token:
                chars.extend(list(token.replace(" ", "")))
        if not chars:
            raise DomainError("用字表为空，或缺少“字/字符”列")
    else:
        idx = mapping["glyph_char"]
        chars = []
        for line_no, raw in enumerate(reader, start=2):
            if not raw or all(not c.strip() for c in raw):
                continue
            token = raw[idx].strip()
            if not token:
                continue
            # 单元格内允许多个字连写
            chars.extend(list(token.replace(" ", "")))
    ordered: list[str] = []
    seen: set[str] = set()
    for ch in chars:
        if ch not in seen:
            seen.add(ch)
            ordered.append(ch)
    if not ordered:
        raise DomainError("用字表没有任何字")
    return ordered
