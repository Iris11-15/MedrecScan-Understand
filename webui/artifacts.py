"""产物读取 + 打码（F4）。页面看到的宽表内容**默认**经过这里打码。

view_raw() 与 plain=True 返回原值（F4「本地查看」/ round41「明文开关」）：
只回浏览器，不落盘、不进 stdout/日志；每次明文访问都写审计（时间+批次+行列数，不含值）。
明文由 server 侧 `plain_allowed` 把关（启动参数 --disable-plain 可整条关掉）。
"""
from __future__ import annotations

import csv
import io
from pathlib import Path

import audit
import jobs
import pii
from table_formats import load_registry

MASK_COLS = pii.NAME_COLS | pii.ID_COLS | pii.PHONE_COLS | pii.ADDR_COLS

# 与产线 src/wide/s6_table.py 的 XLSX_NUMERIC_HEADERS 逐字一致（round42r）。
XLSX_NUMERIC_HEADERS = ("序号", "住院天数（天）")


def wide_rows(batch: str) -> tuple[list[str], list[list[str]], Path]:
    meta = load_registry("wide").load(jobs.batch_dir(batch))
    with open(meta["csv"], newline="", encoding="utf-8-sig") as fh:
        rows = list(csv.reader(fh))
    return rows[0], rows[1:], Path(meta["csv"])


def preview(batch: str, n: int = 10, plain: bool = False) -> dict:
    head, rows, _ = wide_rows(batch)
    want = load_registry("wide").preview_columns
    idx = [head.index(c) for c in want if c in head]
    n = max(1, min(n, 50))
    out = []
    for i, r in enumerate(rows[:n]):
        cells = {head[j]: (r[j] if j < len(r) else "") if plain
                 else pii.mask_value(head[j], r[j] if j < len(r) else "") for j in idx}
        out.append({"row": i, **cells})
    if plain:
        audit.write("view_preview_plain", batch,
                    {"rows": len(out), "cols": len(idx),
                     "pii_cols": [c for c in (head[j] for j in idx) if c in MASK_COLS]})
    return {"batch": batch, "rows_total": len(rows), "cols": len(head),
            "headers": head, "preview": out,
            "pii": "plain" if plain else "masked"}


def view_raw(batch: str, row: int, col: str) -> str:
    head, rows, _ = wide_rows(batch)
    if row < 0 or row >= len(rows) or col not in head:
        raise LookupError(f"行/列不存在：row={row} col={col!r}（列名须与表头逐字一致）")
    j = head.index(col)
    value = rows[row][j] if j < len(rows[row]) else ""
    audit.write("view_raw", batch, {"row": row, "col": col, "chars": len(value)})
    return value


def csv_bytes(batch: str, plain: bool = False) -> tuple[bytes, int, int]:
    """整表下载。plain=True ⇒ 不打码（调用方必须先过 server 的 plain_allowed 闸门）。"""
    head, rows, _ = wide_rows(batch)
    out = []
    if head:
        out.append(["\ufeff" + head[0]] + head[1:])
    for r in rows:
        out.append([v if plain
                    else pii.mask_value(head[j] if j < len(head) else "", v)
                    for j, v in enumerate(r)])
    buf = io.StringIO()
    csv.writer(buf, lineterminator="\n").writerows(out)
    audit.write("download_plain_csv" if plain else "download_masked_csv",
                batch, {"rows": len(rows), "cols": len(head)})
    return buf.getvalue().encode("utf-8"), len(rows), len(head)


def xlsx_bytes(batch: str, plain: bool = False) -> tuple[bytes, int, int]:
    """整表下载 xlsx（round42r）。

    磁盘上那份 06_table/wide.xlsx 是**原文**（含 PII），直接发出去等于绕过 F4 打码，
    所以这里从 wide.csv 在内存里重建：默认打码，只有 plain=True 才出原文。
    单元格格式与产线 write_xlsx 一致 —— 除 序号/住院天数 外全部文本 + number_format "@"，
    Excel/WPS 打开不会把住院号、身份证号、电话变成科学计数法或丢前导零。
    """
    try:
        from openpyxl import Workbook
    except Exception as exc:                                        # noqa: BLE001
        raise RuntimeError(f"openpyxl 不可用，出不了 xlsx：{type(exc).__name__}: {exc}") from exc
    head, rows, _ = wide_rows(batch)
    wb = Workbook()
    ws = wb.active
    ws.title = "wide"
    ws.append(head)
    num_idx = {i for i, hh in enumerate(head) if hh in XLSX_NUMERIC_HEADERS}
    for r in rows:
        out = []
        for j, v in enumerate(r):
            cell = v if plain else pii.mask_value(head[j] if j < len(head) else "", v)
            if cell == "" or cell is None:
                out.append(None)
            elif j in num_idx:
                try:
                    out.append(int(str(cell).strip()))
                except ValueError:
                    out.append(str(cell))
            else:
                out.append(str(cell))
        ws.append(out)
    for row in ws.iter_rows(min_row=2):
        for c in row:
            if (c.column - 1) not in num_idx:
                c.number_format = "@"
    buf = io.BytesIO()
    wb.save(buf)
    audit.write("download_plain_xlsx" if plain else "download_masked_xlsx",
                batch, {"rows": len(rows), "cols": len(head)})
    return buf.getvalue(), len(rows), len(head)
