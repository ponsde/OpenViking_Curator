"""Background scheduler for periodic maintenance tasks.

Disabled by default. Enable by setting ``CURATOR_SCHEDULER_ENABLED=1``.

Jobs
----
freshness
    Scan OV resources, re-search any whose freshness score is below the
    stale threshold.  Runs every ``CURATOR_FRESHNESS_INTERVAL_HOURS`` hours
    (default 24).

strengthen
    Read ``data/weak_topics.json`` (written by ``scripts/analyze_weak.py``)
    and re-run the pipeline for the top-N weakest topics.  Runs every
    ``CURATOR_STRENGTHEN_INTERVAL_HOURS`` hours (default 168 = 7 days).

governance
    Run the full governance cycle (audit, flag, proactive search, report).
    Runs every ``CURATOR_GOVERNANCE_INTERVAL_HOURS`` hours (default 168 = 7 days).
    Enable separately with ``CURATOR_GOVERNANCE_ENABLED=1``.
    Phase 4 (proactive search + replay) runs fully async by default
    (``CURATOR_GOVERNANCE_SYNC_BUDGET=0``).  Results are harvested by the
    next cycle.  Set sync_budget > 0 for immediate feedback if needed.
    When governance is enabled, it subsumes strengthen's functionality.
    Set ``CURATOR_GOVERNANCE_REPLACES_STRENGTHEN=1`` to skip the standalone
    strengthen job.

Config env vars
---------------
CURATOR_SCHEDULER_ENABLED=1               required to activate (default off)
CURATOR_FRESHNESS_INTERVAL_HOURS=24       scan interval in hours
CURATOR_STRENGTHEN_INTERVAL_HOURS=168     strengthen interval in hours (7 days)
CURATOR_STRENGTHEN_TOP_N=3                number of weak topics per run
CURATOR_FRESHNESS_STALE_THRESHOLD=0.4     freshness score below = stale
CURATOR_GOVERNANCE_ENABLED=0              governance cycle (default off)
CURATOR_GOVERNANCE_INTERVAL_HOURS=168     governance interval (7 days)
CURATOR_GOVERNANCE_MODE=normal            "normal" or "team"
CURATOR_GOVERNANCE_REPLACES_STRENGTHEN=0  skip strengthen when governance is on

APScheduler is a transitive dependency of openviking, so it is always
available when the package is installed.  An ImportError fallback is kept
as a safety net.
"""

import json
import os
import threading
from typing import Callable

from .config import DATA_PATH, env, log
from .freshness import uri_freshness_score

_scheduler = None
_scheduler_lock = threading.Lock()

_ENABLED_VALUES = {"1", "true", "yes", "on"}


# ── Freshness job ──


def _run_freshen(
    *,
    _backend=None,
    _run_fn: Callable | None = None,
) -> dict:
    """Scan all resources; re-search those below the stale threshold.

    Args:
        _backend: Optional backend override (for testing).
        _run_fn:  Optional pipeline run override (for testing).

    Returns:
        Summary dict: ``{checked, stale, re_searched}``.
    """
    try:
        stale_threshold = float(env("CURATOR_FRESHNESS_STALE_THRESHOLD", "0.4"))
        if _backend is None:
            from .backend_ov import OpenVikingBackend

            _backend = OpenVikingBackend()
        if _run_fn is None:
            from .pipeline_v2 import run as _pipeline_run

            _fn: Callable = _pipeline_run
        else:
            _fn = _run_fn

        uris = _backend.list_resources()
        if not uris:
            log.info("scheduler.freshness: no resources found, skipping")
            return {"checked": 0, "stale": 0, "re_searched": 0}

        stale_uris = [u for u in uris if uri_freshness_score(u) < stale_threshold]

        log.info(
            "scheduler.freshness: checked=%d stale=%d",
            len(uris),
            len(stale_uris),
        )

        re_searched = 0
        for uri in stale_uris:
            try:
                abstract = _backend.abstract(uri)
                topic = abstract[:100] if abstract else uri.split("/")[-1].replace("_", " ")
                _fn(topic)
                re_searched += 1
                log.debug("scheduler.freshness: re-searched uri=%s", uri)
            except Exception as e:
                log.debug("scheduler.freshness: re-search failed uri=%s: %s", uri, e)

        log.info("scheduler.freshness: re_searched=%d", re_searched)
        return {"checked": len(uris), "stale": len(stale_uris), "re_searched": re_searched}

    except Exception as e:
        log.warning("scheduler.freshness: job error: %s", e)
        return {"checked": 0, "stale": 0, "re_searched": 0, "error": str(e)}


