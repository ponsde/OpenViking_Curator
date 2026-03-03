from __future__ import annotations

import argparse


def test_cmd_show_not_found(monkeypatch, capsys, tmp_path):
    from curator.governance_cli import cmd_show

    monkeypatch.setattr("curator.governance.load_flags", lambda **kwargs: [])
    args = argparse.Namespace(data_path=str(tmp_path), flag_id="abc")
    code = cmd_show(args)
    assert code == 1
    assert "flag not found" in capsys.readouterr().err


def test_cmd_show_success(monkeypatch, capsys, tmp_path):
    from curator.governance_cli import cmd_show

    monkeypatch.setattr(
        "curator.governance.load_flags",
        lambda **kwargs: [
            {
                "flag_id": "flag_aaaabbbbcccc",
                "flag_type": "stale_resource",
                "severity": "low",
                "status": "keep",
                "uri": "viking://resources/x",
                "reason": "old",
                "cycle_id": "cycle_1",
                "timestamp": "2026-01-01T00:00:00Z",
                "details": {"k": "v"},
                "resolution_reason": "confirmed",
                "resolved_at": "2026-01-02T00:00:00Z",
            }
        ],
    )
    args = argparse.Namespace(data_path=str(tmp_path), flag_id="cccc")
    code = cmd_show(args)
    out = capsys.readouterr().out
    assert code == 0
    assert "stale_resource" in out
    assert "decision:" in out


def test_update_flag_cmd_batch(monkeypatch, capsys, tmp_path):
    from curator.governance_cli import _update_flag_cmd

    monkeypatch.setattr(
        "curator.governance.load_flags",
        lambda **kwargs: [
            {
                "flag_id": "flag_1",
                "flag_type": "stale_resource",
                "uri": "viking://resources/a",
                "status": "pending",
            }
        ],
    )
    monkeypatch.setattr("curator.governance.batch_update_flags", lambda ids, status, reason, data_path: (ids, []))

    args = argparse.Namespace(
        data_path=str(tmp_path),
        reason="ok",
        batch=True,
        type="stale_resource",
        severity=None,
        flag_ids=[],
    )
    code = _update_flag_cmd(args, "keep")
    out = capsys.readouterr().out
    assert code == 0
    assert "Updated 1 flag(s)" in out


def test_update_flag_cmd_multi_id(monkeypatch, capsys, tmp_path):
    from curator.governance_cli import _update_flag_cmd

    monkeypatch.setattr(
        "curator.governance.load_flags",
        lambda **kwargs: [
            {"flag_id": "flag_11112222", "flag_type": "stale_resource", "uri": "u1", "status": "pending"},
            {"flag_id": "flag_33334444", "flag_type": "broken_url", "uri": "u2", "status": "pending"},
        ],
    )
    monkeypatch.setattr(
        "curator.governance.batch_update_flags",
        lambda ids, status, reason, data_path: (ids, []),
    )

    args = argparse.Namespace(
        data_path=str(tmp_path),
        reason=None,
        batch=False,
        type=None,
        severity=None,
        flag_ids=["2222", "4444"],
    )
    code = _update_flag_cmd(args, "ignore")
    out = capsys.readouterr().out
    assert code == 0
    assert "→ ignore" in out
