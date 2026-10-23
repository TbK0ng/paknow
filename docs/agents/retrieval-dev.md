# Retrieval / eval dev environment

Copy-paste reference for anything that touches the text index, the evidence graph,
or the gold evaluation. Commands assume the main repo at `E:/PAknow`; run them
from the directory named in each block. Python is the shared venv
`E:/PAknow/.venv/Scripts/python.exe`.

## Paths and overrides

| Thing | Default | Override |
|---|---|---|
| Metadata DB | `sqlite:///./data/paknow.db` (relative to CWD; `E:/PAknow/data/paknow.db` from the main repo root) | `PAKNOW_DB_URL` — always use the absolute form `sqlite:///E:/PAknow/data/paknow.db` |
| Derived objects / Qdrant | `<project_root>/data/objects` (`E:/PAknow/data/objects` in the main repo) | `OBJECTS_DIR=E:/PAknow/data/objects` |
| Embedded Qdrant storage | `<OBJECTS_DIR>/qdrant` | `QDRANT_URL` switches to a server (empty = embedded) |
| BGE-M3 / reranker / VL cache | `E:\use_model` (`HF_HOME` is set by `paknow/config.py`) | `HF_HOME`, or the `hf_home` setting |
| Gold dataset | `evals/gold/graph_expansion.json` | `--dataset` |
| Frozen union-probe baseline | `.scratch/graph-expansion/gold-baseline.json` | `--baseline` |
| Union-probe / text / graph output | `.scratch/graph-expansion/gold-baseline.json`, `.scratch/retrieval-eval-parity/gold-{text,regression}.json` | `--out` |

`project_root` is derived from the installed package location, so a worktree
resolves `data/` inside the worktree. Worktrees are created without the ignored
`data/` tree (`.gitignore`: `data/objects/`, `data/*.db*`): the DB, object store
and Qdrant index are absent. Point the env vars at the main repo's absolute
paths, or create an untracked junction (`mklink /J data\objects E:\PAknow\data\objects`).
Never commit `data/`.

## Environment setup

Git Bash / POSIX form (each block below assumes it):

```bash
export PAKNOW_DB_URL=sqlite:///E:/PAknow/data/paknow.db
export OBJECTS_DIR=E:/PAknow/data/objects
export PYTHONPATH=.
```

PowerShell equivalent:

```powershell
$env:PAKNOW_DB_URL="sqlite:///E:/PAknow/data/paknow.db"
$env:OBJECTS_DIR="E:/PAknow/data/objects"
$env:PYTHONPATH="."
```

Run every command from the checkout root (main repo or worktree); `PYTHONPATH=.`
makes `paknow` and `scripts/` imports resolve. `--help` works with no env at all.

## Commands

### Tests (no DB, no Qdrant, no model)

```bash
PYTHONPATH=. E:/PAknow/.venv/Scripts/python.exe -m pytest tests -q
```

### Gold evaluation (needs DB + objects + BGE-M3)

Three recall modes, all reading the same frozen queries:

| Mode | Path | Artifact | Meaning |
|---|---|---|---|
| `baseline` | `hybrid_search(top_k=--top-k)` per query, union | `.scratch/graph-expansion/gold-baseline.json` | Union probe: upper-bound reference. Byte-identical across reruns; only re-record when the dataset or metric is intentionally re-frozen. |
| `text` | `retrieve_evidence(..., expand_graph=False)` | `.scratch/retrieval-eval-parity/gold-text.json` | Production-parity baseline: same seam, rerank, diversity and width as the online text channel, only the graph layer off. |
| `graph` | `retrieve_evidence(...)` plus a `text` round per item | `.scratch/retrieval-eval-parity/gold-regression.json` | Shipped chain. Reports the union-probe delta and the production-parity delta (graph-only hits, net gain = graph-arm expected hits − text-arm expected hits). |

