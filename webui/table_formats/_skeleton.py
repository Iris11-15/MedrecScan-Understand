"""LongFormat —— **占位**：第二种出表类型（一行一变量 / 长表）。

⚠ 故意**不注册进 REGISTRY**（round37 §7.2「未知需求不要猜」）。
    本页当前只暴露 wide 一种；这里只把「接第二种时要改哪几处」写清楚。

接第二种表类型时要改的 5 处（缺一不可）：
  1. config/columns.yaml —— 由**人**加列/加模板并升 version（红线⑤，网页不许改）。
  2. 产线出表代码 bin/medrec_cli.py + src/wide/s6_table.py —— 由**人**加旗标
     （例如 --table-format long）。本轮 bin/ 与 src/wide/ 禁写，网页也不许发明旗标。
  3. 本文件 —— 实现 check_prereq()/load()/cli_args()，cli_args() 返回产线认可的真实旗标。
  4. table_formats/__init__.py 的 REGISTRY —— 加一行 "long": LongFormat()。
  5. static/index.html —— 下拉框会自动从 /api/table_formats 长出来，无需改前端；
     但「下载」按钮按 output_globs 找文件，产物文件名要在这里写对。
"""
from __future__ import annotations

from pathlib import Path

from .base import TableFormat


class LongFormat(TableFormat):
    id = "long"
    display_name = "长表（一行一变量 · 未实现）"
    description = "占位。产线还没有出长表的能力，因此不注册、页面选不到。"
    output_globs = ["06_table/long.csv"]
    preview_columns = []

    def check_prereq(self, batch_dir: Path) -> tuple[bool, str]:
        return False, "未实现：产线还没有长表出表路径（需人先改 columns.yaml 与 s6_table.py）"

    def load(self, batch_dir: Path) -> dict:
        raise NotImplementedError("LongFormat 是占位，未实现；见模块 docstring 的 5 处改动清单")

    def cli_args(self) -> list[str]:
        raise NotImplementedError("LongFormat 未实现；不许凭空发明产线不认的旗标")
