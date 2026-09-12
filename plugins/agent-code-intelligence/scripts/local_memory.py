#!/usr/bin/env python3
"""Tiny SQLite fallback memory for verified engineering lessons.

This is intentionally simple and local. It is not a replacement for Mem0/Graphiti/Cognee.
"""
import argparse, json, os, sqlite3, sys
from pathlib import Path
from datetime import datetime, timezone

base = os.getenv("PLUGIN_DATA") or os.getenv("CLAUDE_PLUGIN_DATA") or str(Path.home()/".agent-code-intelligence")
Path(base).mkdir(parents=True, exist_ok=True)
db = Path(base)/"lessons.sqlite3"
con = sqlite3.connect(db)
con.execute("CREATE TABLE IF NOT EXISTS lessons (id INTEGER PRIMARY KEY, ts TEXT, scope TEXT, title TEXT, symptom TEXT, cause TEXT, fix TEXT, verification TEXT, tags TEXT)")
con.commit()

p = argparse.ArgumentParser()
sp = p.add_subparsers(dest="cmd", required=True)
a = sp.add_parser("add")
a.add_argument("--scope", default="global")
a.add_argument("--title", required=True)
a.add_argument("--symptom", required=True)
a.add_argument("--cause", required=True)
a.add_argument("--fix", required=True)
a.add_argument("--verification", required=True)
a.add_argument("--tags", default="")
s = sp.add_parser("search")
s.add_argument("query")
s.add_argument("--limit", type=int, default=10)
l = sp.add_parser("list")
l.add_argument("--limit", type=int, default=20)
args = p.parse_args()

if args.cmd == "add":
    con.execute("INSERT INTO lessons(ts,scope,title,symptom,cause,fix,verification,tags) VALUES (?,?,?,?,?,?,?,?)",
      (datetime.now(timezone.utc).isoformat(), args.scope, args.title, args.symptom, args.cause, args.fix, args.verification, args.tags))
    con.commit(); print("stored verified lesson")
elif args.cmd == "search":
    q = f"%{args.query}%"
    cur = con.execute("SELECT id,ts,scope,title,symptom,cause,fix,verification,tags FROM lessons WHERE title LIKE ? OR symptom LIKE ? OR cause LIKE ? OR fix LIKE ? OR tags LIKE ? ORDER BY id DESC LIMIT ?", (q,q,q,q,q,args.limit))
    for row in cur:
        print(json.dumps(dict(zip(["id","ts","scope","title","symptom","cause","fix","verification","tags"], row)), ensure_ascii=False))
else:
    cur = con.execute("SELECT id,ts,scope,title,tags FROM lessons ORDER BY id DESC LIMIT ?", (args.limit,))
    for row in cur:
        print(json.dumps(dict(zip(["id","ts","scope","title","tags"], row)), ensure_ascii=False))
