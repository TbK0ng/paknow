# Issue tracker: Local Markdown

Issues and specs for this repo live as markdown files in `.scratch/`.

## Conventions

- One feature per directory: `.scratch/<feature-slug>/`
- The spec is `.scratch/<feature-slug>/spec.md`
- Implementation issues are one file per ticket at `.scratch/<feature-slug>/issues/<NN>-<slug>.md`, numbered from `01`, never a single combined tickets file
- Triage state is recorded as exactly one `**Status:** <value>` line near the top of each issue file and of `spec.md` (see `triage-labels.md` for the allowed values). The bold form is canonical; the checker accepts a plain `Status:` line for older files.
- Dependencies are recorded as `**Blocked by:** 01, 02` (comma-separated ticket numbers). Ticket numbers on that line must resolve to an `NN-*.md` file in the same `issues/` directory.
- A feature pins the commit it is reviewed against with `**Review base:** <sha>` near the top of `spec.md`. The value is a 7-40 character hex commit SHA.
- Comments and conversation history append to the bottom of the file under a `## Comments` heading

`.scratch/` specs and tickets are committed alongside the code they describe. Local checkpoints and generated eval runs are not specs or tickets.

## Checking the tracker

`scripts/check_scratch.py` (stdlib only) validates every `.scratch/*/spec.md` and `.scratch/*/issues/*.md`:

- exactly one status with a known value;
- every `Blocked by` reference names an existing ticket in the same feature, and a ticket that is `ready-for-agent`, `ready-for-human` or `claimed` has only `resolved` blockers;
- a `Review base`, when present, is a valid short SHA;
- a missing `.scratch/` directory is fine.

```bash
python scripts/check_scratch.py              # whole repo
python scripts/check_scratch.py --root <dir> # another checkout
```

It prints `path:line: message` per failure and exits 1; otherwise one `scratch: N file(s) OK` line. The same check runs in the `scratch` pre-commit hook (on changes under `.scratch/`) and in `.github/workflows/check.yml`.

## Keeping the tracker current

Remove the spec and tickets when a feature is retired. Keep runnable commands and supported endpoints in the docs linked from [AGENTS.md](../../AGENTS.md).
