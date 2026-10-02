"""045 lane 5: the scanner. No network, no real tools: a missing tool is a
NAMED "not run", never a pass; the verdict and exit code follow the contract."""
import importlib.util
import json
import pathlib
import subprocess
import types

import pytest

_SPEC = importlib.util.spec_from_file_location(
    "security_scan",
    pathlib.Path(__file__).resolve().parents[3] / "scripts" / "security_scan.py")
sc = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(sc)


def _proc(rc, out="", err=""):
    return types.SimpleNamespace(returncode=rc, stdout=out, stderr=err)


def _repo(tmp_path):
    (tmp_path / "requirements.lock").write_text("x==1\n")
    (tmp_path / ".gitleaks.toml").write_text("")
    (tmp_path / ".git").mkdir()
    return tmp_path


def test_missing_tools_are_not_run(tmp_path, monkeypatch):
    monkeypatch.setattr("importlib.util.find_spec", lambda n: None)
    r = sc.check_pip_audit(_repo(tmp_path), which=lambda n: None)
    assert r["status"] == "not_run" and "not installed" in r["reason"]
    g = sc.check_gitleaks(tmp_path, which=lambda n: None)
    assert g["status"] == "not_run" and "not installed" in g["reason"]


def test_pip_audit_network_failure_is_not_a_pass(tmp_path):
    r = sc.check_pip_audit(_repo(tmp_path), which=lambda n: "/x/pip-audit",
                           run=lambda c, t: _proc(2, err="ConnectionError: no route"))
    assert r["status"] == "not_run"
    assert "ConnectionError" in r["reason"]


def test_pip_audit_findings_and_clean(tmp_path):
    repo = _repo(tmp_path)
    vuln = {"dependencies": [{"name": "foo", "version": "1.0",
                              "vulns": [{"id": "PYSEC-1", "fix_versions": ["1.1"]}]},
                             {"name": "bar", "version": "2", "vulns": []}]}
    r = sc.check_pip_audit(repo, which=lambda n: "/x",
                           run=lambda c, t: _proc(1, json.dumps(vuln)))
    assert r["status"] == "findings"
    assert r["findings"] == ["foo==1.0: PYSEC-1 (fix: 1.1)"]
    ok = sc.check_pip_audit(repo, which=lambda n: "/x",
                            run=lambda c, t: _proc(0, json.dumps({"dependencies": []})))
    assert ok["status"] == "clean"


def test_pip_audit_timeout_is_not_run(tmp_path):
    def boom(c, t):
        raise subprocess.TimeoutExpired(c, t)
    r = sc.check_pip_audit(_repo(tmp_path), which=lambda n: "/x", run=boom)
    assert r["status"] == "not_run" and "timed out" in r["reason"]


def test_gitleaks_reports_location_never_the_secret(tmp_path):
    repo = _repo(tmp_path)

    def run(cmd, timeout):
        assert "--redact" in cmd
        path = cmd[cmd.index("--report-path") + 1]
        pathlib.Path(path).write_text(json.dumps([{
            "RuleID": "generic-api-key", "File": "a.py", "StartLine": 3,
            "Commit": "abcdef1234567", "Secret": "SHOULD-NOT-APPEAR",
            "Match": "SHOULD-NOT-APPEAR"}]))
        return _proc(1)
    r = sc.check_gitleaks(repo, which=lambda n: "/x", run=run)
    assert r["status"] == "findings"
    assert r["findings"] == ["generic-api-key a.py:3 @abcdef123"]
    assert "SHOULD-NOT-APPEAR" not in json.dumps(r)


def test_host_checks_skip_cleanly_off_box(tmp_path):
    absent = tmp_path / "nope"
    for r in (sc.check_host_files(absent), sc.check_host_units(tmp_path, absent),
              sc.check_config_drift(absent), sc.check_tls_expiry(tmp_path, absent)):
        assert r["status"] == "not_run" and "off-box" in r["reason"]


def test_host_file_modes(tmp_path):
    etc = tmp_path / "etc"
    etc.mkdir()
    (etc / "polyrob.env").write_text("A=1\n")
    (etc / "polyrob.env").chmod(0o640)
    (etc / "release.env").write_text("T=1\n")
    (etc / "release.env").chmod(0o640)
    (etc / "leaky.env").write_text("")
    (etc / "leaky.env").chmod(0o644)
    r = sc.check_host_files(etc)
    assert r["status"] == "findings"
    text = " ".join(r["findings"])
    assert "leaky.env" in text and "release.env" in text
    assert "polyrob.env" not in text


def test_units_must_not_run_as_root(tmp_path):
    etc = tmp_path / "etc"; etc.mkdir()
    units = tmp_path / "units"; units.mkdir()
    (units / "polyrob.service").write_text("[Service]\nUser=polyrob\n")
    (units / "polyrob-webview.service").write_text("[Service]\nExecStart=x\n")
    r = sc.check_host_units(units, etc)
    assert r["findings"] == ["polyrob-webview.service: no User= (runs as root)"]


def test_config_drift_reads_names_not_values(tmp_path):
    etc = tmp_path / "etc"; etc.mkdir()
    (etc / "polyrob.env").write_text("WEBVIEW_READ_ONLY=true\nPOLYROB_POSTURE=\nSECRET=hunter2\n")
    r = sc.check_config_drift(etc)
    assert r["findings"] == [f"POLYROB_POSTURE is not set in {etc / 'polyrob.env'} (defaults apply)"]
    assert "hunter2" not in json.dumps(r)


def test_aggregate_and_exit_codes():
    clean = sc._result("a", "clean")
    nr = sc._not_run("b", "gone")
    bad = sc._result("c", "findings", findings=["x", "y"])
    assert sc.exit_code_for(sc.aggregate([clean])) == sc.EXIT_CLEAN
    v = sc.aggregate([clean, nr])
    assert v["verdict"] == "incomplete" and v["not_run"] == ["b"]
    assert sc.exit_code_for(v) == sc.EXIT_INCOMPLETE
    v = sc.aggregate([clean, nr, bad])
    assert v["verdict"] == "findings" and v["findings"] == 2
    assert sc.exit_code_for(v) == sc.EXIT_FINDINGS


def test_main_writes_verdict_the_digest_reads(tmp_path, monkeypatch):
    monkeypatch.setattr(sc, "build_checks", lambda repo: {
        "pip_audit": lambda: sc._not_run("pip_audit", "pip-audit not installed"),
        "gitleaks": lambda: sc._result("gitleaks", "clean"),
    })
    rc = sc.main(["--repo", str(tmp_path), "--data", str(tmp_path / "data")])
    assert rc == sc.EXIT_INCOMPLETE
    from core.security_digest import read_scan_verdict
    import time
    assert read_scan_verdict(str(tmp_path / "data"), time.time()) == \
        "incomplete (pip_audit not run)"
    reports = list((tmp_path / "docs" / "ops").glob("security-*.md"))
    assert len(reports) == 1
    assert "NOT RUN (pip-audit not installed)" in reports[0].read_text()


def test_a_crashing_check_is_not_run(tmp_path, monkeypatch):
    def crash():
        raise RuntimeError("bad")
    monkeypatch.setattr(sc, "build_checks", lambda repo: {"gitleaks": crash})
    rc = sc.main(["--repo", str(tmp_path), "--data", str(tmp_path), "--no-report"])
    assert rc == sc.EXIT_INCOMPLETE


def test_unknown_skip_is_a_usage_error(tmp_path):
    assert sc.main(["--repo", str(tmp_path), "--skip", "nope"]) == sc.EXIT_ERROR
