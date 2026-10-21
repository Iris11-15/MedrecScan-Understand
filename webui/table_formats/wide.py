"""WideFormat —— 本轮唯一实现的出表类型：宽表（一行一例 · 23 列）。

产线 `bin/medrec run` 本来就出 06_table/wide.csv，所以 cli_args() 必须返回 []：
**不许**发明 --table-format 之类的旗标去改产线（round37 §7.2，bin/ 与 src/wide/ 本轮禁写）。
"""
from __future__ import annotations

import csv
import glob as globmod
import os
from pathlib import Path

from .base import TableFormat

CSV_REL = "06_table/wide.csv"


class WideFormat(TableFormat):
    id = "wide"
    display_name = "宽表（一行一例 · 23 列）"
    description = ("产线默认产物 run/wide/<批次>/06_table/wide.csv；"
                   "列数 = config/columns.yaml 的列数 − normalize.drop_output_columns。")
    output_globs = [CSV_REL, "06_table/wide.xlsx"]
    # 预览列（服务端返回前一律打码）
    preview_columns = ["序号", "来源目录", "病案号", "姓名", "年龄",
                       "入院时间（住院时间）", "出院日期", "主诉"]

    def check_prereq(self, batch_dir: Path) -> tuple[bool, str]:
        return True, "宽表是产线默认出表，无需前置条件"

    def cli_args(self) -> list[str]:
        return []

    def files(self, batch_dir: Path) -> list[dict]:
        out = []
        for pat in self.output_globs:
            for p in sorted(globmod.glob(str(Path(batch_dir) / pat))):
                try:
                    st = os.stat(p)
                except OSError:
                    continue
                out.append({"path": p, "bytes": st.st_size, "mtime": int(st.st_mtime)})
        return out

    def load(self, batch_dir: Path) -> dict:
        """读 wide.csv ⇒ 表元数据。列数按表头算（宽表里有重名列，按**出现次数**计）。"""
        csv_path = Path(batch_dir) / CSV_REL
        if not csv_path.is_file():
            raise FileNotFoundError(f"没有 {csv_path}")
        with open(csv_path, newline="", encoding="utf-8-sig") as fh:
            rd = csv.reader(fh)
            try:
                head = next(rd)
            except StopIteration:
                head = []
            rows = sum(1 for _ in rd)
        return {
            "id": self.id,
            "display_name": self.display_name,
            "rows": rows,
            "cols": len(head),
            "headers": head,
            "csv": str(csv_path),
            "files": self.files(batch_dir),
        }
