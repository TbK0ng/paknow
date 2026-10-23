# AGENTS.md

Operating notes for AI agents working in this repo.

## Research tasks: delegate, don't self-explore

**Any research task MUST be delegated to a subagent.** Research means multi-step investigation: "where does X live", "how does Y work end to end", "what breaks if I change Z", call-chain/dependency mapping, or impact analysis before an edit. A single `read` of a known path or a config value check is not research — do it inline.

- Spawn a **read-only scout** subagent. Give it the question, the suspected path scope, and the shape of the answer you need back. It must not edit files.
- The subagent MUST query the **codebase-memory MCP** graph before touching the filesystem. This repo is indexed as project `E-PAknow` (auto-refresh on use):
  - `search_graph` — find symbols by name/semantics (returns `qn`, file, lines)
  - `trace_path` — callers/callees, data flow
  - `get_code_snippet` — source for a symbol
  - `get_architecture` — packages, entry points, layers, cycles
  - `query_graph` — multi-hop Cypher
  - `detect_changes` — map a git diff to affected files/symbols
  - Fall back to `grep`/`read` only for literals or paths the index flags as uncovered (`check_index_coverage`).
- Fan out independent questions as separate subagents in one batch, never serially.
- The main agent synthesises the returned findings. It does not re-read the same files to "confirm" — that wastes the delegation.
- Only if no subagent can be spawned (harness reports no model / spawn error): say so in one line, then run the same MCP-first procedure inline. Never silently fall back to blind file crawling.

**Implementation work is graph-first too.** For call-chain, dependency, or impact questions while editing a spec, use `search_graph` / `trace_path` / `get_code_snippet` instead of hunting through files; `detect_changes` maps a diff to affected symbols. Reading an already-known path directly is fine and preferred over extra search hops — read that file, don't crawl the tree.

## Retrieval and eval

Commands, path overrides, and failure modes for retrieval/eval/taxonomy/alignment work: `docs/agents/retrieval-dev.md`. The embedded Qdrant is single-process — serialize runs against `data/objects`.

## Concept identity

Canonical concept ids live only in `configs/terms.yaml`. Resolve cross-module links through `paknow/graph/identity.py` (`canonical_for` / `resolved_canonical`); a `TermMatch.canonical` is a local match, not an identity. See `GLOSSARY.md` 概念身份.

## Agent skills

### Issue tracker

Issues and specs live as markdown files under `.scratch/<feature>/` in this repo (no remote tracker). See `docs/agents/issue-tracker.md`.

### Triage labels

Five canonical roles mapped to `Status:` values: `needs-triage`, `needs-info`, `ready-for-agent`, `ready-for-human`, `wontfix`. See `docs/agents/triage-labels.md`.

### Domain docs

Single-context: `GLOSSARY.md` and `docs/adr/` at the repo root. See `docs/agents/domain.md`.
