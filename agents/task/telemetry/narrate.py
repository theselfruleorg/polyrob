"""A pure action → one human line narrator (043 A14).

Turns ONE tool-execution feed event into ONE human sentence for the running
transcript — ``Read 4 files in ~/price-watch``, ``Ran the test suite and all 18
passed``, ``Building the container``. It reads the fields the feed already
carries (``tool_name`` / ``action_name`` / ``render`` / ``result_preview`` /
``success``) and returns a finished string.

The phrasing is NOT here: every word comes from :mod:`core.copy` under the
``chat.act.*`` keys, so the console (JS), the CLI transcript and this function
say the same thing, and a translator changes one file. This module only chooses
the key and fills the numbers.

Three hard rules, from ``docs/design/040/web/chat-running.html``:

* One line. No ``[step]`` / ``[event]`` / ``[iter]`` trace token, ever.
* No ``repr()`` of the event, and no raw machine name (a tool id is a machine
  name) — an unknown tool degrades to a plain, name-free line.
* Newest last; the running/done distinction is the ``success`` field, and the
  CSS class (``.act.is-running``) is the caller's job, not this one's.

Layering: core-safe. It imports :mod:`core.copy` and nothing from the agents,
tools, webview or surfaces tiers, so any of them can call ``narrate`` on the
event it already has. It lives in the agents tree only because the feed event
shape does.
"""
from __future__ import annotations

from typing import Any, Optional

from core.copy import t

__all__ = ["narrate"]

# The formatter's default for an unset name; treat it as no name at all.
_UNKNOWN = "unknown"
# A path/place longer than this, or with a newline, is not fit for one line.
_PLACE_MAX = 120


def narrate(event: Any) -> str:
    """The one-line narration of *event*. Always a non-empty human string."""
    if not isinstance(event, dict):
        return t("chat.act.did_work")

    tool = _name(_pick(event, "tool_name"))
    action = _name(_pick(event, "action_name"))
    success = _pick(event, "success")

    if success is False:
        # Never surface the raw error string here — it can carry a repr.
        return t("chat.act.failed")

    running = success is None
    line = _known(tool, action, running, event)
    if line is not None:
        return line
    return t("chat.act.working") if running else t("chat.act.did_work")


# --------------------------------------------------------------------- routing


def _known(tool: str, action: str, running: bool, event: dict) -> Optional[str]:
    """A line for a tool action this narrator knows, or ``None`` to fall back."""
    if action == "read_file" or (tool == "filesystem" and action in ("read", "read_files")):
        return _files_read(event)
    if action == "write_file" or (tool == "filesystem" and action == "write"):
        return _file_wrote(event)
    if action == "run_tests":
        return _tests(event)
    if action == "run_code":
        return t("chat.act.code_running") if running else t("chat.act.code_done")
    if action == "deploy" and tool == "app_service":
        return t("chat.act.deploy_running") if running else t("chat.act.deploy_done")
    return None


def _files_read(event: dict) -> str:
    count = _int(_pick(event, "count"))
    if count is None:
        count = 1  # a bare read_file event is one file
    where = _place(_pick(event, "where") or _dir_of(_pick(event, "path")))
    if count == 1:
        return t("chat.act.file_read_one", where=where) if where else t("chat.act.file_read_one_nodir")
    if where:
        return t("chat.act.files_read", count=count, where=where)
    return t("chat.act.files_read_nodir", count=count)


def _file_wrote(event: dict) -> str:
    name = _place(_base(_pick(event, "path")))
    return t("chat.act.file_wrote", name=name) if name else t("chat.act.file_wrote_plain")


def _tests(event: dict) -> str:
    passed = _int(_pick(event, "passed"))
    failed = _int(_pick(event, "failed"))
    if passed is None and failed is None:
        return t("chat.act.tests_ran")
    if not failed:  # 0 or None -> a clean run
        if passed is None:
            return t("chat.act.tests_ran")
        return t("chat.act.tests_all_passed", passed=passed)
    return t("chat.act.tests_mixed", passed=passed or 0, failed=failed)


# ----------------------------------------------------------------- field reads


def _render(event: dict) -> dict:
    """The A16 ``{kind, payload}`` block, wherever it rides."""
    r = event.get("render")
    if isinstance(r, dict):
        return r
    data = event.get("data")
    if isinstance(data, dict) and isinstance(data.get("render"), dict):
        return data["render"]
    return {}


def _pick(event: dict, key: str, default: Any = None) -> Any:
    """*key* from the richest source that has it: the render payload first
    (the structured facts A16 emits), then the event, its data envelope and its
    parameters. Works whether the caller passes the event envelope or the flat
    ``data`` dict."""
    render = _render(event)
    payload = render.get("payload") if isinstance(render.get("payload"), dict) else None
    data = event.get("data") if isinstance(event.get("data"), dict) else None
    params = event.get("parameters")
    if not isinstance(params, dict) and data is not None:
        params = data.get("parameters")
    if not isinstance(params, dict):
        params = None
    for src in (payload, render, event, data, params):
        if isinstance(src, dict) and src.get(key) is not None:
            return src[key]
    return default


def _name(v: Any) -> str:
    """A tool/action name lowered for matching; the formatter's ``Unknown``
    default and any non-string become the empty name."""
    if not isinstance(v, str):
        return ""
    s = v.strip().lower()
    return "" if s in ("", _UNKNOWN) else s


def _int(v: Any) -> Optional[int]:
    if isinstance(v, bool):
        return None
    if isinstance(v, int):
        return v
    if isinstance(v, float):
        return int(v)
    if isinstance(v, str):
        s = v.strip()
        if s.lstrip("-").isdigit():
            try:
                return int(s)
            except ValueError:
                return None
    return None


def _place(v: Any) -> str:
    """A path/place safe to drop into one line, or ``""`` if it is not."""
    if not isinstance(v, str):
        return ""
    s = v.strip()
    if not s or "\n" in s or "\r" in s or len(s) > _PLACE_MAX:
        return ""
    return s


def _dir_of(path: Any) -> str:
    if not isinstance(path, str):
        return ""
    s = path.strip()
    if "/" not in s:
        return ""
    return s.rsplit("/", 1)[0]


def _base(path: Any) -> str:
    if not isinstance(path, str):
        return ""
    return path.strip().rstrip("/").rsplit("/", 1)[-1]
