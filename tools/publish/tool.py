"""``publish`` — give a built page a real, stable URL.

The gap this closes: Rob could BUILD a page or an API and had no verb that put
it at an address. Every "ship" goal therefore terminated in a request to the
owner. "Ship x402 ecosystem map: static HTML visualization" was recorded done
with a file nobody could open; "Owner deploy package: mainnet-ready x402
endpoint for api.theselfrule.org" blocked outright and stayed blocked.

Shape, deliberately the same as ``hf_deploy``: a FIRST publish of a NEW public
address needs an approving provider (interactive by default, the durable
owner-queue under autonomous mode, denied when neither can reach a human); an
already-approved slug then iterates unattended. The owner grants an ADDRESS
once, not every write — a review gate, not a treadmill.

Hard lines:
  * a leaf/sub-agent or a forged (self_wake / delegation_result) turn can never
    publish — putting something at a public URL is an owner-tenant act;
  * the slug is validated before it becomes a path or a URL (``core.publish``);
  * sources are confined to the session workspace and credential files are
    refused, so a publish can never carry a secret to a public address.
"""
from typing import Any, List, Optional

from pydantic import BaseModel, Field

from tools.base_tool import BaseTool
from tools.controller.types import ActionResult


class PublishParams(BaseModel):
    slug: str = Field(..., description="Public address segment: lowercase letters, digits "
                                       "and single hyphens (e.g. 'rob-status'). The page "
                                       "is served at <base>/<slug>/.")
    files: List[str] = Field(..., description="Workspace-relative (or absolute in-workspace) "
                                              "files to publish. Include an index.html for a "
                                              "page. Existing files at this slug are replaced.")


class UnpublishParams(BaseModel):
    slug: str = Field(..., description="Slug of a publication to take down (files removed).")


class PublishListParams(BaseModel):
    pass


def _deny_reason(execution_context: Any) -> Optional[str]:
    """Non-None -> refuse, with this message.

    Publishing needs no HOST capability, so it does not require a compute
    posture — but it is emphatically not something a background re-entry may do.
    ``compute_posture_allows(ctx, 0)`` cannot express that: level 0 is an
    unconditional pass by contract ("posture-0 capabilities keep their own
    gates"), so this states the three clauses it would otherwise borrow —
    owner tenant, not a leaf/sub-agent, not a forged turn — directly.

    The forged-turn kinds come from the same SSOT the posture gate uses
    (``core.security.forged_turns``), so a new forged kind is denied here too
    without a second edit. Fail-CLOSED: any fault in resolution denies.
    """
    if execution_context is None:
        return "publish requires an execution context"
    try:
        if getattr(execution_context, "is_sub_agent", False):
            return "publish denied: a delegated sub-agent never chooses what the world sees"
        if getattr(execution_context, "role", "leaf") != "orchestrator":
            return "publish denied: a leaf agent never chooses what the world sees"
        metadata = getattr(execution_context, "metadata", None) or {}
        from core.security.forged_turns import FORGED_TURN_KINDS
        if metadata.get("turn_kind") in FORGED_TURN_KINDS:
            return ("publish denied: a self-wake / delegation-result re-entry is not "
                    "an owner asking to ship")
        from core.config_policy import local_mode_enabled
        from core.instance import is_owner_local_safe, resolve_owner_principal
        if not is_owner_local_safe(
                getattr(execution_context, "user_id", None),
                owner_principal=resolve_owner_principal(),
                local_enabled=local_mode_enabled()):
            return "publish denied: only the owner tenant may put something at a public URL"
    except Exception:
        return "publish denied: capability gate unavailable"
    return None


