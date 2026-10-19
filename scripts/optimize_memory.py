"""Run the 评价→蒸馏→发布 memory-optimization loop manually."""
import json

from paknow.config import settings
from paknow.db import init_db
from paknow.memory import gitstore
from paknow.memory.optimizer import evaluate, run_loop, should_optimize

settings.ensure_dirs()
init_db()

print(f"unscored traces: {should_optimize.__wrapped__ if hasattr(should_optimize, '__wrapped__') else ''}", flush=True)
by_intent, _ = evaluate()
print("问题分组:", {k: dict(v) for k, v in by_intent.items()}, flush=True)

print("running 评价→蒸馏→发布 loop...", flush=True)
published = run_loop()
print("published:", json.dumps(published, ensure_ascii=False, indent=2), flush=True)

print("--- git log ---", flush=True)
for line in gitstore.log(n=10):
    print(line, flush=True)