Recall metrics in graph mode: `graph_only` (expected units in graph evidence
but not in the text arm) is the primary metric. The graph arm's expected hits
are `union_found` = text hits ∪ graph evidence, so `net_gain = union_found −
regression_text_found`; `graph_found` (graph evidence only) stays a separate
field. Truncation counts are still reported but are not a verdict. Graph
evidence budget is `MAX_EVIDENCE=14`, bounded by `MAX_CONTEXT_TOKENS=1200`;
edge traversal stays at `MAX_EDGES=32`.

```bash
# production-parity baseline, then graph regression against the committed union probe
PAKNOW_DB_URL=sqlite:///E:/PAknow/data/paknow.db \
OBJECTS_DIR=E:/PAknow/data/objects \
PYTHONPATH=. E:/PAknow/.venv/Scripts/python.exe scripts/eval_gold.py --mode text

PAKNOW_DB_URL=sqlite:///E:/PAknow/data/paknow.db \
OBJECTS_DIR=E:/PAknow/data/objects \
PYTHONPATH=. E:/PAknow/.venv/Scripts/python.exe scripts/eval_gold.py --mode graph --reuse-baseline

# re-record the union probe only when intentionally re-freezing
PAKNOW_DB_URL=sqlite:///E:/PAknow/data/paknow.db \
OBJECTS_DIR=E:/PAknow/data/objects \
PYTHONPATH=. E:/PAknow/.venv/Scripts/python.exe scripts/eval_gold.py --mode baseline
```

- `--mode text` and `--mode graph` each write their own artifact and never
  touch the union-probe JSON.
- `--mode graph --reuse-baseline` verifies `evals/gold/graph_expansion.sha256`
  before trusting the union probe; `--mode graph` without it reruns the probe
  in memory.
- `--limit N` and `--out PATH` are available for partial runs; the same
  commands run from the main repo or a worktree with the env vars above.
- None of the modes calls the routing model; all load the embedding model and
  the Qdrant text collection. Run them one at a time (see below).

### Answer-level evaluation (needs DB + objects + BGE-M3 + cloud key)

`scripts/eval_answers.py` runs every v2 gold question through the full
production chain twice — same question, same parameters, one flag apart:

| Arm | `run_query` flag | Meaning |
|---|---|---|
| `text` | `expand_graph=False` | Production-parity text channel; graph layer off at the retrieval seam. |
| `graph` | `expand_graph=True` | Shipped chain, graph evidence rendered as a non-citable understanding block. |

Each (item, arm) is one stateless `run_query` call. Generation only sees the
question and its own retrieval result; the runner never passes the expected
evidence or the gold answer points into the chain. Routes, rounds, retrieval
failures, the answer, cited ids, the citable set and the graph evidence ids are
recorded per item. The deterministic checks are pure: every cited id must be in
that answer's citable set, no graph-only evidence id may be cited (a graph entry
that duplicates a citable text unit is still that text unit, not synthesis),
and expected-evidence coverage (`covered / expected`, with the missing ids) is
computed per item. Coverage is reported as a distribution, not folded into the
hard verdict.

Artifacts go to `.scratch/retrieval-eval-parity/answers/<UTC-timestamp>/`:
`records.jsonl` (one small record per item/arm, no retrieved text),
`summary.json` (per-arm summaries, paired graph-vs-text delta, wall time) and
`checkpoint.jsonl`. The checkpoint is appended per (item, arm), so an
interrupted run resumes without repeating completed arms; error records are
retried. A failed item is recorded and the batch continues. Ticket 05's judge
consumes `records.jsonl`.

```bash
# full batch: 60 items x 2 arms, sequential
PAKNOW_DB_URL=sqlite:///E:/PAknow/data/paknow.db \
OBJECTS_DIR=E:/PAknow/data/objects \
PYTHONPATH=. E:/PAknow/.venv/Scripts/python.exe scripts/eval_answers.py

# cheap smoke or limited slice
PAKNOW_DB_URL=sqlite:///E:/PAknow/data/paknow.db \
OBJECTS_DIR=E:/PAknow/data/objects \
PYTHONPATH=. E:/PAknow/.venv/Scripts/python.exe scripts/eval_answers.py --limit 2

# resume an interrupted batch (same --out directory)
PAKNOW_DB_URL=sqlite:///E:/PAknow/data/paknow.db \
OBJECTS_DIR=E:/PAknow/data/objects \
PYTHONPATH=. E:/PAknow/.venv/Scripts/python.exe scripts/eval_answers.py \
  --resume --out .scratch/retrieval-eval-parity/answers/<timestamp>
```

