#!/usr/bin/env bash
# ============================================================
# medrec F5 升级包安装脚本（幂等，可重复执行）
#
#   1) 先对**全部**文件做 sha256 校验 —— 任一不符就中止，
#      绝不写入任何文件（防传输损坏把环境改坏）
#   2) 内容已相同的文件直接跳过（不动时间戳）
#   3) 需要替换的先备份成 <名字>.bak.<月日时分>
#   4) 只往目标目录写下面 FILES 里列出的文件，**不删除、不触碰任何其他文件**
#
# 用法:
#   bash install.sh              # 装到 ~/medrec/src
#   bash install.sh /some/dir
# ============================================================
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DST="${1:-$HOME/medrec/src}"
STAMP="$(date +%m%d%H%M)"
FILES="f3_system.txt f3_schema.json f4_columns.json f4_alias.json f4_table.py f4_check.py batch_run.sh to_xlsx.py structure.py"

if [ ! -d "$DST" ]; then
  echo "[x] 目标目录不存在：$DST"
  exit 2
fi
if ! command -v sha256sum >/dev/null 2>&1; then
  echo "[x] 找不到 sha256sum，无法校验，中止"
  exit 2
fi
if [ ! -f "$HERE/SHA256SUMS" ]; then
  echo "[x] 缺少 SHA256SUMS，中止"
  exit 2
fi

echo "== 源    : $HERE"
echo "== 目标  : $DST"
echo
echo "== 第 1 步：全量 sha256 校验（不通过就不动任何文件）"
bad=0
for f in $FILES; do
  src="$HERE/$f"
  if [ ! -f "$src" ]; then
    echo "  [x] 包内缺少 $f"
    bad=1
    continue
  fi
  want="$(awk -v n="$f" '$2==n || $2=="*"n {print $1; exit}' "$HERE/SHA256SUMS")"
  got="$(sha256sum "$src" | cut -d' ' -f1)"
  if [ -z "$want" ]; then
    echo "  [x] $f 不在 SHA256SUMS 里"
    bad=1
  elif [ "$want" != "$got" ]; then
    echo "  [x] $f sha256 不符"
    echo "      期望 $want"
    echo "      实际 $got"
    bad=1
  else
    echo "  ✓ $f"
  fi
done
if [ "$bad" != 0 ]; then
  echo
  echo "[x] 校验未通过 —— 包可能在传输中损坏。已中止，**未改动任何文件**。"
  exit 1
fi

echo
echo "== 第 2 步：安装"
n_new=0; n_same=0; n_bak=0
for f in $FILES; do
  t="$DST/$f"
  if [ -f "$t" ] && cmp -s "$HERE/$f" "$t"; then
    echo "  = $f（内容已相同，跳过）"
    n_same=$((n_same + 1))
    continue
  fi
  if [ -f "$t" ]; then
    cp -a "$t" "$t.bak.$STAMP" && echo "  ↺ $f（旧版已备份为 $(basename "$t").bak.$STAMP）"
    n_bak=$((n_bak + 1))
  fi
  cp -f "$HERE/$f" "$t" || { echo "  [x] 写入 $f 失败"; exit 1; }
  echo "  + $f"
  n_new=$((n_new + 1))
done

for f in batch_run.sh medrec; do
  [ -f "$DST/$f" ] && chmod +x "$DST/$f" 2>/dev/null
done

echo
echo "== 第 3 步：校验安装结果"
bad=0
for f in $FILES; do
  want="$(awk -v n="$f" '$2==n || $2=="*"n {print $1; exit}' "$HERE/SHA256SUMS")"
  got="$(sha256sum "$DST/$f" | cut -d' ' -f1)"
  if [ "$want" != "$got" ]; then
    echo "  [x] $f 安装后 sha256 不符"
    bad=1
  fi
done
if [ "$bad" != 0 ]; then
  echo "[x] 安装结果校验失败。回滚：把 $DST/*.bak.$STAMP 覆盖回原文件名"
  exit 1
fi
echo "  ✓ 全部 $(echo $FILES | wc -w) 个文件就位且 sha256 一致"

echo
echo "== 完成：新增/替换 $n_new 个，跳过 $n_same 个，备份 $n_bak 份"
echo "   回滚：cp -a $DST/<文件>.bak.$STAMP $DST/<文件>"
echo
echo "== 下一步见 UPGRADE-F5.md"
