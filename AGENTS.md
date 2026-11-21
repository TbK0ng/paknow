# AGENTS.md

Working notes for agents maintaining PA Agent and PAknow retrieval.

## Start here

- [README.md](README.md): local startup and project overview.
- [docs/pa-agent.md](docs/pa-agent.md): replay UI, API, analysis and reviews.
- [docs/pa-rag.md](docs/pa-rag.md): text corpus, graph expansion and evidence provenance.
- [docs/agents/retrieval-dev.md](docs/agents/retrieval-dev.md): current corpus build, PA retrieval checks and failure modes.
- [PA Agent spec](.scratch/pa-agent/spec.md): current feature scope; use the docs above for commands and endpoints.

## Research tasks: delegate, don't self-explore

**Any research task MUST be delegated to a subagent.** Research means multi-step investigation: "where does X live", "how does Y work end to end", "what breaks if I change Z", call-chain/dependency mapping, or impact analysis before an edit. A single `read` of a known path or a config value check is not research — do it inline.

- Spawn a **read-only scout** with the question, scope, and evidence needed. Fan out independent questions in one batch. Scouts must not edit files.
- Start with the **codebase-memory** graph for `E-PAknow`: confirm the generation, locate symbols with `search_graph`, trace dependencies with `trace_path`, and read symbol source with `get_code_snippet`. Use `detect_changes` for diff impact and `check_index_coverage` for evidence paths and relevant scopes. Read source for missed or stale ranges, literal strings and config values. A clean coverage report does not prove that code is unused.
- Share the parent’s graph findings, coverage gaps, and unresolved questions. Scouts with graph access should query it first; otherwise they should use the supplied evidence and read the relevant source without claiming graph access.
- Synthesize the findings without redoing the scout’s work. If the harness cannot spawn a scout, say so and continue graph-first. Direct reads of already-known files are fine for implementation.

## Local data and cleanup

- Embedded Qdrant is single-process. Do not run two servers or run retrieval, ingest, or eval against the same object store while the server owns it. Use separate database **and** object-store paths for rebuilds; see [retrieval-dev.md](docs/agents/retrieval-dev.md).
- Ignored does not mean disposable. Preserve `.env`, the local environment, source PDF/EPUBs, databases, object stores, model weights, market/review data, and rebuild or rollback evidence unless the task explicitly replaces them. Delete only named, verified disposable outputs; do not run a blanket `git clean`.

## Concept identity

Canonical concept ids live only in `configs/terms.yaml`. Resolve cross-module links through `paknow/graph/identity.py` (`canonical_for`); a `TermMatch.canonical` is a local match, not an identity. See `GLOSSARY.md` 概念身份.

## Feature workflow and verification

- Record the integration base as `**Review base:** <sha>` in the feature spec before review, and use that commit as the diff base.
- Before closing, run `python scripts/check_scratch.py` and `python -m pytest -q` from the project environment. Remove specs, handoffs, and run instructions for retired features; keep current commands in the docs.

## Tracking and domain references

### Issue tracker

Issues and specs live as Markdown files under `.scratch/<feature>/` in this repo (no remote tracker). See [the local tracker guide](docs/agents/issue-tracker.md).

### Triage labels

Five canonical roles mapped to `Status:` values: `needs-triage`, `needs-info`, `ready-for-agent`, `ready-for-human`, `wontfix`. See `docs/agents/triage-labels.md`.

### Domain docs

Single-context: `GLOSSARY.md`, `docs/pa-agent.md`, and `docs/pa-rag.md`. See `docs/agents/domain.md`.