Budget: one chain call per (item, arm); the chain itself makes roughly 3–4
`deepseek-flash` calls (route, up to two sufficiency checks, generate), so a
full 60×2 batch stays within the spec's 600–900 calls. Run it in the same
serialized window as the recall eval: embedded Qdrant is single-process.

### Answer judgement (needs DB + cloud key, no Qdrant)

`scripts/eval_judge.py` judges the committed answer batch
(`.scratch/retrieval-eval-parity/answers/<timestamp>/records.jsonl`) against
`evals/gold/graph_expansion_v2.json`. One verdict per (item, arm):

1. **Hard constraints first, deterministically.** A record whose answer run
   errored, whose answer is empty, or whose stored citations break the citation
   constraints (out-of-set id, graph-only id) is a `fail` verdict and is never
   sent to the model. The constraints are recomputed from the stored ids on
   every run, so a tightened definition applies to an old batch without
   re-running the answers.
2. **One blind `gateway.chat_json` call otherwise.** The message carries only
   the question, the expected answer points, the answer text and the text of the
   evidence the answer cited — fetched read-only from the metadata DB
   (`evidence_units`, one `SELECT`, no writes; each unit clipped to 1200 chars).
   The arm, item id, stratum, routes, rounds and retrieval failures never enter
   the message: a `text` and a `graph` record with the same answer produce
   byte-identical messages.
3. **Strict schema.** `JudgeScores`: `correctness`, `condition_preservation` and
   `citation_support`, each an integer 0–2, plus `unsupported_claims:
   list[str]`. A missing field, out-of-range value, coerced type or extra key
   fails that record. The three scores are never merged into one number and the
   hard verdict stays the verdict. The call raises `max_tokens` to 16384: the
   reasoning budget shares that ceiling, and at 8192 the longest answers came
   back with empty content at `finish=length`.

The 40 new v2 items carry authored `expected_points` bound to evidence ids; the
20 original items ship unchanged and have none, so for those the
expected-evidence text is the correctness anchor and the record says
`points_source: "evidence_excerpt"`.

Artifacts go to `.scratch/retrieval-eval-parity/judgements/<UTC-timestamp>/`:
`records.jsonl` (per-record verdict: hard reasons, the three scores, unsupported
claims, coverage, evidence size), `summary.json` (counts, per-arm and
per-stratum hard-pass rate / score distributions / unsupported-claim counts,
paired graph-minus-text deltas, hard failures, the exact prompt and schema, wall
time) and `checkpoint.jsonl`. `--limit N` judges the first N pending records,
`--sample N` judges N spread evenly over the batch (use it for the pre-full-run
manual spot check), `--resume --out <dir>` retries errored records only.

```bash
# 10-record spot check, then the full batch
PAKNOW_DB_URL=sqlite:///E:/PAknow/data/paknow.db PYTHONPATH=. \
  E:/PAknow/.venv/Scripts/python.exe scripts/eval_judge.py --sample 10 \
  --out .scratch/retrieval-eval-parity/judgements/spot-$(date -u +%Y%m%dT%H%M%SZ)

PAKNOW_DB_URL=sqlite:///E:/PAknow/data/paknow.db PYTHONPATH=. \
  E:/PAknow/.venv/Scripts/python.exe scripts/eval_judge.py \
  --out .scratch/retrieval-eval-parity/judgements/$(date -u +%Y%m%dT%H%M%SZ)

# retry errored records only
PAKNOW_DB_URL=sqlite:///E:/PAknow/data/paknow.db PYTHONPATH=. \
  E:/PAknow/.venv/Scripts/python.exe scripts/eval_judge.py \
  --resume --out .scratch/retrieval-eval-parity/judgements/<timestamp>
```

`--answers PATH` points the judge at a different batch. It needs the metadata DB
and the cloud key but no Qdrant and no embedding model: it reads stored answers
and evidence text only, so it does not take the embedded-Qdrant lock.