# ── Strengthen job ──


def _run_strengthen(
    *,
    _run_fn: Callable | None = None,
    data_path: str | None = None,
    top_n: int | None = None,
) -> dict:
    """Read weak_topics.json and re-search the top-N weakest topics.

    Args:
        _run_fn:    Optional pipeline run override (for testing).
        data_path:  Override data directory (for testing).
        top_n:      Override number of topics (for testing).

    Returns:
        Summary dict: ``{strengthened, skipped}``.
    """
    _data_path = data_path or DATA_PATH
    if top_n is not None:
        _top_n = top_n
    else:
        try:
            _top_n = max(1, int(env("CURATOR_STRENGTHEN_TOP_N", "3")))
        except (ValueError, TypeError):
            log.warning("scheduler.strengthen: invalid CURATOR_STRENGTHEN_TOP_N, using 3")
            _top_n = 3

    if _run_fn is None:
        from .pipeline_v2 import run as _pipeline_run

        _fn: Callable = _pipeline_run
    else:
        _fn = _run_fn

    weak_path = os.path.join(_data_path, "weak_topics.json")
    if not os.path.exists(weak_path):
        log.info("scheduler.strengthen: %s not found, skipping", weak_path)
        return {"strengthened": 0, "skipped": 0}

    try:
        with open(weak_path, encoding="utf-8") as f:
            weak_topics = json.load(f)
    except Exception as e:
        log.warning("scheduler.strengthen: failed to read weak_topics.json: %s", e)
        return {"strengthened": 0, "skipped": 0}

    if not isinstance(weak_topics, list):
        log.warning(
            "scheduler.strengthen: weak_topics.json is not a list (got %s), skipping", type(weak_topics).__name__
        )
        return {"strengthened": 0, "skipped": 0}

    targets = weak_topics[:_top_n]
    if not targets:
        log.info("scheduler.strengthen: no weak topics, skipping")
        return {"strengthened": 0, "skipped": 0}

    log.info("scheduler.strengthen: strengthening top %d weak topics", len(targets))
    strengthened = 0
    for t in targets:
        if not isinstance(t, dict):
            log.debug("scheduler.strengthen: skipping non-dict entry: %r", t)
            continue
        topic = t.get("topic", "")
        if not topic:
            continue
        query = f"{topic} 最佳实践与常见问题"
        try:
            _fn(query)
            strengthened += 1
            log.debug("scheduler.strengthen: done topic=%s", topic[:40])
        except Exception as e:
            log.debug("scheduler.strengthen: topic=%s error: %s", topic[:40], e)

    log.info(
        "scheduler.strengthen: completed=%d/%d",
        strengthened,
        len(targets),
    )
    return {"strengthened": strengthened, "skipped": len(targets) - strengthened}


# ── Governance job ──


def _run_governance(
    *,
    _run_fn: Callable | None = None,
    data_path: str | None = None,
) -> dict:
    """Run the full governance cycle as a scheduled job.

    Args:
        _run_fn:    Optional pipeline run override (for testing).
        data_path:  Override data directory (for testing).

    Returns:
        Governance report dict.
    """
    try:
        from .backend_ov import OpenVikingBackend
        from .governance import run_governance_cycle

        mode = env("CURATOR_GOVERNANCE_MODE", "normal")
        dry_run = env("CURATOR_GOVERNANCE_DRY_RUN", "").lower() in _ENABLED_VALUES

        backend = OpenVikingBackend()
        report = run_governance_cycle(
            backend=backend,
            data_path=data_path,
            mode=mode,
            dry_run=dry_run,
            _run_fn=_run_fn,
        )
        log.info(
            "scheduler.governance: completed cycle=%s flags=%d proactive=%d",
            report.get("cycle_id", "?"),
            report.get("flags", {}).get("total", 0),
            report.get("proactive", {}).get("queries_run", 0),
        )
        return report
    except Exception as e:
        log.warning("scheduler.governance: job error: %s", e, exc_info=True)
        return {"error": str(e)}


