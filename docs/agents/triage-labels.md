# Triage statuses

The [local Markdown tracker](issue-tracker.md) records triage and work state in exactly one line per ticket or spec:

```markdown
**Status:** ready-for-agent
```

| Status | When |
| --- | --- |
| `needs-triage` | Maintainer needs to evaluate the issue |
| `needs-info` | More information is needed |
| `ready-for-agent` | Ready for implementation by an agent; all blockers resolved |
| `ready-for-human` | Requires human implementation; all blockers resolved |
| `wontfix` | Will not be acted on |
| `claimed` | In progress; all blockers already resolved |
| `resolved` | Done |

The first five are triage states; `claimed` and `resolved` track progress. The checker accepts plain `Status:` for older files; use the bold form for new files.
