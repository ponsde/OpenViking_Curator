# Mixed Mode Runtime Strategy

This project runs in **hybrid mode**:

## A) Background (low frequency, proactive)

Run daily (recommended):

1. `star_watch.py` – notify only when star count changes
2. `maintenance.py` – decay feedback + stale case report
3. `freshness_rescan.py` – refresh source metadata

Suggested schedule (Beijing time):
- 09:00 star check
- 09:10 maintenance
- 09:20 freshness rescan

## B) Foreground (high value, on-demand)

For each user query:

1. scope routing
2. local retrieval
3. coverage gate
4. external search only if needed
5. review + optional ingest
6. conflict detection
7. final answer + metrics

## Default tuning values

- Coverage gate threshold: `0.65`
- Router model order: `gemini-3-flash-preview -> gemini-3-flash-high -> Claude-Sonnet 4-5`
- External search model: `grok-4-fast`
- Feedback decay factor: `0.95` daily
- Stale case threshold: `30` days

## Why this works

- Keeps cost predictable
- Preserves answer speed
- Maintains a self-evolving knowledge base
