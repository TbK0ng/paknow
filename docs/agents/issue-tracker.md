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

`.scratch/` is not gitignored in this repo, so tickets are committed alongside the code they describe.

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

## When a skill says "publish to the issue tracker"

Create a new file under `.scratch/<feature-slug>/` (creating the directory if needed).

## When a skill says "fetch the relevant ticket"

Read the file at the referenced path. The user will normally pass the path or the issue number directly.

## Wayfinding operations

Used by `/wayfinder`. The **map** is a file with one **child** file per ticket.

- **Map**: `.scratch/<effort>/map.md` (the Notes / Decisions-so-far / Fog body).
- **Child ticket**: `.scratch/<effort>/issues/NN-<slug>.md`, numbered from `01`, with the question in the body. A `Type:` line records the ticket type (`research`/`prototype`/`grilling`/`task`); the `**Status:**` line records `claimed`/`resolved`.
- **Blocking**: a `**Blocked by:** NN, NN` line near the top. A ticket is unblocked when every file it lists is `resolved`.
- **Frontier**: scan `.scratch/<effort>/issues/` for files that are open, unblocked, and unclaimed; first by number wins.
- **Claim**: set `**Status:** claimed` and save before any work.
- **Resolve**: append the answer under an `## Answer` heading, set `**Status:** resolved`, then append a context pointer (gist + link) to the map's Decisions-so-far in `map.md`.
