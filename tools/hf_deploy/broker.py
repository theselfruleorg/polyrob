"""``HFSpacesBroker`` — the ONLY Hugging Face-specific surface (proposal §3.3).

Token custody: ``HF_TOKEN`` is read (and stripped) at call time by
``resolve_token()`` and flows ONLY into the injected ``api_factory`` (or the
lazily-imported ``huggingface_hub.HfApi``) — it is never a parameter, a
result, a log line, or an error message. Any exception raised by the
underlying client is caught and re-raised as a ``BrokerError`` with the token
string scrubbed from the message.

``huggingface_hub`` is an OPTIONAL dependency: it is imported lazily, only
when ``api_factory`` is not injected, and only inside ``deploy_space``/
``delete_space``. Its absence surfaces as a clear ``BrokerError``, not an
ImportError at module load — so this module (and the tool built on it) is
importable in an environment that never installed it.
"""
import asyncio
import logging
import os
import re
import time
from typing import Any, Callable, Dict, Optional

logger = logging.getLogger(__name__)

# HF Spaces runtime "stage" values that mean the build/run is terminally dead —
# waiting longer would never reach RUNNING. Not exhaustive of every HF stage
# name; deploy_space also has its own wall-clock timeout as a backstop for any
# non-terminal stage that just never arrives.
_BAD_TERMINAL_STAGES = frozenset({
    "BUILD_ERROR", "RUNTIME_ERROR", "CONFIG_ERROR", "NO_APP_FILE",
    "STOPPED", "DELETED", "PAUSED",
})

_DEFAULT_RUNTIME_WAIT_SEC = 300.0
_DEFAULT_POLL_INTERVAL_SEC = 5.0


class BrokerError(Exception):
    """Raised for any HF Spaces broker failure. Message is always token-scrubbed."""


def _scrub(message: str, token: Optional[str],
           secret_values: Optional[Any] = None) -> str:
    """Redact the HF token AND any declared Space-secret values from a message
    before it becomes a ``BrokerError`` (which flows into ``ActionResult.error``
    → agent memory). A raising ``add_space_secret`` must never echo the secret
    value it was setting."""
    if token:
        message = message.replace(token, "<redacted>")
    for value in (secret_values or []):
        if value and isinstance(value, str):
            message = message.replace(value, "<redacted>")
    return message


#: Text-file size cap for the content scan — skip anything larger (likely binary
#: or a data blob; the credential-NAME check still applies to it).
_SCAN_MAX_BYTES = 512 * 1024


def scan_workspace_for_secrets(workspace_dir: str, *, limit: int = 20) -> list:
    """Return a list of workspace-relative paths that look like they contain or
    ARE secrets (P1 finalization).

    ``deploy_space`` publishes the ENTIRE workspace to a PUBLIC Space, so it must
    refuse when the workspace carries a credential file (``.env`` etc.) or a file
    whose text content contains a credential shape — unlike the sibling ``coding``/
    ``self_env`` tools that touch the same directory, the old broker uploaded it
    blind. Returns at most ``limit`` offenders (paths only, never values).
    """
    import os as _os
    from pathlib import Path as _Path
    try:
        from core.security.secret_guard import is_credential_file
    except Exception:
        is_credential_file = None
    try:
        from core.secret_scrub import scrub_secret_shapes
    except Exception:
        scrub_secret_shapes = None

    root = _Path(workspace_dir)
    offenders: list = []
    if not root.exists():
        return offenders
    for dirpath, dirnames, filenames in _os.walk(root):
        # Never descend into VCS/metadata dirs.
        dirnames[:] = [d for d in dirnames if d not in (".git", ".hg", ".svn")]
        for fname in filenames:
            fpath = _Path(dirpath) / fname
            rel = str(fpath.relative_to(root))
            # 1. Credential-shaped filename (.env, *credential*, id_rsa, …).
            if is_credential_file is not None and is_credential_file(fpath):
                offenders.append(rel)
            elif scrub_secret_shapes is not None:
                # 2. Credential shape inside a text file's content.
                try:
                    if fpath.stat().st_size > _SCAN_MAX_BYTES:
                        continue
                    text = fpath.read_text(encoding="utf-8", errors="strict")
                except (OSError, UnicodeDecodeError):
                    continue  # unreadable / binary → skip content scan
                if scrub_secret_shapes(text) != text:
                    offenders.append(rel)
            if len(offenders) >= limit:
                return offenders
    return offenders