class PublishTool(BaseTool):
    """Publish workspace files to a stable public URL, behind one owner decision."""

    def __init__(self, name: str = "publish", config=None, container=None):
        super().__init__(name=name, config=config, container=container)
        self._store = None
        self._approval_provider = None
        self._workspace_override = None

    # --- collaborators (overridable in tests) ----------------------------

    def _get_store(self):
        if self._store is None:
            from core.publish import get_publish_store
            self._store = get_publish_store()
        return self._store

    def _get_approval_provider(self):
        """The approver that gates a FIRST publish of an unknown slug.

        Mirrors ``hf_deploy``: resolve the provider the Controller uses, and
        remap ``auto_notify`` to the durable ``owner_queue``. ``auto_notify``
        always returns True, so honoring it would silently put a brand-new public
        address live from an unattended run — inverting the whole point of the
        gate. ``owner_queue`` still lets a real owner approve out-of-band (e.g.
        Telegram ``/approve``) without ever rubber-stamping.
        """
        if self._approval_provider is None:
            try:
                import tools.controller.approval_interactive  # noqa: F401
            except Exception:
                pass
            from tools.controller.approval import (
                get_approval_provider_or_deny, resolve_gated_actions,
            )
            _required, provider_name = resolve_gated_actions()
            if provider_name == "auto_notify":
                try:
                    import tools.controller.approval_queue  # noqa: F401
                except Exception:
                    pass
                provider_name = "owner_queue"
            self._approval_provider = get_approval_provider_or_deny(provider_name)
        return self._approval_provider

    def _workspace_root(self, execution_context: Any) -> Optional[str]:
        """The directory a publish may copy from. NEVER falls back to cwd — on a
        shared project-root workspace that would be the whole install tree."""
        if self._workspace_override:
            return self._workspace_override
        ws = getattr(execution_context, "workspace_dir", None)
        if ws:
            return str(ws)
        try:
            from agents.task.path import pm
            return str(pm().get_workspace_dir(
                getattr(execution_context, "session_id", "") or "",
                getattr(execution_context, "user_id", None)))
        except Exception:
            return None

    # --- actions ---------------------------------------------------------

    @BaseTool.action(
        "Publish workspace files to a stable public URL. A NEW slug needs owner "
        "approval once; after that the same slug can be updated freely. Returns "
        "the URL. Include an index.html to publish a page.",
        param_model=PublishParams,
    )
    async def publish(self, params: PublishParams, execution_context=None) -> ActionResult:
        deny = _deny_reason(execution_context)
        if deny:
            return ActionResult(error=deny)

        user_id = str(getattr(execution_context, "user_id", "") or "")
        if not user_id:
            return ActionResult(error="publish denied: no tenant on the execution context")

        store = self._get_store()
        from core.publish import valid_slug
        if not valid_slug(params.slug):
            return ActionResult(error=(
                f"invalid slug {params.slug!r}: use lowercase letters, digits and single "
                f"hyphens (e.g. 'rob-status'), 48 chars max"))

        workspace = self._workspace_root(execution_context)
        if not workspace:
            return ActionResult(error="publish denied: cannot resolve the session workspace")

        sources = [
            f if str(f).startswith("/") else f"{workspace.rstrip('/')}/{f}"
            for f in (params.files or [])
        ]
        if not sources:
            return ActionResult(error="publish: no files given")

        existing = store.get(params.slug)
        if existing is not None and existing.user_id != user_id:
            return ActionResult(error=f"slug {params.slug!r} belongs to another tenant")

        # A slug already granted an address iterates without asking again.
        needs_approval = existing is None or existing.status != "live"
        if needs_approval:
            approver = self._get_approval_provider()
            try:
                approved = await approver.request(
                    "publish", {"slug": params.slug, "files": params.files},
                    execution_context)
            except Exception as e:
                return ActionResult(error=f"publish: approval failed ({str(e)[:120]})")
            if not approved:
                return ActionResult(error=(
                    f"publish refused: owner approval is required before {params.slug!r} "
                    f"becomes a public address"))

        try:
            store.stage(user_id, params.slug, sources, confine_to=workspace)
        except (ValueError, PermissionError) as e:
            return ActionResult(error=f"publish refused: {e}")
        except OSError as e:
            return ActionResult(error=f"publish failed: {str(e)[:160]}")

        if needs_approval:
            store.approve(user_id, params.slug)

        pub = store.get(params.slug)
        url = pub.url if pub else store.url_for(params.slug)
        self._link_artifacts(user_id, sources, url)
        return ActionResult(
            extracted_content=(f"Published {pub.file_count} file(s) to {url} "
                               f"({pub.bytes} bytes)."),
            include_in_memory=True,
            metadata={"slug": params.slug, "url": url, "status": pub.status},
        )

    @BaseTool.action("List this tenant's publications with their URLs and status.",
                     param_model=PublishListParams)
    async def publish_list(self, params: Any = None, execution_context=None) -> ActionResult:
        user_id = str(getattr(execution_context, "user_id", "") or "")
        if not user_id:
            return ActionResult(error="publish_list denied: no tenant on the execution context")
        pubs = self._get_store().list_for(user_id)
        if not pubs:
            return ActionResult(extracted_content="No publications yet.")
        lines = [f"- {p.slug} [{p.status}] {p.url} ({p.file_count} file(s), {p.bytes} bytes)"
                 for p in pubs]
        return ActionResult(extracted_content="\n".join(lines), include_in_memory=True)

    @BaseTool.action("Take a publication down and delete its served files.",
                     param_model=UnpublishParams)
    async def unpublish(self, params: UnpublishParams, execution_context=None) -> ActionResult:
        deny = _deny_reason(execution_context)
        if deny:
            return ActionResult(error=deny)
        user_id = str(getattr(execution_context, "user_id", "") or "")
        if not user_id:
            return ActionResult(error="unpublish denied: no tenant on the execution context")
        ok = self._get_store().unpublish(user_id, params.slug)
        if not ok:
            return ActionResult(error=f"unpublish: no publication {params.slug!r} for this tenant")
        return ActionResult(extracted_content=f"Unpublished {params.slug}.",
                            include_in_memory=True)

    # --- internals -------------------------------------------------------

    @staticmethod
    def _link_artifacts(user_id: str, sources: List[str], url: str) -> None:
        """Stamp the published URL onto the artifact rows for these files.

        That is what makes `url` on an artifact mean something: the deliverables
        block can then hand the owner a link instead of a bare filename — the
        2026-07-19 usability failure mode. Fail-open bookkeeping.
        """
        try:
            import os

            from core.artifacts import get_artifact_ledger
            ledger = get_artifact_ledger()
            for src in sources:
                row = ledger._row_by_path(user_id, os.path.realpath(src))
                if row:
                    ledger.set_url(row["id"], user_id, url)
        except Exception:
            pass
