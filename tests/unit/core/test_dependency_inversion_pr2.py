"""PR 2 dependency-inversion locks.

These tests pin the boundaries the core/server split depends on so they
don't silently regress as the codebase evolves.
"""

import importlib
import inspect

import pytest


# --- Cut 1: auth constants live in core, api/ is a shim ---------------------


def test_core_constants_has_canonical_definitions():
    from core import constants

    # 'owner' = the password-gated instance operator (admin-equivalent for reads,
    # not admin-assignable — see core/constants.py). It's a canonical role.
    assert constants.VALID_ROLES == ['user', 'admin', 'owner']
    assert 'admin' in constants.ADMIN_ROLES
    assert 'owner' in constants.ADMIN_ROLES
    assert 'owner' not in constants.ASSIGNABLE_ROLES  # never admin-grantable
    assert {'free', 'free_access', 'holder', 'x402', 'admin'} == set(constants.VALID_TIERS)
    assert callable(constants.is_admin)
    assert callable(constants.validate_role)


def test_api_auth_constants_is_a_shim():
    """api.auth_constants must re-export the *same object identities* as core."""
    from core import constants as core_const
    from api import auth_constants as api_const

    for name in [
        'VALID_ROLES',
        'ADMIN_ROLES',
        'VALID_TIERS',
        'FULL_ACCESS_TIERS',
        'is_admin',
        'is_admin_role',
        'validate_role',
        'validate_tier',
    ]:
        assert getattr(api_const, name) is getattr(core_const, name), (
            f"api.auth_constants.{name} must be the same object as core.constants.{name}"
        )


# --- Cut 2: domain exceptions in core, modules/auth is fastapi-free ----------


def test_core_exceptions_defines_auth_and_billing_exceptions():
    from core.exceptions import (
        AuthError,
        BotError,
        InsufficientCreditsError,
        TierError,
        UserNotFoundError,
    )

    assert issubclass(AuthError, BotError)
    assert issubclass(TierError, AuthError)
    assert issubclass(UserNotFoundError, AuthError)
    assert issubclass(InsufficientCreditsError, BotError)

    assert TierError.status_code == 403
    assert UserNotFoundError.status_code == 404
    assert InsufficientCreditsError.status_code == 402


def test_insufficient_credits_signature_preserved():
    """Existing agent code constructs this with user_id/required/available."""
    from core.exceptions import InsufficientCreditsError

    e = InsufficientCreditsError(user_id='u1', required=100, available=25)
    assert e.user_id == 'u1'
    assert e.required == 100
    assert e.available == 25
    assert '100' in str(e) and '25' in str(e)


def test_usage_tracker_reexports_same_exception_object():
    """Backward compat: legacy import path must yield the same class."""
    from core.exceptions import InsufficientCreditsError as IceCore
    from modules.credits.usage_tracker import InsufficientCreditsError as IceLegacy

    assert IceLegacy is IceCore


def test_modules_auth_files_have_no_fastapi_imports():
    """modules/auth/{tier_manager,api_key_manager} must not import fastapi.

    Inspects source text rather than module namespace so the check survives
    even if a transitive import happens to load fastapi.
    """
    for path in [
        'modules/auth/tier_manager.py',
        'modules/auth/api_key_manager.py',
    ]:
        with open(path) as f:
            src = f.read()
        assert 'fastapi' not in src.lower(), f"{path} still references fastapi"
        assert 'HTTPException' not in src, f"{path} still references HTTPException"


# --- Cut 3+4: orchestrator decoupled from webview transport -----------------


def test_orchestrator_does_not_import_httpx_module_level():
    """Orchestrator must not pull in httpx at import time (core-mode rule)."""
    with open('agents/task/agent/orchestrator.py') as f:
        src = f.read()
    # `import httpx` at module level would couple core to a transport.
    # The streaming callback factory now lives in agents/task/utils_webview.py.
    assert 'import httpx' not in src, (
        "orchestrator.py must not import httpx; the transport belongs in the "
        "server-side stream callback factory"
    )


def test_orchestrator_accepts_on_stream_chunk_kwarg():
    from agents.task.agent.orchestrator import SessionOrchestrator

    sig = inspect.signature(SessionOrchestrator.__init__)
    assert 'on_stream_chunk' in sig.parameters
    assert sig.parameters['on_stream_chunk'].default is None


def test_stream_chunk_callback_type_alias_exists():
    from agents.task.agent import orchestrator

    assert hasattr(orchestrator, 'StreamChunkCallback')


def test_webview_stream_callback_factory_returns_callable():
    """Server-side factory must produce an async (session, agent, chunk, step) callable."""
    from agents.task.utils_webview import make_webview_stream_callback

    cb = make_webview_stream_callback()
    assert callable(cb)
    sig = inspect.signature(cb)
    # 4 positional args: session_id, agent_id, chunk, step
    assert list(sig.parameters.keys()) == ['session_id', 'agent_id', 'chunk', 'step']
