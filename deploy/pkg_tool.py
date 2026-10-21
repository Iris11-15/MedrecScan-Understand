#!/usr/bin/env python3
"""round52 部署包的清单工具 —— 只被 install.sh 调用。

包内路径 -> 目标机落点的映射只在这里写一份（GROUPS），install.sh 不重复定义。
除 patchjson 之外不写任何文件。
"""
import csv
import hashlib
import json
import os
import shutil
import sys

# (kind, archive_prefix, dest_root_template, replacement_prefix)
# dest_root_template 里的 MEDREC_ROOT / HOME 由环境变量展开。
GROUPS = [
    ("code", "medrec-code/", "MEDREC_ROOT", ""),
    ("assets", "medrec-assets/models/", "MEDREC_ROOT/models/models", ""),
    ("assets", "medrec-assets/modules/", "MEDREC_ROOT/models", ""),
    ("assets", "medrec-assets/paddlex/", "HOME/.paddlex", ""),
    ("assets", "medrec-assets/smoke/", "MEDREC_ROOT", "smoke"),
    ("assets", "medrec-assets/wheels/", "MEDREC_ROOT", "wheels"),
]


def roots():
    mr = os.environ.get("MEDREC_ROOT") or os.path.join(os.path.expanduser("~"), "medrec")
    return {"MEDREC_ROOT": mr, "HOME": os.path.expanduser("~")}


