"""出表类型接口自测（round37 §7.3）。

跑法：
  /home/ubuntu/anaconda3/envs/vl-env/bin/python /home/ubuntu/medrec/webui/table_formats/_selftest.py [批次目录]
默认批次目录 = run/wide/r36_a（既有产物，只读）。
"""
from __future__ import annotations

import sys
from pathlib import Path

WEBUI_DIR = Path(__file__).resolve().parent.parent
if str(WEBUI_DIR) not in sys.path:
    sys.path.insert(0, str(WEBUI_DIR))

from table_formats import REGISTRY, UnknownTableFormat, load_registry  # noqa: E402

DEFAULT_BATCH = "/home/ubuntu/medrec/run/wide/r36_a"
EXPECT_COLS = 23


def main() -> int:
    batch_dir = Path(sys.argv[1] if len(sys.argv) > 1 else DEFAULT_BATCH)
    fails = []

    def chk(name: str, ok: bool, got: object = "") -> None:
        print(f"{'PASS' if ok else 'FAIL'}  {name}  {got}")
        if not ok:
            fails.append(name)

    chk("REGISTRY 只有 wide", list(REGISTRY) == ["wide"], f"keys={list(REGISTRY)}")
    fmt = load_registry("wide")
    chk('REGISTRY["wide"].id == "wide"', fmt.id == "wide", f"id={fmt.id}")
    chk("wide.cli_args() == []", fmt.cli_args() == [], f"cli_args={fmt.cli_args()}")
    chk("_skeleton 的 long 不在 REGISTRY", "long" not in REGISTRY,
        f"ids={sorted(REGISTRY)}")

    try:
        load_registry("long")
        chk("未知 id 抛明确异常", False, "没抛异常（静默回落）")
    except UnknownTableFormat as exc:
        chk("未知 id 抛明确异常", True, f"{type(exc).__name__}: {exc}")

    try:
        meta = fmt.load(batch_dir)
        chk(f"load({batch_dir}) 列数 == {EXPECT_COLS}", meta["cols"] == EXPECT_COLS,
            f"rows={meta['rows']} cols={meta['cols']}")
        chk("load() 有产物文件清单", bool(meta["files"]),
            f"files={[f['path'].split('/')[-1] for f in meta['files']]}")
    except Exception as exc:  # noqa: BLE001
        chk(f"load({batch_dir})", False, f"{type(exc).__name__}: {exc}")

    try:
        from table_formats._skeleton import LongFormat
        LongFormat().load(batch_dir)
        chk("_skeleton.load() 抛 NotImplementedError", False, "没抛")
    except NotImplementedError as exc:
        chk("_skeleton.load() 抛 NotImplementedError", True, str(exc))

    print(f"SELFTEST {'OK' if not fails else 'FAIL'} fails={fails}")
    return 0 if not fails else 1


if __name__ == "__main__":
    raise SystemExit(main())
