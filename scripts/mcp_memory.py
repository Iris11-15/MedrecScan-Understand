#!/usr/bin/env python3
# mcp_memory.py - stdio MCP server: 给 Codex 的长期记忆工具(add/search/stats)
# 注册: codex mcp add memory -- /home/ubuntu/anaconda3/envs/vl-env/bin/python /home/ubuntu/medrec/scripts/mcp_memory.py
import json, os, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import mem

PROTO = "2024-11-05"
TOOLS = [
    {"name": "memory_add",
     "description": "写入一条长期记忆。决策类默认写成 draft, 由人确认后才可被检索到。",
     "inputSchema": {"type": "object", "properties": {
         "text": {"type": "string", "description": "要记住的内容, 一句话, <=200字"},
         "kind": {"type": "string", "enum": ["fact", "decision", "lesson", "todo"], "default": "fact"},
         "tags": {"type": "string", "description": "逗号分隔标签"},
         "source": {"type": "string", "description": "来源: 哪个任务/命令"},
         "confirmed": {"type": "boolean", "description": "关键事实可置 true; 默认 false"}},
         "required": ["text"]}},
    {"name": "memory_search",
     "description": "按关键词检索长期记忆。默认只返回已确认条目; drafts=true 才看草稿。",
     "inputSchema": {"type": "object", "properties": {
         "query": {"type": "string"},
         "limit": {"type": "integer", "default": 5},
         "drafts": {"type": "boolean", "default": False}},
         "required": ["query"]}},
    {"name": "memory_stats",
     "description": "记忆库统计: 条数/草稿数/最近写入",
     "inputSchema": {"type": "object", "properties": {}}},
]

def reply(rid, result=None, error=None):
    m = {"jsonrpc": "2.0", "id": rid}
    if error:
        m["error"] = error
    else:
        m["result"] = result
    sys.stdout.write(json.dumps(m, ensure_ascii=False) + "\n")
    sys.stdout.flush()

def text_result(s):
    return {"content": [{"type": "text", "text": s}]}

def handle(msg):
    meth = msg.get("method")
    rid = msg.get("id")
    if meth == "initialize":
        reply(rid, {"protocolVersion": PROTO,
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": "medrec-memory", "version": "1.0"}})
    elif meth == "tools/list":
        reply(rid, {"tools": TOOLS})
    elif meth == "tools/call":
        p = msg.get("params") or {}
        name = p.get("name")
        a = p.get("arguments") or {}
        try:
            if name == "memory_add":
                mid = mem.add(a.get("text", ""), a.get("kind", "fact"), a.get("tags", ""),
                              a.get("source", ""), bool(a.get("confirmed", False)))
                reply(rid, text_result("已写入 #%d%s" % (mid, "" if a.get("confirmed") else " (draft, 待人工确认)")))
            elif name == "memory_search":
                rs = mem.search(a.get("query", ""), int(a.get("limit", 5)), bool(a.get("drafts", False)))
                reply(rid, text_result("\n".join(mem.fmt(r) for r in rs) if rs else "(没有命中)"))
            elif name == "memory_stats":
                reply(rid, text_result(json.dumps(mem.stats(), ensure_ascii=False)))
            else:
                reply(rid, error={"code": -32601, "message": "unknown tool: %s" % name})
        except Exception as e:
            reply(rid, text_result("错误: %s" % e))
    elif rid is not None:
        reply(rid, error={"code": -32601, "message": "unknown method: %s" % meth})

def main():
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except Exception:
            continue
        handle(msg)

if __name__ == "__main__":
    main()