# ── Lifecycle ──


def start_scheduler() -> bool:
    """Start the background scheduler if ``CURATOR_SCHEDULER_ENABLED=1``.

    Safe to call multiple times — only starts once per process.

    Returns:
        ``True`` if the scheduler was started, ``False`` if it was already
        running, disabled, or APScheduler is unavailable.
    """
    global _scheduler
    if env("CURATOR_SCHEDULER_ENABLED", "").lower() not in _ENABLED_VALUES:
        return False

    with _scheduler_lock:
        if _scheduler is not None:
            return False

        try:
            from apscheduler.schedulers.background import BackgroundScheduler

            freshness_h = max(1.0, float(env("CURATOR_FRESHNESS_INTERVAL_HOURS", "24") or "24"))
            strengthen_h = max(1.0, float(env("CURATOR_STRENGTHEN_INTERVAL_HOURS", "168") or "168"))

            governance_enabled = env("CURATOR_GOVERNANCE_ENABLED", "").lower() in _ENABLED_VALUES
            governance_replaces_strengthen = (
                governance_enabled and env("CURATOR_GOVERNANCE_REPLACES_STRENGTHEN", "").lower() in _ENABLED_VALUES
            )

            _scheduler = BackgroundScheduler(daemon=True)
            _scheduler.add_job(
                _run_freshen,
                "interval",
                hours=freshness_h,
                id="freshness_scan",
                max_instances=1,
                coalesce=True,
            )

            if not governance_replaces_strengthen:
                _scheduler.add_job(
                    _run_strengthen,
                    "interval",
                    hours=strengthen_h,
                    id="strengthen",
                    max_instances=1,
                    coalesce=True,
                )

            if governance_enabled:
                governance_h = max(1.0, float(env("CURATOR_GOVERNANCE_INTERVAL_HOURS", "168") or "168"))
                _scheduler.add_job(
                    _run_governance,
                    "interval",
                    hours=governance_h,
                    id="governance",
                    max_instances=1,
                    coalesce=True,
                )

            _scheduler.start()

            jobs_desc = "freshness=%.0fh" % freshness_h
            if not governance_replaces_strengthen:
                jobs_desc += " strengthen=%.0fh" % strengthen_h
            if governance_enabled:
                jobs_desc += " governance=%.0fh" % governance_h
            log.info("scheduler started: %s", jobs_desc)
            return True
        except ImportError:
            log.warning(
                "scheduler: APScheduler not installed — " "install openviking or apscheduler to enable background jobs"
            )
            return False
        except Exception as e:
            log.warning("scheduler: failed to start (pipeline unaffected): %s", e)
            _scheduler = None
            return False


def stop_scheduler() -> None:
    """Stop the background scheduler if running."""
    global _scheduler
    with _scheduler_lock:
        if _scheduler is not None:
            try:
                _scheduler.shutdown(wait=False)
            except Exception as e:
                log.debug("scheduler stop error: %s", e)
            _scheduler = None


def scheduler_status() -> dict:
    """Return current scheduler state and next-run times for each job.

    Returns:
        Dict with keys ``running`` (bool) and ``jobs`` (list of
        ``{id, next_run}`` dicts).
    """
    if _scheduler is None or not _scheduler.running:
        return {"running": False, "jobs": []}

    jobs = [
        {
            "id": job.id,
            "next_run": job.next_run_time.isoformat() if job.next_run_time else None,
        }
        for job in _scheduler.get_jobs()
    ]
    return {"running": True, "jobs": jobs}