def default_http_get(url: str, timeout: float):
    """Real (non-test) HTTP GET used when no ``http_get`` is injected —
    returns an int status code. Best-effort: an unreachable host returns a
    synthetic non-2xx code rather than raising, so ``health_check`` degrades
    to "unhealthy" instead of throwing on a broker used without DI. Exported
    so ``reconcile.py``'s boot sweep can reuse the same default getter."""
    import urllib.error
    import urllib.request

    req = urllib.request.Request(url, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return getattr(resp, "status", 200)
    except urllib.error.HTTPError as e:
        return e.code
    except Exception as e:
        logger.debug("hf_deploy default http_get failed for %s: %s", url, e)
        return 599


class HFSpacesBroker:
    """Thin async wrapper over ``huggingface_hub.HfApi`` for Space lifecycle ops.

    ``api_factory(token) -> api`` is injectable (tests use a fake); when
    ``None`` the real ``HfApi`` is lazily imported. ``http_get(url, timeout)``
    is injectable for ``health_check`` (may be sync or return an awaitable —
    both are handled); when ``None`` a real (non-test) HTTP GET is used.
    """

    def __init__(self, api_factory: Optional[Callable[[str], Any]] = None,
                http_get: Optional[Callable] = None,
                runtime_wait_sec: float = _DEFAULT_RUNTIME_WAIT_SEC,
                poll_interval_sec: float = _DEFAULT_POLL_INTERVAL_SEC):
        self._api_factory = api_factory
        self._http_get = http_get or default_http_get
        self._runtime_wait_sec = runtime_wait_sec
        self._poll_interval_sec = poll_interval_sec

    @staticmethod
    def resolve_token() -> Optional[str]:
        """Read+strip ``HF_TOKEN`` at CALL time (never cached). ``None`` when unset/blank."""
        raw = os.environ.get("HF_TOKEN")
        if raw is None:
            return None
        raw = raw.strip()
        return raw or None

    @staticmethod
    def space_url(space_repo: str) -> str:
        """``"Org/Name"`` -> ``https://<slug-org>-<slug-name>.hf.space``
        (lowercase; ``_``/``.`` -> ``-``)."""
        owner, _, name = space_repo.partition("/")

        def _slug(part: str) -> str:
            return re.sub(r"[_.]", "-", part.strip().lower())

        return f"https://{_slug(owner)}-{_slug(name)}.hf.space"

    def _make_api(self, token: str):
        if self._api_factory is not None:
            return self._api_factory(token)
        try:
            from huggingface_hub import HfApi
        except ImportError as e:
            raise BrokerError(
                f"huggingface_hub is required for HF Spaces deploy but is not installed: {e}"
            ) from None
        return HfApi(token=token)

    async def _wait_for_running(self, api: Any, space_repo: str) -> None:
        deadline = time.monotonic() + self._runtime_wait_sec
        while True:
            runtime = api.get_space_runtime(space_repo)
            stage = getattr(runtime, "stage", None)
            if stage == "RUNNING":
                return
            if stage in _BAD_TERMINAL_STAGES:
                raise BrokerError(f"space build failed for '{space_repo}': stage={stage}")
            if time.monotonic() >= deadline:
                raise BrokerError(
                    f"space '{space_repo}' did not reach RUNNING within "
                    f"{self._runtime_wait_sec}s (last stage={stage})"
                )
            await asyncio.sleep(self._poll_interval_sec)

    async def deploy_space(self, *, space_repo: str, workspace_dir: str,
                           secrets: Optional[Dict[str, str]] = None) -> str:
        """Create/update the Space, upload the workspace, set any declared
        secrets, then poll until RUNNING. Returns the public Space URL."""
        token = self.resolve_token()
        if not token:
            raise BrokerError("HF_TOKEN is not set")
        # SECURITY (P1 finalization): the workspace is published to a PUBLIC Space,
        # so refuse if it carries a credential file or a file with a credential
        # shape — BEFORE any network call. Names only in the error (never values).
        offenders = scan_workspace_for_secrets(workspace_dir)
        if offenders:
            raise BrokerError(
                "refusing to publish: workspace contains likely secrets in "
                f"{len(offenders)} file(s): {', '.join(offenders[:20])}. "
                "Remove them or pass real secrets via the `secrets` arg (set as "
                "Space secrets, never uploaded)."
            )
        secret_values = list((secrets or {}).values())
        try:
            # _make_api is inside the scrub try/except: a token-bearing exception
            # from HfApi construction must not propagate unscrubbed either.
            api = self._make_api(token)
            api.create_repo(repo_id=space_repo, repo_type="space",
                            space_sdk="docker", exist_ok=True)
            api.upload_folder(repo_id=space_repo, repo_type="space",
                              folder_path=workspace_dir)
            for key, value in (secrets or {}).items():
                api.add_space_secret(repo_id=space_repo, key=key, value=value)
            await self._wait_for_running(api, space_repo)
        except BrokerError:
            raise
        except Exception as e:
            raise BrokerError(_scrub(str(e), token, secret_values)) from None
        return self.space_url(space_repo)

    async def health_check(self, url: str, timeout: float = 10.0) -> bool:
        """2xx -> True, anything else (or a raise) -> propagate/False per caller."""
        result = self._http_get(url, timeout)
        if asyncio.iscoroutine(result):
            result = await result
        return 200 <= int(result) < 300

    async def delete_space(self, *, space_repo: str) -> None:
        token = self.resolve_token()
        if not token:
            raise BrokerError("HF_TOKEN is not set")
        try:
            # _make_api inside the scrub guard (parity with deploy_space).
            api = self._make_api(token)
            api.delete_repo(repo_id=space_repo, repo_type="space")
        except BrokerError:
            raise
        except Exception as e:
            raise BrokerError(_scrub(str(e), token)) from None


__all__ = ["HFSpacesBroker", "BrokerError", "default_http_get"]
