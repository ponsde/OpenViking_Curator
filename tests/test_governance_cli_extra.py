from __future__ import annotations

import argparse


def test_cmd_flags_invalid_type(capsys, tmp_path):
    from curator.governance_cli import cmd_flags

    args = argparse.Namespace(data_path=str(tmp_path), all=False, type="bad", severity=None, cycle=None)
    code = cmd_flags(args)
    err = capsys.readouterr().err
    assert code == 1
    assert "invalid --type" in err


def test_cmd_flags_lists_pending(monkeypatch, capsys, tmp_path):
    from curator.governance_cli import cmd_flags

    monkeypatch.setattr(
        "curator.governance.load_flags",
        lambda **kwargs: [
            {
                "flag_id": "flag_123456789abc",
                "flag_type": "stale_resource",
                "severity": "medium",
                "status": "pending",
                "uri": "viking://resources/a",
            }
        ],
    )

    args = argparse.Namespace(data_path=str(tmp_path), all=False, type=None, severity=None, cycle=None)
    code = cmd_flags(args)
    out = capsys.readouterr().out
    assert code == 0
    assert "stale_resource" in out
    assert "Total: 1" in out


def test_cmd_report_no_reports(tmp_path, capsys):
    from curator.governance_cli import cmd_report

    args = argparse.Namespace(data_path=str(tmp_path), format="ascii")
    code = cmd_report(args)
    out = capsys.readouterr().out
    assert code == 1
    assert "No governance reports found" in out
