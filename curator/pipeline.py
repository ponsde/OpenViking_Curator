"""Pipeline: main 8-step run() function."""

import os
import json

import openviking as ov
from metrics import Metrics
from memory_capture import capture_case

from .config import log, validate_config, OPENVIKING_CONFIG_FILE, DATA_PATH
from .router import route_scope
from .retrieval import local_search, build_priority_context
from .search import external_boost_needed, external_search, cross_validate
from .review import judge_and_pack, ingest_markdown, detect_conflict
from .answer import answer, _build_source_footer


def run(query: str) -> dict:
    """Main pipeline. Returns structured result dict."""
    m = Metrics()
    validate_config()
    os.environ["OPENVIKING_CONFIG_FILE"] = OPENVIKING_CONFIG_FILE

    result = {"query": query, "answer": "", "meta": {}, "metrics": {}, "case_path": None}

    log.info("STEP 1/8 初始化...")
    client = ov.SyncOpenViking(path=DATA_PATH)
    client.initialize()
    m.step('init', True)

    try:
        log.info("STEP 2/8 范围路由...")
        scope = route_scope(query)
        m.step('route', True, {'domain': scope.get('domain'), 'confidence': scope.get('confidence')})
        m.score('router_confidence', scope.get('confidence', 0))
        log.info("STEP 2 完成: domain=%s, confidence=%s", scope.get('domain'), scope.get('confidence'))

        log.info("STEP 3/8 本地检索...")
        local_txt, coverage, meta = local_search(client, query, scope)
        m.step('local_search', True, {'coverage': coverage, 'kw_cov': meta.get('kw_cov'), 'domain_hit': meta.get('domain_hit')})
        m.score('coverage_before_external', round(coverage, 3))
        log.info("STEP 3 完成: coverage=%.2f, core_cov=%s, uris=%d",
                 coverage, meta.get('core_cov', '?'), len(meta.get('uris', [])))
        log.debug("local_search detail: %s", json.dumps({
            k: meta.get(k) for k in ['kw_cov', 'core_cov', 'domain_hit', 'relevance',
                                       'evidence_ratio', 'avg_top_trust', 'fresh_ratio',
                                       'priority_uris', 'target_terms']
        }, ensure_ascii=False, default=str))

        external_txt = ""
        ingested = False
        cv_warnings = []
        boost_needed, boost_reason = external_boost_needed(query, scope, coverage, meta)

        if boost_needed:
            m.flag('external_triggered', True)
            m.flag('external_reason', boost_reason)
            log.info("STEP 4/8 外部搜索... reason=%s", boost_reason)
            try:
                external_txt = external_search(query, scope)
                m.step('external_search', True, {'len': len(external_txt), 'reason': boost_reason})
                log.info("STEP 4 完成: %d chars", len(external_txt))
            except Exception as e:
                log.warning("STEP 4 外部搜索失败: %s", e)
                m.step('external_search', False, {'error': str(e)})

            if external_txt:
                log.info("STEP 5/8 交叉验证...")
                cv = cross_validate(query, external_txt, scope)
                external_txt = cv.get("validated", external_txt)
                cv_warnings = cv.get("warnings", [])
                m.step('cross_validate', True, {
                    'followup_done': cv.get('followup_done', False),
                    'high_risk_count': cv.get('high_risk_count', 0),
                })
                if cv_warnings:
                    log.warning("交叉验证警告: %s", cv_warnings[:3])

                log.info("STEP 6/8 审核入库...")
                j = judge_and_pack(query, external_txt)
                m.step('judge', True, {'pass': j.get('pass'), 'trust': j.get('trust')})
                log.info("审核: pass=%s, trust=%s, freshness=%s",
                         j.get('pass'), j.get('trust'), j.get('freshness'))
                if j.get("pass") and j.get("markdown"):
                    freshness = j.get("freshness", "unknown")
                    if freshness == "outdated":
                        m.step('ingest', False, {'reason': 'outdated_info'})
                        log.warning("未入库: 信息已过时 (freshness=outdated)")
                    else:
                        try:
                            ing = ingest_markdown(client, "curated", j["markdown"], freshness=freshness)
                            ingested = True
                            m.step('ingest', True, {'uri': ing.get('root_uri', '')})
                            log.info("已入库: %s", ing.get("root_uri", ""))
                        except Exception as e:
                            m.step('ingest', False, {'error': str(e)})
                            log.warning("入库失败: %s", e)
                else:
                    m.step('ingest', False)
        else:
            m.flag('external_triggered', False)
            m.flag('external_reason', boost_reason)
            log.info("STEP 4/8 跳过外搜: %s", boost_reason)

        log.info("STEP 7/8 冲突检测...")
        conflict = detect_conflict(query, local_txt, external_txt)
        conflict_card = ""
        if conflict.get('has_conflict'):
            pts = '\n'.join([f"- {x}" for x in conflict.get('points', [])[:5]])
            conflict_card = f"⚠️ 存在冲突: {conflict.get('summary','')}\n{pts}"
        m.step('conflict', True, {'has_conflict': conflict.get('has_conflict', False)})
        m.flag('has_conflict', bool(conflict.get('has_conflict', False)))

        # ── 渐进式去重（每次检查 2-3 对，不阻塞主流程） ──
        try:
            from .dedup import incremental_dedup
            dedup_uris = meta.get('uris', [])
            if len(dedup_uris) >= 2:
                dedup_result = incremental_dedup(client, dedup_uris, max_checks=2)
                if dedup_result["merged"] > 0:
                    log.info("dedup: 本次合并 %d 对重复文档", dedup_result["merged"])
                m.step('dedup', True, dedup_result)
            else:
                m.step('dedup', True, {'checked': 0, 'merged': 0})
        except Exception as e:
            log.debug("dedup skipped: %s", e)
            m.step('dedup', False, {'error': str(e)})

        log.info("STEP 8/8 生成回答...")
        priority_ctx = build_priority_context(client, meta.get('priority_uris', []), query=query)
        ans = answer(query, local_txt, external_txt, priority_ctx=priority_ctx,
                     conflict_card=conflict_card, warnings=cv_warnings)

        source_info = _build_source_footer(meta, coverage, boost_needed, cv_warnings)
        ans = ans.rstrip() + "\n\n" + source_info

        m.step('answer', True, {'answer_len': len(ans), 'priority_uris': meta.get('priority_uris', [])})
        m.score('priority_uris_count', len(meta.get('priority_uris', [])))
        m.flag('ingested', ingested)
        m.score('answer_len', len(ans))
        report = m.finalize()

        case_path = None
        if os.getenv('CURATOR_CAPTURE_CASE', '1') in ('1', 'true', 'True'):
            case_path = capture_case(query, scope, report, ans, out_dir=os.getenv('CURATOR_CASE_DIR', 'cases'))

        result["answer"] = ans
        result["meta"] = {
            "coverage": coverage,
            "core_cov": meta.get("core_cov"),
            "external_triggered": report["flags"].get("external_triggered", False),
            "external_reason": report["flags"].get("external_reason"),
            "has_conflict": report["flags"].get("has_conflict", False),
            "ingested": ingested,
            "priority_uris": meta.get("priority_uris", []),
            "warnings": cv_warnings,
        }
        result["metrics"] = {
            "duration_sec": report["duration_sec"],
            "flags": report["flags"],
            "scores": report["scores"],
        }
        result["case_path"] = case_path
        log.info("完成: %.1fs, coverage=%.2f, external=%s",
                 report["duration_sec"], coverage, report["flags"].get("external_triggered"))
    finally:
        try:
            client.close()
        except Exception:
            pass

    return result
