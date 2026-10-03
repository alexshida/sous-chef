"""The CLI commands that manage the background service, and dedupe."""

import plistlib
import subprocess

import pytest
from click.testing import CliRunner

from sous_chef import main


@pytest.fixture
def mac(tmp_path, monkeypatch):
    monkeypatch.setattr(main.sys, "platform", "darwin")
    monkeypatch.setattr(main.Path, "home", lambda: tmp_path)
    calls = []

    def fake_run(cmd, **kw):
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, "", "")
    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.setattr(main, "_wait_for_server", lambda port, seconds=15.0: True)
    return tmp_path, calls


def _install_plist(home, port=8766):
    path = home / "Library" / "LaunchAgents" / f"{main.SERVICE_LABEL}.plist"
    path.parent.mkdir(parents=True)
    path.write_bytes(plistlib.dumps({"Label": main.SERVICE_LABEL,
                                     "ProgramArguments": ["/x/sous-chef", "web", "--port", str(port)]}))


def test_restart_kicks_the_installed_service(mac):
    home, calls = mac
    _install_plist(home, port=9001)
    result = CliRunner().invoke(main.cli, ["restart"])
    assert result.exit_code == 0, result.output
    assert calls[0][:3] == ["launchctl", "kickstart", "-k"]
    assert calls[0][3].endswith("/com.sous-chef.web")
    assert "localhost:9001" in result.output


def test_restart_without_a_service_says_what_to_do(mac):
    result = CliRunner().invoke(main.cli, ["restart"])
    assert result.exit_code == 1
    assert "install-service" in result.output and "Ctrl-C" in result.output


def test_restart_loads_a_service_that_was_unloaded(mac, monkeypatch):
    home, calls = mac
    _install_plist(home)

    def fake_run(cmd, **kw):
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 3 if cmd[1] == "kickstart" else 0, "", "not loaded")
    monkeypatch.setattr(subprocess, "run", fake_run)
    result = CliRunner().invoke(main.cli, ["restart"])
    assert result.exit_code == 0, result.output
    assert [c[1] for c in calls] == ["kickstart", "load"]


def test_dedupe_lists_before_it_merges(fresh_db):
    result = CliRunner().invoke(main.cli, ["dedupe"])
    assert result.exit_code == 0 and "No recipes saved more than once" in result.output


def test_doctor_reports_and_says_what_to_fix(fresh_db, monkeypatch):
    from sous_chef.web import chef

    def missing():
        raise chef.ChefError("not found")
    monkeypatch.setattr(chef, "claude_cli_path", missing)
    result = CliRunner().invoke(main.cli, ["doctor", "--port", "1"])
    assert result.exit_code == 0, result.output            # optional things only warn
    for name in ("Python", "Data", "Claude", "Server", "Phone access"):
        assert name in result.output
    assert "claude.ai/install.sh" in result.output


def test_doctor_fails_when_sous_chef_cannot_work(fresh_db, monkeypatch):
    from sous_chef import doctor
    monkeypatch.setattr(doctor, "check_data",
                        lambda: doctor.Check("Data", doctor.FAIL, "read-only", "fix it"))
    result = CliRunner().invoke(main.cli, ["doctor", "--port", "1"])
    assert result.exit_code == 1 and "fix it" in result.output
