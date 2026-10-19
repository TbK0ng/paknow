"""End-to-end query through the full LangGraph workflow."""
from paknow.config import settings
from paknow.db import init_db
from paknow.workflows.graph import run_query

settings.ensure_dirs()
init_db()

q = "双底多头旗形的两个低点必须一样吗？结合原文说明。"
print("=" * 70, flush=True)
print(f"QUERY: {q}", flush=True)
result = run_query(q)
print(f"intents : {result['intents']}", flush=True)
print(f"routes  : {result['routes']}", flush=True)
print(f"hits    : {len(result['hits'])}", flush=True)
ans = result.get("answer") or {}
print("-" * 70, flush=True)
print("ANSWER:\n" + (ans.get("answer") or "<none>")[:1200], flush=True)
print("-" * 70, flush=True)
print("uncertainty:", ans.get("uncertainty"), flush=True)
print("evidence_ids:", ans.get("evidence_ids"), flush=True)
