#!/usr/bin/env python3
# mem.py - 长期记忆库（SQLite; 有 FTS5/trigram 就建索引, 否则 LIKE 兜底）
# 用法:
#   mem.py add --kind fact|decision|lesson|todo --text "..." [--tags a,b] [--source ...] [--confirmed]
#   mem.py recall "关键词" [--limit 5] [--drafts]
#   mem.py list [--limit 20] [--kind fact]
#   mem.py confirm <id>
#   mem.py stats
# DB 路径: 环境变量 MEDREC_MEM_DB, 默认 /home/ubuntu/medrec/memory/memory.db
import argparse, json, os, sqlite3, sys, time

DB = os.environ.get("MEDREC_MEM_DB", "/home/ubuntu/codex-scratch/memory/memory.db")
KINDS = ("fact", "decision", "lesson", "todo")
MAXCHARS = 200

def con():
    d = os.path.dirname(DB)
    if d:
        os.makedirs(d, exist_ok=True)
    c = sqlite3.connect(DB)
    c.row_factory = sqlite3.Row
    c.execute("""CREATE TABLE IF NOT EXISTS memories(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ts TEXT, kind TEXT, text TEXT, tags TEXT, source TEXT, confirmed INTEGER DEFAULT 0)""")
    return c

def has_fts(c):
    try:
        c.execute("""CREATE VIRTUAL TABLE IF NOT EXISTS mem_fts USING fts5(
            text, tags, content='memories', content_rowid='id', tokenize='trigram')""")
        return True
    except sqlite3.OperationalError:
        return False

def add(text, kind="fact", tags="", source="", confirmed=False):
    c = con(); fts = has_fts(c)
    cur = c.execute("INSERT INTO memories(ts,kind,text,tags,source,confirmed) VALUES(?,?,?,?,?,?)",
                    (time.strftime("%Y-%m-%d %H:%M:%S"), kind, text.strip(),
                     tags.strip(), source.strip(), 1 if confirmed else 0))
    mid = cur.lastrowid
    if fts:
        c.execute("INSERT INTO mem_fts(rowid,text,tags) VALUES(?,?,?)", (mid, text, tags))
    c.commit(); c.close()
    return mid

def search(q, limit=5, drafts=False):
    c = con(); fts = has_fts(c)
    rows, seen = [], set()
    if fts and len(q) >= 3:
        try:
            for r in c.execute("""SELECT m.* FROM memories m JOIN mem_fts f ON f.rowid=m.id
                                  WHERE mem_fts MATCH ? ORDER BY m.id DESC LIMIT ?""", (q, limit * 2)):
                rows.append(r); seen.add(r["id"])
        except sqlite3.OperationalError:
            pass
    like = "%" + q + "%"
    for r in c.execute("""SELECT * FROM memories WHERE text LIKE ? OR tags LIKE ? OR kind LIKE ?
                          ORDER BY id DESC LIMIT ?""", (like, like, like, limit * 2)):
        if r["id"] not in seen:
            rows.append(r); seen.add(r["id"])
    c.close()
    if not drafts:
        rows = [r for r in rows if r["confirmed"]]
    return rows[:limit]

def listing(limit=20, kind=None):
    c = con()
    if kind:
        rs = list(c.execute("SELECT * FROM memories WHERE kind=? ORDER BY id DESC LIMIT ?", (kind, limit)))
    else:
        rs = list(c.execute("SELECT * FROM memories ORDER BY id DESC LIMIT ?", (limit,)))
    c.close()
    return rs

def confirm(mid):
    c = con(); c.execute("UPDATE memories SET confirmed=1 WHERE id=?", (mid,)); c.commit(); c.close()

def stats():
    c = con()
    tot = c.execute("SELECT COUNT(*) FROM memories").fetchone()[0]
    conf = c.execute("SELECT COUNT(*) FROM memories WHERE confirmed=1").fetchone()[0]
    by = list(c.execute("SELECT kind, COUNT(*) n FROM memories GROUP BY kind ORDER BY n DESC"))
    last = c.execute("SELECT ts, kind, substr(text,1,60) FROM memories ORDER BY id DESC LIMIT 3").fetchall()
    fts = has_fts(c); c.close()
    return {"total": tot, "confirmed": conf, "drafts": tot - conf, "fts": fts,
            "by_kind": [[r[0], r[1]] for r in by], "last": [[r[0], r[1], r[2]] for r in last]}

def fmt(r):
    t = r["text"] if len(r["text"]) <= MAXCHARS else r["text"][:MAXCHARS] + "…"
    flag = "confirmed" if r["confirmed"] else "draft"
    s = "#%d [%s/%s] %s | %s" % (r["id"], r["kind"], flag, r["ts"], t)
    if r["tags"]:
        s += " | tags=" + r["tags"]
    if r["source"]:
        s += " | src=" + r["source"]
    return s

def main():
    ap = argparse.ArgumentParser(description="长期记忆库")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("add"); p.add_argument("--kind", default="fact", choices=KINDS)
    p.add_argument("--text", required=True); p.add_argument("--tags", default="")
    p.add_argument("--source", default=""); p.add_argument("--confirmed", action="store_true")
    p = sub.add_parser("recall"); p.add_argument("query"); p.add_argument("--limit", type=int, default=5)
    p.add_argument("--drafts", action="store_true"); p.add_argument("--json", action="store_true")
    p = sub.add_parser("list"); p.add_argument("--limit", type=int, default=20); p.add_argument("--kind")
    p = sub.add_parser("confirm"); p.add_argument("id", type=int)
    sub.add_parser("stats")
    a = ap.parse_args()
    if a.cmd == "add":
        mid = add(a.text, a.kind, a.tags, a.source, a.confirmed)
        print("已写入 #%d (%s%s)" % (mid, a.kind, "" if a.confirmed else ", draft 待确认"))
    elif a.cmd == "recall":
        rs = search(a.query, a.limit, a.drafts)
        if a.json:
            print(json.dumps([dict(r) for r in rs], ensure_ascii=False, indent=1))
        elif not rs:
            print("(没有命中)")
        else:
            for r in rs:
                print(fmt(r))
    elif a.cmd == "list":
        for r in listing(a.limit, a.kind):
            print(fmt(r))
    elif a.cmd == "confirm":
        confirm(a.id); print("已确认 #%d" % a.id)
    else:
        print(json.dumps(stats(), ensure_ascii=False, indent=1))

if __name__ == "__main__":
    main()
