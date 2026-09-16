"""Run-bound evaluation identity and fail-closed, evidence-based scorecards.

This is an operator/evaluation API, never an agent-callable assertion writer.
Only independently executed verifiers may publish assertion results here. Keep
the run directory outside agent-controlled workspaces. Digests detect corruption
and mixed runs, not a malicious storage owner. Budgets are audited, not enforced
at a provider boundary; this module does not enable live execution.
"""
from __future__ import annotations

import hashlib
import copy
import json
import math
import os
import time
import uuid
from pathlib import Path, PurePosixPath

from core.security.confined_write import confined_parent

MAX_BYTES = 8 * 1024 * 1024


def digest(value) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                                    ensure_ascii=False, allow_nan=False).encode()).hexdigest()


def _identifier(value):
    if not isinstance(value, str) or not value or len(value) > 160:
        raise ValueError("invalid evaluation identifier")
    if any(c not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-.:" for c in value):
        raise ValueError("unsafe evaluation identifier")
    if value in {".", "..", "manifest", "completion"}:
        raise ValueError("reserved evaluation identifier")
    return value


def _exclusive_json(path: Path, value):
    encoded = json.dumps(value, sort_keys=True, indent=2, allow_nan=False).encode()
    if len(encoded) > MAX_BYTES:
        raise ValueError("evaluation record exceeds byte limit")
    with confined_parent(path, path.parent.parent) as (directory, name):
        fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                     0o600, dir_fd=directory)
        with os.fdopen(fd, "wb") as stream:
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        os.fsync(directory)


def _amount(value):
    if type(value) not in (int, float) or not math.isfinite(value) or value < 0:
        raise ValueError("spend must be finite and nonnegative")


def _validate_manifest(manifest):
    if not isinstance(manifest, dict) or manifest.get("version") != 1:
        raise ValueError("invalid manifest")
    _identifier(manifest.get("run_id"))
    if manifest.get("mode") not in {"fixture", "live"}:
        raise ValueError("invalid evaluation mode")
    _amount(manifest.get("budget_usd"))
    if manifest["mode"] == "fixture" and manifest["budget_usd"] != 0:
        raise ValueError("fixture evaluations cannot have a spend budget")
    if (type(manifest.get("seed")) is not int
            or not isinstance(manifest.get("configuration"), dict)
            or not isinstance(manifest.get("revision"), dict)):
        raise ValueError("invalid run configuration")
    scenarios = manifest.get("scenarios")
    if not isinstance(scenarios, list) or not 1 <= len(scenarios) <= 1000:
        raise ValueError("run needs a bounded nonempty scenario list")
    ids, goals = set(), set()
    for scenario in scenarios:
        if not isinstance(scenario, dict):
            raise ValueError("scenario must be an object")
        sid = _identifier(scenario.get("id"))
        if sid in ids:
            raise ValueError("duplicate scenario id")
        ids.add(sid)
        assertions = scenario.get("assertions")
        if not isinstance(assertions, list) or not 1 <= len(assertions) <= 100:
            raise ValueError("scenario needs a bounded assertion list")
        for assertion in assertions:
            _identifier(assertion)
        if len(set(assertions)) != len(assertions):
            raise ValueError("duplicate assertion")
        body = {k: v for k, v in scenario.items() if k != "scenario_hash"}
        if scenario.get("scenario_hash") != digest(body):
            raise ValueError("altered scenario")
        goal = scenario.get("goal_id")
        if manifest["mode"] == "live" or goal is not None:
            _identifier(goal)
            if goal in goals:
                raise ValueError("duplicate goal id")
            goals.add(goal)