### Taxonomy rebuild (needs DB)

```bash
# dry run: prints dedupe/parse/coverage numbers, edge counts and fingerprint
PAKNOW_DB_URL=sqlite:///E:/PAknow/data/paknow.db PYTHONPATH=. \
  E:/PAknow/.venv/Scripts/python.exe scripts/build_taxonomy.py

# apply: replaces every origin='heading_parse' edge; seeded/claim/figure edges untouched
PAKNOW_DB_URL=sqlite:///E:/PAknow/data/paknow.db PYTHONPATH=. \
  E:/PAknow/.venv/Scripts/python.exe scripts/build_taxonomy.py --apply
```

`--doc-id <id>` scopes to one document; `--header-threshold N` drops titles that
repeat across headings (default 5). Dry run writes nothing.

### Claim alignment (needs DB)

```bash
# dry run: deterministic alias matching only; rewrites the two report files below
PAKNOW_DB_URL=sqlite:///E:/PAknow/data/paknow.db PYTHONPATH=. \
  E:/PAknow/.venv/Scripts/python.exe scripts/align_claims.py

# apply deterministic alignments; --model adds the residual model pass (needs DEEPSEEK_API_KEY)
PAKNOW_DB_URL=sqlite:///E:/PAknow/data/paknow.db PYTHONPATH=. \
  E:/PAknow/.venv/Scripts/python.exe scripts/align_claims.py --apply
```

Dry run does not write the DB but does regenerate
`.scratch/graph-expansion/claim-alignment.md` and `claim-residuals.json` in the
current checkout — restore those if you did not mean to change them.

### Smoke runs (needs the full corpus, Qdrant and often the cloud key)

`scripts/smoke_{full,query,search,ingest,pdf,chart}.py`. Keep them in the same
serialized window as the eval (see below).

## Embedded Qdrant is single-process

The default local-mode client locks its storage folder. A second client on the
same `data/objects/qdrant` fails with:

```
Storage folder ... is already accessed by another instance of Qdrant client.
```

Serialize every retrieval/eval/smoke run: stop `uvicorn` (and any notebook or
script holding the client) before starting another. Never run two evals, an eval
plus a smoke run, or two agents' retrieval work against the same objects dir
concurrently. Two checks may read the DB concurrently; only the vector store is
locked. For parallel work, either give one process `QDRANT_URL=http://localhost:6333`
server or copy the objects dir per worktree (large).

## Failure lookup

| Symptom | Cause | Fix |
|---|---|---|
| `sqlite3.OperationalError: unable to open database file`, or "no such table" | `PAKNOW_DB_URL` relative and resolved against the worktree CWD / absent `data/` | Set the absolute main-repo URL |
| `Storage folder ... is already accessed by another instance of Qdrant client.` | Embedded client already open (API server, eval, smoke) | Stop the other process; run serially |
| Empty search results / "collection not found" | `OBJECTS_DIR` points at an empty or stale tree | Set `OBJECTS_DIR=E:/PAknow/data/objects` |
| Model download attempts, `OSError` resolving `BAAI/bge-m3`, or slow first call | `HF_HOME` not pointing at the warmed cache | Confirm `settings.hf_home` is `E:\use_model`; set `HF_HOME` before importing `paknow` |
| `ModuleNotFoundError: paknow` / `scripts` | `PYTHONPATH=.` missing or wrong CWD | Run from the checkout root with `PYTHONPATH=.` |
| Baseline numbers changed unexpectedly | `--mode baseline` was used instead of `--mode graph` | Restore `gold-baseline.json` from git; use graph mode |
| Evals are unusably slow | First call downloads/loads BGE-M3 on CPU | Check the CUDA env (torch CUDA build per README); reuse one process for repeated calls |

## Not runnable without infrastructure

`eval_gold.py` (both modes), `eval_answers.py`, the taxonomy dry run/apply, the
claim-alignment dry run/apply and all `smoke_*` scripts need the local corpus
artifacts above (the answer runner additionally needs the cloud key). Only
`--help` and the test suite run green without them.
