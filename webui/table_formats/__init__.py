"""出表类型注册表（round37 §七）。

未知 id ⇒ 抛 UnknownTableFormat，**不静默回落**到 wide。
_skeleton.LongFormat 故意不在这里（§7.2）。
"""
from __future__ import annotations

from .base import TableFormat
from .wide import WideFormat


class UnknownTableFormat(KeyError):
    """页面/调用方给了一个注册表里没有的表类型 id。"""


REGISTRY: dict[str, TableFormat] = {
    "wide": WideFormat(),
}


def load_registry(fmt_id: str | None = None) -> dict | TableFormat:
    """不带参数 ⇒ 返回 {id: 自述 dict}；带 id ⇒ 返回该 TableFormat 实例。

    未知 id 直接抛 UnknownTableFormat（明确异常，不是回落）。
    """
    if fmt_id is None:
        return {k: v.describe() for k, v in REGISTRY.items()}
    if fmt_id not in REGISTRY:
        known = "、".join(sorted(REGISTRY)) or "（空）"
        raise UnknownTableFormat(
            f"未知出表类型 id={fmt_id!r}；已注册：{known}。"
            "要加新表类型请先按 table_formats/_skeleton.py 的清单改产线与配置。")
    return REGISTRY[fmt_id]


def ids() -> list[str]:
    return sorted(REGISTRY)
