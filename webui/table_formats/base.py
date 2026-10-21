"""TableFormat —— 出表类型接口的唯一契约（round37 §七）。

网页只认这个契约：选一种表类型 ⇒ 问它三件事
  ① 跑批要不要额外参数（cli_args）
  ② 产物在哪、行列多少（load）
  ③ 页面预览哪几列（preview_columns，服务端一律打码后返回）

加第二种表类型时**只加文件、只注册进 REGISTRY**，不改网页的其它逻辑。
"""
from __future__ import annotations

from pathlib import Path


class TableFormat:
    id: str = ""
    display_name: str = ""
    description: str = ""
    # 相对批次目录的产物 glob（页面「下载」按钮按这个找文件）
    output_globs: list[str] = []
    # 页面预览列（表头名）；服务端返回前必须过 pii.mask_value
    preview_columns: list[str] = []

    def check_prereq(self, batch_dir: Path) -> tuple[bool, str]:
        """跑批前：这种表需不需要产线具备什么。返回 (ok, 原因)。"""
        raise NotImplementedError

    def load(self, batch_dir: Path) -> dict:
        """跑批后：读产物，返回表元数据（rows / cols / headers / files）。"""
        raise NotImplementedError

    def cli_args(self) -> list[str]:
        """追加到 `medrec run` 后面的参数。产线本轮没有出表类型旗标 ⇒ 必须返回 []。"""
        raise NotImplementedError

    # 通用：给页面用的自述
    def describe(self) -> dict:
        return {
            "id": self.id,
            "display_name": self.display_name,
            "description": self.description,
            "output_globs": list(self.output_globs),
            "preview_columns": list(self.preview_columns),
            "cli_args": self.cli_args(),
        }
