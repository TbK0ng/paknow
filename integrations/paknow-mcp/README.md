# PAknow MCP

An evidence-first MCP server for the PAknow price-action knowledge base.

It exposes PAknow's REST API to Pi, Claude Desktop, Cursor, and any other MCP client.
The adapter does **not** import PAknow internals: the FastAPI process owns SQLite,
Qdrant (embedded mode allows one client per index path), and the local models.

## Capabilities

| Tool | What it does | Side effects |
|---|---|---|
| `paknow_ask` | Full grounded answer with citations | Writes a PAknow `RunTrace`; not idempotent |
| `paknow_chart` | Chart image + optional right-edge cutoff | Writes a PAknow `RunTrace`; not idempotent |
| `paknow_search` | Retrieval only; compact evidence hits | Read-only |
| `paknow_evidence` | Full evidence unit by ID | Read-only |
| `paknow_documents` | List corpus documents | Read-only |
| `paknow_source` | One document's metadata and counts | Read-only |
| `paknow_page_image` | PDF page render as MCP image | Read-only |
| `paknow_figure_image` | Stored figure as MCP image | Read-only |
| `paknow_run` | Run trace / audit record | Read-only |
| `paknow_sops` | List SOPs and lifecycle states | Read-only |
| `paknow_health` | Service health and latency | Read-only |

The server instructions and the bundled skill enforce the important boundary:
price-action questions go through `paknow_ask`; the adapter never invents citations,
and the model must not answer from its own memory when PAknow is unavailable.

## Requirements

- Python 3.11+
- A running PAknow service (`uvicorn apps.api.main:app --port 8000`)
- The corpus ingested and indexed (see the repository README)

## Install

```bash
pip install paknow-mcp
# or, from this directory:
pip install -e .
```

## Configure

Environment variables:

| Variable | Default | Purpose |
|---|---|---|
| `PAKNOW_BASE_URL` | `http://127.0.0.1:8000` | PAknow HTTP API |
| `PAKNOW_TOKEN` | unset | Bearer token for a non-loopback service |
| `PAKNOW_TIMEOUT` | `300` | Read timeout in seconds; the first query loads local models |
| `PAKNOW_CONNECT_TIMEOUT` | `10` | Connect timeout in seconds |
| `PAKNOW_MAX_IMAGE_BYTES` | `20971520` | Max chart/figure image size |
| `PAKNOW_MAX_FIGURE_IMAGES` | `4` | Max images attached to one answer |
| `PAKNOW_MAX_TEXT_CHARS` | `45000` | Markdown result cap |

## Run

```bash
# stdio (Pi, Claude Desktop, Cursor)
paknow-mcp

# streamable HTTP
paknow-mcp --transport streamable-http --host 127.0.0.1 --port 8765 --path /mcp

# one-off overrides
paknow-mcp --base-url http://127.0.0.1:8000 --timeout 300

# show version
paknow-mcp --version
```

MCP client configuration (stdio):

```json
{
  "mcpServers": {
    "paknow": {
      "command": "paknow-mcp",
      "env": {
        "PAKNOW_BASE_URL": "http://127.0.0.1:8000",
        "PAKNOW_TIMEOUT": "300"
      }
    }
  }
}
```

For Pi specifically, prefer `exposure: "direct"` if you configure the server through
`mcp.json`. Pi's default MCP exposure is `codemode`, which hides the tools from the
model unless it writes a script.

## Skill

The bundled generic Agent Skill is at `skills/paknow/SKILL.md`. It is client-neutral
and covers intent routing, follow-up rewriting, citation fidelity, image handling,
failure behavior, and out-of-scope requests.

Copy the `skills/paknow/` directory into your agent's skills location, or install the
release bundle that contains both the wheel and the skill.

## Release

From the repository root:

```bash
# Build a deterministic bundle under integrations/paknow-mcp/dist/release/
python integrations/paknow-mcp/scripts/build_release.py

# Extract into .omp/, install the wheel, run the mock PAknow service, and verify
# every MCP tool end to end.
python integrations/paknow-mcp/scripts/verify_release.py

# Build, verify, and publish only if verification passes
python integrations/paknow-mcp/scripts/release.py --publish
```

Verification artifacts are written under `.omp/release-verify/<timestamp>/`.

## Development

```bash
cd integrations/paknow-mcp
uv venv .venv
uv pip install -e ".[dev]"
pytest
```

## Design notes

- **HTTP, not imports.** Qdrant embedded mode permits a single client per path.
  A second Python process importing `run_query` would contend for that path and
  duplicate model loads.
- **Answer first, search on gap.** `paknow_ask` owns routing, retrieval, sufficiency,
  generation, and citation binding. `paknow_search` is for explicit evidence gaps,
  not for replacing the answer pipeline.
- **No automatic retry for `ask`/`chart`.** These calls write `RunTrace` and may
  trigger the memory optimizer. Read-only tools retry once on transient failures.
- **Citation bases are explicit.** Tool text is 1-based for humans; structured
  output keeps `citation_index_base: 0` and the raw answer.
- **Scoped output.** Quotes are capped and full page text is never dumped.