def load(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            b = f.read(1 << 22)
            if not b:
                break
            h.update(b)
    return h.hexdigest()


def dest_of(archive_path):
    r = roots()
    for kind, prefix, tmpl, repl in GROUPS:
        if archive_path == prefix.rstrip("/") or archive_path.startswith(prefix):
            rest = archive_path[len(prefix):]
            base = r[tmpl.split("/")[0]]
            extra = tmpl.split("/", 1)[1] if "/" in tmpl else ""
            root = os.path.join(base, extra) if extra else base
            return os.path.join(root, (repl + "/" if repl else "") + rest)
    return None


def cmd_sha(argv):
    print(sha256_file(argv[0]))
    return 0


def cmd_check(argv):
    man, archive = load(argv[0]), argv[1]
    want = man.get("archive_sha256", "")
    got = sha256_file(archive)
    size = os.path.getsize(archive)
    if want != got:
        print("MISMATCH want=%s got=%s bytes=%d" % (want, got, size))
        return 1
    if int(man.get("archive_bytes", -1)) != size:
        print("SIZE-MISMATCH manifest=%s actual=%d" % (man.get("archive_bytes"), size))
        return 1
    print("OK sha256=%s bytes=%d files=%s total_bytes=%s"
          % (got, size, man.get("file_count"), man.get("total_bytes")))
    return 0


def cmd_field(argv):
    man = load(argv[0])
    print(man.get(argv[1], ""))
    return 0


def cmd_groups(argv):
    for kind, prefix, tmpl, repl in GROUPS:
        if argv and argv[0] != kind:
            continue
        r = roots()
        base = r[tmpl.split("/")[0]]
        extra = tmpl.split("/", 1)[1] if "/" in tmpl else ""
        root = os.path.join(base, extra) if extra else base
        print("%s\t%s\t%s" % (prefix, root, repl))
    return 0


def _root_of(tmpl):
    r = roots()
    base = r[tmpl.split("/")[0]]
    extra = tmpl.split("/", 1)[1] if "/" in tmpl else ""
    return os.path.join(base, extra) if extra else base


def cmd_group_count(argv):
    print(len(GROUPS))
    return 0


def cmd_group_field(argv):
    i, field = int(argv[0]), argv[1]
    kind, prefix, tmpl, repl = GROUPS[i]
    if field == "kind":
        print(kind)
    elif field == "prefix":
        print(prefix)
    elif field == "root":
        print(_root_of(tmpl))
    elif field == "repl":
        print(repl)
    else:
        print("BADFIELD " + field)
        return 1
    return 0


def cmd_members(argv):
    """只列【文件】成员 —— tar --transform 与目录成员不兼容：
    裸目录名（不带尾斜杠）不命中 ^prefix 正则，会被按原样路径解出（造出 medrec-assets/... 垃圾目录），
    同时 tar 已把它的子孙递归吃掉，子孙随后报 Not found in archive 且不落到正确位置。
    目录由 cmd_mkdirs 单独补。"""
    man, prefix = load(argv[0]), argv[1]
    for e in man["files"]:
        if e["kind"] == "dir":
            continue
        if e["archive"].startswith(prefix):
            print(e["archive"])
    return 0


def cmd_mkdirs(argv):
    """该组下所有目录（含空目录）在目标机上的落点，一行一个。"""
    man, prefix = load(argv[0]), argv[1]
    for e in man["files"]:
        if e["kind"] != "dir":
            continue
        if e["archive"] == prefix.rstrip("/") or e["archive"].startswith(prefix):
            d = dest_of(e["archive"])
            if d:
                print(d)
    return 0


def cmd_checkdests(argv):
    """解包后按 manifest 逐条确认落点存在（只查在不在位，sha 由 verify 负责）。
    抓的是 transform/前缀写错导致文件落到别处这类静默错。"""
    man, prefix = load(argv[0]), argv[1]
    miss = 0
    for e in man["files"]:
        if e["kind"] == "dir" or not e["archive"].startswith(prefix):
            continue
        d = dest_of(e["archive"])
        if not d or not os.path.isfile(d):
            print("MISSING " + e["archive"] + "  (期望 " + str(d) + ")")
            miss += 1
    print("checkdests prefix=%s missing=%d" % (prefix, miss))
    return 1 if miss else 0


def cmd_marker(argv):
    """该组在目标机上对应的根目录（manifest 里那条目录条目落到哪儿）。"""
    man, prefix = load(argv[0]), argv[1]
    for e in man["files"]:
        if e["kind"] == "dir" and e["archive"] == prefix.rstrip("/"):
            print(dest_of(e["archive"]))
            return 0
    for e in man["files"]:
        if e["archive"].startswith(prefix):
            print(os.path.dirname(dest_of(e["archive"])))
            return 0
    print("")
    return 1


def cmd_dest(argv):
    d = dest_of(argv[0])
    if d is None:
        print("UNMAPPED " + argv[0])
        return 1
    print(d)
    return 0


def cmd_verify(argv):
    man = load(argv[0])
    allow = set()
    if len(argv) > 1 and argv[1] and os.path.isfile(argv[1]):
        with open(argv[1], encoding="utf-8") as f:
            for line in f:
                if line.strip():
                    allow.add(line.strip())
    n_ok = n_bad = n_missing = n_patched = 0
    for e in man["files"]:
        if e["kind"] != "file":
            continue
        d = dest_of(e["archive"])
        if d is None:
            print("UNMAPPED %s" % e["archive"])
            n_bad += 1
            continue
        if not os.path.isfile(d):
            print("MISSING %s  (期望 %s)" % (e["archive"], d))
            n_missing += 1
            continue
        if sha256_file(d) != e["sha256"]:
            if e["archive"] in allow:
                print("PATCHED %s  (安装期被 install.sh 按 brief 3.5 改写，原件在 backup/)" % e["archive"])
                n_patched += 1
                continue
            print("SHA-DIFF %s  (落点 %s)" % (e["archive"], d))
            n_bad += 1
            continue
        n_ok += 1
    print("verified=%d sha_diff=%d patched=%d missing=%d total_files=%s"
          % (n_ok, n_bad, n_patched, n_missing, man.get("file_count")))
    return 0 if (n_bad == 0 and n_missing == 0) else 1


def cmd_backup(argv):
    """把将要被覆盖的既有文件挪到备份区，并打印 BACKUP 记账行。"""
    man, prefix, bakdir = load(argv[0]), argv[1], argv[2]
    n = 0
    for e in man["files"]:
        if e["kind"] != "file" or not e["archive"].startswith(prefix):
            continue
        d = dest_of(e["archive"])
        if not d or not os.path.isfile(d):
            continue
        rel = e["archive"]
        dst = os.path.join(bakdir, rel)
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        shutil.copy2(d, dst)
        print("BACKUP\t%s\t%s" % (d, dst))
        n += 1
    print("backed_up=%d" % n)
    return 0


def cmd_csvshape(argv):
    with open(argv[0], encoding="utf-8-sig", newline="") as f:
        rows = list(csv.reader(f))
    if not rows:
        print("rows=0 cols=0")
        return 1
    cols = len(rows[0])
    ragged = [i for i, r in enumerate(rows) if len(r) != cols]
    print("rows=%d cols=%d data_rows=%d ragged=%s"
          % (len(rows), cols, len(rows) - 1, ragged if ragged else "none"))
    return 0


def cmd_patchjson(argv):
    """patchjson <file> <key> <json-value> —— 改前自动备份到 <file>.bak-round52。"""
    path, key, value = argv[0], argv[1], json.loads(argv[2])
    if not os.path.isfile(path):
        print("MISSING " + path)
        return 1
    with open(path, encoding="utf-8") as f:
        doc = json.load(f)
    old = doc.get(key, "__(absent)__")
    doc[key] = value
    bak = path + ".bak-round52"
    if not os.path.exists(bak):
        shutil.copy2(path, bak)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(doc, f, ensure_ascii=False, indent=1)
        f.write("\n")
    print("PATCHED %s %s: %s -> %s (备份 %s)" % (path, key, json.dumps(old), json.dumps(value), bak))
    return 0


ROTATE_SRC = """#!/usr/bin/env bash
# rotate-logs.sh —— 日志轮转，copytruncate 语义（brief §3.5 陷阱 7）。
# ★ 不能 rename+create：产线执行器持有 O_APPEND 的旧 fd，rename 之后它继续写旧 inode，日志会静默断流。
# 用法：bash rotate-logs.sh <日志文件>...    阈值 MEDREC_LOG_MAX_BYTES（默认 512 MiB）
set -euo pipefail
MAX=${MEDREC_LOG_MAX_BYTES:-536870912}
for f in "$@"; do
  [ -f "$f" ] || continue
  sz=$(stat -c %s "$f" 2>/dev/null || echo 0)
  if [ "$sz" -le "$MAX" ]; then printf "[SKIP] %s %s B 未超阈值\\n" "$f" "$sz"; continue; fi
  ts=$(date +%Y%m%d-%H%M%S)
  cp -a "$f" "$f.$ts"
  truncate -s 0 "$f"
  gzip -f "$f.$ts" || true
  printf "[OK] rotated %s (%s B) -> %s.gz（copytruncate：旧 fd 仍有效）\\n" "$f" "$sz" "$f.$ts"
done
"""


def cmd_write_rotate(argv):
    path = argv[0]
    if os.path.exists(path):
        print("EXISTS " + path)
        return 0
    with open(path, "w", encoding="utf-8") as f:
        f.write(ROTATE_SRC)
    os.chmod(path, 0o700)
    print("WROTE " + path)
    return 0


def cmd_envlists(argv):
    """envlists <freeze 目录> <输出目录>
    把 pip freeze 的清单洗成 pip 能直接吃的安装清单 install-<env>.txt。
    ★ 为什么要洗：freeze-vl-env.txt 里有两条 `@ git+https://...`（aslprep / neuromaps），
      那是上一个项目（神经影像）留下的，medrec 的 src/ scripts/ webui/ bin/ 里 grep 不到；
      目标机上装它们 = 多两次 GitHub 克隆，失败就整条 pip 非零退出（把环境装成半成品）。
      另外 pip/setuptools/wheel 由 conda 提供，freeze 里的旧 pin 会让 pip 装到一半降级自己。
    ★ 不洗掉的：`name @ https://...whl`（直接 wheel URL）—— paddlepaddle-gpu 的 cu126/cu129 wheel
      只有这个 URL 能装对，删了就是静默装错 CUDA 变体。
    """
    src_dir, out_dir = argv[0], argv[1]
    os.makedirs(out_dir, exist_ok=True)
    dropped_total = 0
    for name in sorted(os.listdir(src_dir)):
        if not name.startswith("freeze-") or not name.endswith(".txt"):
            continue
        env = name[len("freeze-"):-len(".txt")]
        out = os.path.join(out_dir, "install-%s.txt" % env)
        kept, dropped = [], []
        with open(os.path.join(src_dir, name), encoding="utf-8") as f:
            for raw in f:
                line = raw.strip()
                if not line or line.startswith("#"):
                    continue
                low = line.split("==")[0].split("@")[0].strip().lower()
                if "@ git+" in line or line.startswith("-e ") or "egg=" in line:
                    dropped.append("VCS/editable: " + line)
                    continue
                if low in ("pip", "setuptools", "wheel"):
                    dropped.append("conda 自带: " + line)
                    continue
                kept.append(line)
        with open(out, "w", encoding="utf-8") as f:
            f.write("# 由 pkg_tool.py envlists 从 freeze-%s.txt 生成，给 install.sh 建环境用\n" % env)
            f.write("# 保留 %d 条；洗掉 %d 条（明细见 env/README.md）\n" % (len(kept), len(dropped)))
            f.write("\n".join(kept) + "\n")
        os.chmod(out, 0o600)
        dropped_total += len(dropped)
        print("WROTE %s kept=%d dropped=%d" % (out, len(kept), len(dropped)))
        for d in dropped:
            print("  DROP %s: %s" % (env, d))
    print("TOTAL_DROPPED %d" % dropped_total)
    return 0


def main():
    if len(sys.argv) < 2:
        print("usage: pkg_tool.py sha|check|field|groups|members|mkdirs|checkdests|marker|dest|verify|backup|csvshape|patchjson ...")
        return 2
    cmd, argv = sys.argv[1], sys.argv[2:]
    return {
        "sha": cmd_sha, "check": cmd_check, "field": cmd_field, "groups": cmd_groups,
        "members": cmd_members, "mkdirs": cmd_mkdirs, "marker": cmd_marker, "dest": cmd_dest,
        "verify": cmd_verify, "checkdests": cmd_checkdests,
        "backup": cmd_backup, "csvshape": cmd_csvshape, "patchjson": cmd_patchjson,
        "group-count": cmd_group_count, "group-field": cmd_group_field,
        "write-rotate": cmd_write_rotate,
        "envlists": cmd_envlists,
    }[cmd](argv)


if __name__ == "__main__":
    sys.exit(main())
