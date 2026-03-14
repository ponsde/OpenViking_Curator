---
name: curator
description: >
  Knowledge base query tool. Auto-triggers when the user asks knowledge/technical
  questions (concepts, how-to, troubleshooting, comparisons, architecture).
  Also invocable manually via /curator.
---

# Curator — Knowledge Base Query

Query a knowledge base with retrieval, external search fallback, conflict detection,
and auto-ingest. Returns structured context for you to answer from.

## Commands

```
/curator "question"              Query the knowledge base
/curator --status                Health check
/curator --review "question"     Query without auto-ingest (human review mode)
```

## How to run

```bash
python3 curator_query.py --force "question"
python3 curator_query.py --status
python3 curator_query.py --force --review "question"
```

Use `--force` to skip routing checks (recommended for all skill invocations).

Run from the Curator project directory, or adjust the path accordingly.

## Output

JSON with these key fields:

| Field | Meaning |
|-------|---------|
| `routed` | `false` = not a knowledge question, answer directly. `true` = see below. |
| `context` | Combined local + external context. **Use this to answer the user.** |
| `coverage` | 0.0–1.0. How well local knowledge covers the query. |
| `meta.external_triggered` | `true` if external search was used (local coverage insufficient). |
| `meta.has_conflict` | `true` if local and external sources disagree. |
| `meta.ingested` | `true` if new knowledge was ingested into the knowledge base. |

## When to use (auto-trigger)

**Use** when the user asks:
- Knowledge/concept questions (what is X, how does X work, X vs Y)
- Technical how-to (deploy, configure, troubleshoot)
- Error diagnosis (502, timeout, stack traces)
- Experience/history recall (how did we do X last time)

**Don't use** when:
- Casual chat (greetings, thanks, confirmations)
- Direct commands (commit, run tests, restart)
- The question is about the current code being edited (use codebase search instead)

## Auto-trigger toggle

- If the user says "disable auto-query" or similar, stop auto-triggering.
  Only respond to explicit `/curator` invocations.
- If the user says "enable auto-query" or similar, resume auto-triggering.

## How to present results

1. If `routed` is `false`: answer the question directly without Curator.
2. If `routed` is `true`: use the `context` field as reference to answer.
3. If `meta.external_triggered` is `true`: mention that some info came from external search.
4. If `meta.has_conflict` is `true`: warn the user that sources disagree.
