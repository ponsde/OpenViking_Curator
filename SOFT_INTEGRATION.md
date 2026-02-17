# Soft Integration Plan (OpenClaw + Curator + OpenViking Memory)

## Goal
- Keep OpenClaw main flow unchanged (stable)
- Route knowledge-heavy questions into Curator when useful
- Store durable long-term memory into OpenViking (`long_memory` collection)

## Current soft mode

### 1) Long-term memory bridge
Use:

```bash
python openviking_memory_bridge.py remember "<text>" --tags "pref,project" --source chat
python openviking_memory_bridge.py recall "<query>" --limit 8
```

### 2) Knowledge query path (manual soft trigger)
Use Curator for retrieval-heavy questions:

```bash
python curator_v0.py "<query>"
```

If Curator fails, fall back to default OpenClaw response path.

## Next (full soft automation)
- Add a lightweight query classifier (`knowledge` vs `normal`) and auto-trigger Curator in the wrapper.
- Wire memory bridge into the conversation hooks:
  - if user says “记住/remember this”, call `remember`
  - if user asks prior preferences/history, call `recall`