class EvalRun:
    """One sealed manifest and one immutable verifier result per scenario."""
    def __init__(self, directory):
        self.directory = Path(directory).resolve()
        from core.security.confined_read import read_confined_bytes
        self._manifest = json.loads(read_confined_bytes(
            self.directory / "manifest.json", self.directory, 8 * 1024 * 1024))
        _validate_manifest(self._manifest)
        body = {k: v for k, v in self.manifest.items() if k != "digest"}
        if self.manifest.get("digest") != digest(body) or body.get("version") != 1:
            raise ValueError("invalid or altered evaluation manifest")
        if self.directory.name != self.manifest["run_id"]:
            raise ValueError("manifest does not belong to this run directory")

    @property
    def manifest(self):
        return copy.deepcopy(self._manifest)

    def _check_manifest(self):
        if EvalRun(self.directory).manifest != self._manifest:
            raise ValueError("manifest changed after opening the run")

    def _check_evidence(self, evidence):
        from core.security.confined_read import read_confined_bytes
        if not isinstance(evidence, list) or not 1 <= len(evidence) <= 100:
            raise ValueError("result needs a bounded list of evidence artifacts")
        for item in evidence:
            if not isinstance(item, dict) or set(item) != {"path", "sha256"}:
                raise ValueError("evidence must specify a relative path and sha256")
            path = item["path"]
            if (not isinstance(path, str) or not path or "\\" in path or ":" in path
                    or PurePosixPath(path).is_absolute()
                    or any(p in {"", ".", ".."} for p in path.split("/"))):
                raise ValueError("evidence path must be confined to this run")
            data = read_confined_bytes(self.directory / path, self.directory, MAX_BYTES)
            if hashlib.sha256(data).hexdigest() != item["sha256"]:
                raise ValueError("evidence artifact changed")

    @classmethod
    def create(cls, parent, *, scenarios, configuration, revision, seed=0,
               mode="fixture", budget_usd=0.0):
        if mode not in {"fixture", "live"}:
            raise ValueError("unknown evaluation mode")
        _amount(budget_usd)
        if mode == "fixture" and budget_usd != 0:
            raise ValueError("fixture evaluations cannot have a spend budget")
        if not isinstance(scenarios, list) or not scenarios or not all(isinstance(s, dict) for s in scenarios):
            raise ValueError("an evaluation needs required scenarios")
        ids = [_identifier(item["id"]) for item in scenarios]
        if len(set(ids)) != len(ids):
            raise ValueError("duplicate scenario id")
        records = []
        for item in scenarios:
            assertions = item.get("assertions")
            if not isinstance(assertions, list) or not assertions:
                raise ValueError("each scenario needs unique required verifier assertions")
            for assertion in assertions:
                _identifier(assertion)
            if len(assertions) != len(set(assertions)) or "scenario_hash" in item:
                raise ValueError("duplicate assertions or caller-supplied scenario hash")
            records.append({**item, "scenario_hash": digest(item)})
        run_id = uuid.uuid4().hex
        parent = Path(parent).resolve()
        parent.mkdir(parents=True, exist_ok=True)
        directory = parent / run_id
        directory.mkdir(mode=0o700)
        manifest = {"version": 1, "run_id": run_id, "mode": mode, "seed": seed,
                    "started_at": time.time(), "budget_usd": budget_usd,
                    "configuration": configuration, "revision": revision,
                    "scenarios": records}
        _validate_manifest(manifest)
        _exclusive_json(directory / "manifest.json", {**manifest, "digest": digest(manifest)})
        return cls(directory)

    def record(self, scenario_id, *, assertions, evidence, status="complete", spend_usd=0.0,
               session_id=None, goal_id=None, error=None):
        self._check_manifest()
        if (self.directory / "completion.json").exists():
            raise ValueError("run already finalized")
        _identifier(scenario_id)
        scenario = next((s for s in self.manifest["scenarios"] if s["id"] == scenario_id), None)
        if scenario is None:
            raise ValueError("scenario does not belong to this run")
        if status not in {"complete", "unknown", "error"}:
            raise ValueError("invalid verifier status")
        if not isinstance(assertions, dict) or set(assertions) != set(scenario["assertions"]) or any(type(v) is not bool for v in assertions.values()):
            raise ValueError("all manifest assertions must have explicit boolean results")
        self._check_evidence(evidence)
        _amount(spend_usd)
        if self.manifest["mode"] == "live":
            _identifier(session_id)
        if scenario.get("goal_id") != goal_id:
            raise ValueError("result goal does not match the manifest")
        record = {"run_id": self.manifest["run_id"], "manifest_digest": self.manifest["digest"],
                  "scenario_id": scenario_id, "scenario_hash": scenario["scenario_hash"],
                  "status": status, "assertions": assertions, "evidence": evidence,
                  "session_id": session_id, "goal_id": goal_id, "spend_usd": spend_usd,
                  "error": error, "completed_at": time.time()}
        _exclusive_json(self.directory / f"{scenario_id}.json", {**record, "digest": digest(record)})

    def finalize(self, *, runner_exit_code, revision_after):
        """Persist runner failure/source drift so rescoring cannot erase it."""
        self._check_manifest()
        if type(runner_exit_code) is not int or not isinstance(revision_after, dict):
            raise ValueError("invalid runner completion")
        record = {"run_id": self.manifest["run_id"], "manifest_digest": self.manifest["digest"],
                  "runner_exit_code": runner_exit_code, "revision_after": revision_after}
        _exclusive_json(self.directory / "completion.json", {**record, "digest": digest(record)})

    def score(self):
        from core.security.confined_read import read_confined_bytes
        self._check_manifest()
        runner_error = None
        try:
            completion = json.loads(read_confined_bytes(
                self.directory / "completion.json", self.directory, MAX_BYTES))
            body = {k: v for k, v in completion.items() if k != "digest"}
            if (completion.get("digest") != digest(body)
                    or completion.get("run_id") != self.manifest["run_id"]
                    or completion.get("manifest_digest") != self.manifest["digest"]):
                raise ValueError("invalid runner completion")
            code = completion.get("runner_exit_code")
            if type(code) is not int or code != 0:
                raise ValueError("runner did not exit successfully")
            if completion.get("revision_after") != self.manifest["revision"]:
                raise ValueError("source changed during evaluation")
        except Exception as exc:
            runner_error = str(exc)
        results, total_spend = [], 0.0
        for scenario in self.manifest["scenarios"]:
            outcome, reason = "error", None
            try:
                record = json.loads(read_confined_bytes(
                    self.directory / f"{_identifier(scenario['id'])}.json", self.directory, 2 * 1024 * 1024))
                body = {k: v for k, v in record.items() if k != "digest"}
                if (record.get("digest") != digest(body)
                        or record.get("run_id") != self.manifest["run_id"]
                        or record.get("manifest_digest") != self.manifest["digest"]
                        or record.get("scenario_hash") != scenario["scenario_hash"]
                        or record.get("scenario_id") != scenario["id"]
                        or record.get("goal_id") != scenario.get("goal_id")):
                    raise ValueError("result identity mismatch or altered result")
                assertions = record.get("assertions", {})
                if set(assertions) != set(scenario["assertions"]) or any(type(v) is not bool for v in assertions.values()):
                    raise ValueError("incomplete or malformed verifier assertions")
                spend = record["spend_usd"]
                _amount(spend)
                total_spend += spend
                self._check_evidence(record.get("evidence"))
                if self.manifest["mode"] == "live":
                    _identifier(record.get("session_id"))
                status = record.get("status")
                if status == "complete":
                    outcome = "passed" if all(assertions.values()) else "failed"
                elif status == "unknown":
                    outcome = "unknown"
                else:
                    reason = record.get("error") or "verifier error"
            except FileNotFoundError:
                outcome, reason = "missing", "required scenario has no result"
            except Exception as exc:
                reason = str(exc)
            results.append({"scenario_id": scenario["id"], "outcome": outcome, "reason": reason})
        passed = runner_error is None and all(row["outcome"] == "passed" for row in results)
        within_budget = total_spend <= self.manifest["budget_usd"]
        return {"run_id": self.manifest["run_id"], "manifest_digest": self.manifest["digest"],
                "mode": self.manifest["mode"], "results": results, "spend_usd": total_spend,
                "runner_error": runner_error,
                "within_budget": within_budget, "passed": passed and within_budget,
                "exit_code": 0 if passed and within_budget else 1}
