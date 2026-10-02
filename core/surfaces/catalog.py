"""The surface catalog (064 S2b F1, E-S2-14): ONE row per chat surface.

Before this, one new surface cost ~13 hand edits — a literal surface-id list in
config, owner address, owner admin, the session creator label, the `message`
fan-out order, cron delivery, the owner alias, the forgeable set, the db
manifest, the gateway, the CLI map, the doctor lines and the update guard. They
drifted: owner-admin's summary listed three of the seven surfaces.

Every one of those lists is now DERIVED from :data:`SURFACES`. Adding a surface
is one row here + one ``surfaces/<id>/`` package (with a ``launch.py``, see
``cli/commands/gateway.py``) + its flag row in ``docs/CONFIGURATION.md``.
``tests/unit/core/surfaces/test_surface_catalog.py`` fails on a new literal
surface-id list in any of the touch-point modules.

⚠️ Layering: this module is in ``core/`` and names the surface package and its
CLI command as STRINGS. It never imports ``surfaces/`` or ``cli/``.

**Pack rows (067 P3b).** A FIRST-PARTY pack may contribute a row: it is DATA in
the pack's ``pack.toml`` (``[surfaces.<id>]``), registered by the pack loader's
phase 1 (:func:`register_surface`) — at process entry, before any snapshot of
these lists is taken, and without importing the pack. A third-party pack that
declares a surface is refused (``core.packs.loader``): the security columns
below need core review. A pack row may not set ``alias_owner``, its ``module``
and ``cli_command`` must live in the pack's own package, and once the loader's
phase 2 has left the pack disabled or refused, :func:`surfaces` omits the row
and :func:`withheld_reason` names why. :func:`all_surfaces` keeps it — the
backup set and the server-process guard must still see a disabled surface's
state files and command.

⚠️ ``owner_seat`` is "the owner can be REACHED here" (outbound). Whether an
INBOUND sender on the surface may ever be the owner is ``forgeable`` (never) and
``alias_owner`` (the configured owner id aliases to the owner principal without
a pairing row). Those two are security data — change them only with a spoofed-id
test and an independent review.
"""
import re
import sys
from dataclasses import dataclass, field, replace
from typing import Dict, List, Optional, Tuple


@dataclass(frozen=True)
class SurfaceSpec:
    id: str                      # "discord" — the surface_id on every envelope
    label: str                   # "Discord" — what a person reads
    module: str                  # "surfaces.discord" — imported lazily, never by core
    enabled_flag: str            # "DISCORD_SURFACE_ENABLED"
    owner_env: Optional[str]     # the env var holding the owner's address; None = none
    owner_seat: bool             # the owner can be reached here (owner fan-out, admin)
    forgeable: bool              # the sender address is not platform-authenticated
    transport: str               # "ws" | "webhook" | "longpoll" | "poll" | "sse"
    extra: Optional[str]         # pip extra that carries its deps; None = bare install
    cron_target: bool            # a cron job may deliver its report here
    region: str                  # "global" | "CN" | ...
    max_message_chars: int       # the platform's hard per-message limit
    #: The configured owner id on this surface IS the owner principal, with no
    #: pairing row. Only for a surface whose sender id the platform signs.
    alias_owner: bool = False
    #: cron delivers through its own sink (telegram bot, email tool), not the
    #: outbound router.
    dedicated_sink: bool = False
    #: The ONE credential whose presence doctor cross-checks against the flag
    #: (``polyrob <id>`` runs off it alone). None = flag-only surface.
    token_env: Optional[str] = None
    #: Env vars ``polyrob surfaces add <id>`` asks for, in order (secrets).
    credentials: Tuple[str, ...] = ()
    #: The subset that must be set for the surface to count as configured
    #: (None = all of ``credentials``) …
    required: Optional[Tuple[str, ...]] = None
    #: … or any ONE of these complete alternative sets (X's OAuth 2.0 token,
    #: email's AgentMail key).
    alt_required: Tuple[Tuple[str, ...], ...] = ()
    #: ``module:attr`` of the standalone ``polyrob <id>`` command; None = none.
    cli_command: Optional[str] = None
    #: Sidecar SQLite files the surface opens under the data home (backup set).
    state_dbs: Tuple[str, ...] = ()
    #: The name doctor prints when it differs from the id.
    display: Optional[str] = None
    #: Webhook transport: the pre-auth request-body cap (F4).
    body_cap_bytes: int = 64 * 1024
    #: Bot-to-bot loop guard (F4): at most ``events`` per ``window_s`` per
    #: (bot, bot) pair, then ``cooldown_s`` of silence.
    bot_pair_limit: Tuple[int, int, int] = (20, 60, 60)
    #: Configured = on: ``polyrob gateway`` starts the surface when its required
    #: credentials are set, with no enable flag (the flag stays an explicit
    #: override: ``false`` keeps it off, ``true`` forces it). False for a
    #: surface whose credentials are SHARED with a tool (email: the email tool;
    #: x: the posting tool) or that runs as its own primary process (telegram).
    auto_start: bool = True
    #: Extra facts a surface wants to carry without a new field.
    meta: Dict[str, str] = field(default_factory=dict)
    #: The pack that contributes this row (067 P3b); None = a core row.
    pack: Optional[str] = None

    @property
    def display_name(self) -> str:
        return self.display or self.id

    def missing_credentials(self, env) -> Tuple[str, ...]:
        """The required credentials absent from ``env`` (empty = configured)."""
        def _absent(keys):
            return tuple(k for k in keys if not (env.get(k) or "").strip())
        for alt in self.alt_required:
            if not _absent(alt):
                return ()
        return _absent(self.credentials if self.required is None else self.required)


#: Order is load-bearing: it is the owner fan-out order when ``message`` omits
#: ``surface`` (the owner's primary chat surface first).
SURFACES: Tuple[SurfaceSpec, ...] = (
    SurfaceSpec(
        id="telegram", label="Telegram", module="surfaces.telegram",
        enabled_flag="TELEGRAM_SURFACE_ENABLED", owner_env="POLYROB_OWNER_TELEGRAM_ID",
        owner_seat=True, forgeable=False, transport="longpoll", extra="telegram",
        cron_target=True, region="global", max_message_chars=4096,
        alias_owner=True, dedicated_sink=True, token_env="TELEGRAM_BOT_TOKEN",
        auto_start=False,
        credentials=("TELEGRAM_BOT_TOKEN",),
        cli_command="cli.commands.telegram:telegram", state_dbs=("tg_dedup.db",)),
    SurfaceSpec(
        id="email", label="Email", module="surfaces.email",
        enabled_flag="EMAIL_SURFACE_ENABLED", owner_env="POLYROB_OWNER_EMAIL",
        owner_seat=True, forgeable=True, transport="poll", extra=None,
        cron_target=True, region="global", max_message_chars=1_000_000,
        dedicated_sink=True, auto_start=False,
        credentials=("GMAIL_EMAIL", "GMAIL_APP_PASSWORD"),
        alt_required=(("AGENTMAIL_API_KEY",),),
        cli_command="cli.commands.email:email", state_dbs=("email_dedup.db",)),
    SurfaceSpec(
        id="slack", label="Slack", module="surfaces.slack",
        enabled_flag="SLACK_SURFACE_ENABLED", owner_env="OWNER_SLACK_ID",
        owner_seat=True, forgeable=False, transport="ws", extra=None,
        cron_target=True, region="global", max_message_chars=4000,
        token_env="SLACK_BOT_TOKEN", credentials=("SLACK_BOT_TOKEN", "SLACK_APP_TOKEN"),
        cli_command="cli.commands.slack:slack", state_dbs=("slack_dedup.db",)),
    SurfaceSpec(
        id="discord", label="Discord", module="surfaces.discord",
        enabled_flag="DISCORD_SURFACE_ENABLED", owner_env="OWNER_DISCORD_ID",
        owner_seat=True, forgeable=False, transport="ws", extra=None,
        cron_target=True, region="global", max_message_chars=2000,
        token_env="DISCORD_BOT_TOKEN", credentials=("DISCORD_BOT_TOKEN",),
        cli_command="cli.commands.discord:discord", state_dbs=("discord_dedup.db",)),
    SurfaceSpec(
        id="signal", label="Signal", module="surfaces.signal",
        enabled_flag="SIGNAL_SURFACE_ENABLED", owner_env="OWNER_SIGNAL_ID",
        owner_seat=True, forgeable=False, transport="sse", extra=None,
        cron_target=True, region="global", max_message_chars=2000,
        credentials=("SIGNAL_ACCOUNT", "SIGNAL_DAEMON_URL"),
        required=("SIGNAL_ACCOUNT",),       # the daemon URL has a default
        cli_command="cli.commands.signal:signal", state_dbs=("signal_dedup.db",)),
    SurfaceSpec(
        id="whatsapp", label="WhatsApp", module="surfaces.whatsapp",
        enabled_flag="WHATSAPP_SURFACE_ENABLED", owner_env="OWNER_WHATSAPP_ID",
        owner_seat=True, forgeable=False, transport="webhook", extra=None,
        cron_target=True, region="global", max_message_chars=4096,
        credentials=("WHATSAPP_ACCESS_TOKEN", "WHATSAPP_PHONE_NUMBER_ID",
                     "WHATSAPP_VERIFY_TOKEN", "WHATSAPP_WEBHOOK_SECRET"),
        cli_command="cli.commands.whatsapp:whatsapp",
        state_dbs=("wa_dedup.db", "wa_window.db"),
        # Meta batches many changes into one delivery; its documented payload
        # ceiling is 3 MB. The 64 KB default would refuse a legitimate batch.
        body_cap_bytes=3 * 1024 * 1024),
    # "x" (X DMs): the x pack's pack.toml row (067 P3b).
    SurfaceSpec(
        id="feishu", label="Feishu / Lark", module="surfaces.feishu",
        enabled_flag="FEISHU_SURFACE_ENABLED", owner_env="OWNER_FEISHU_ID",
        owner_seat=True, forgeable=False, transport="ws", extra="feishu",
        # cron delivery is not wired yet (order 0001 is text chat only).
        # The platform refuses a text message over ~150 KB of request body;
        # 10 000 chars keeps the worst case (3-byte CJK, doubly JSON-escaped) far below.
        cron_target=False, region="CN", max_message_chars=10_000,
        credentials=("FEISHU_APP_ID", "FEISHU_APP_SECRET"),
        state_dbs=("feishu_dedup.db",)),
    SurfaceSpec(
        id="dingtalk", label="DingTalk", module="surfaces.dingtalk",
        enabled_flag="DINGTALK_SURFACE_ENABLED", owner_env="OWNER_DINGTALK_ID",
        owner_seat=True, forgeable=False, transport="ws", extra=None,
        # Stream Mode is hand-rolled on aiohttp (JSON frames) — no SDK, no extra.
        # cron delivery is not wired yet (order 0007 is text chat only).
        # DingTalk documents no hard per-message char cap for the robot text
        # message; ~20 KB of body is where sends start to be refused, so 5 000
        # chars (3-byte CJK, JSON-escaped) stays well inside it.
        cron_target=False, region="CN", max_message_chars=5000,
        credentials=("DINGTALK_CLIENT_ID", "DINGTALK_CLIENT_SECRET"),
        state_dbs=("dingtalk_dedup.db",)),
)


# --- pack rows (067 P3b) --------------------------------------------------------

#: Rows first-party packs registered in the loader's phase 1, in pack order.
_PACK_SURFACES: List[SurfaceSpec] = []

_ID_RE = re.compile(r"^[a-z][a-z0-9_]{0,31}$")


def _under(ref: Optional[str], package: str) -> bool:
    mod = (ref or "").partition(":")[0]
    return mod == package or mod.startswith(package + ".")


def validate_surface(spec: SurfaceSpec, *, pack_id: str, package: str) -> None:
    """Raise ``ValueError`` when *spec* may not join the catalog from pack
    *pack_id* (top-level package *package*). Registers nothing."""
    if not _ID_RE.match(spec.id or ""):
        raise ValueError(f"surface id {spec.id!r} must match {_ID_RE.pattern}")
    if spec.id in {s.id for s in all_surfaces()}:
        raise ValueError(f"surface {spec.id!r} is already in the catalog")
    if spec.alias_owner:
        raise ValueError(f"surface {spec.id!r}: a pack surface may not alias the owner "
                         "principal (alias_owner needs a core row and review)")
    if not _under(spec.module, package):
        raise ValueError(f"surface {spec.id!r}: module {spec.module!r} is not in the "
                         f"pack's package {package!r}")
    if spec.cli_command is not None and not _under(spec.cli_command, package):
        raise ValueError(f"surface {spec.id!r}: cli_command {spec.cli_command!r} is not "
                         f"in the pack's package {package!r}")
    if spec.transport not in ("ws", "webhook", "longpoll", "poll", "sse"):
        raise ValueError(f"surface {spec.id!r}: unknown transport {spec.transport!r}")
    if spec.max_message_chars <= 0:
        raise ValueError(f"surface {spec.id!r}: max_message_chars must be positive")
    if spec.owner_seat and not spec.owner_env:
        raise ValueError(f"surface {spec.id!r}: an owner seat needs an owner address env")


def register_surface(spec: SurfaceSpec, *, pack_id: str, package: str) -> SurfaceSpec:
    """Add a first-party pack's row (the loader's phase 1; the tier check is the
    loader's). Returns the stored row (``pack`` set)."""
    validate_surface(spec, pack_id=pack_id, package=package)
    row = replace(spec, pack=pack_id)
    _PACK_SURFACES.append(row)
    return row


def withheld_reason(surface_id: str) -> Optional[str]:
    """Why a pack row is out of :func:`surfaces`, or None (a core row, an
    unknown id, or a pack that loaded — or whose phase 2 has not run yet).

    Reads the loader state only when a loader ran in this process (the signer
    never imports ``core.packs``)."""
    sid = (surface_id or "").strip().lower()
    row = next((s for s in _PACK_SURFACES if s.id == sid), None)
    if row is None:
        return None
    state = sys.modules.get("core.packs.state")
    if state is None or not state.phase_done("packs"):
        return None
    rec = state.record(row.pack)
    if rec is not None and rec.status == state.LOADED:
        return None
    status = rec.status if rec is not None else "not installed"
    why = f" ({rec.reason})" if rec is not None and rec.reason else ""
    return f"provided by pack {row.pack!r}, which is {status}{why}"


def all_surfaces() -> Tuple[SurfaceSpec, ...]:
    """Core rows + every registered pack row, withheld ones included."""
    return tuple(SURFACES) + tuple(_PACK_SURFACES)


def withheld_surfaces() -> Tuple[Tuple[SurfaceSpec, str], ...]:
    """``(row, reason)`` for every pack row that phase 2 left out."""
    out = []
    for s in _PACK_SURFACES:
        why = withheld_reason(s.id)
        if why:
            out.append((s, why))
    return tuple(out)


def reset_pack_surfaces_for_tests() -> None:
    _PACK_SURFACES.clear()


# --- derivations (read at CALL time, so a row added in a test propagates) -----

def surfaces() -> Tuple[SurfaceSpec, ...]:
    """The rows this process can run: core rows + pack rows not withheld."""
    return tuple(SURFACES) + tuple(s for s in _PACK_SURFACES if not withheld_reason(s.id))


def surface_ids() -> Tuple[str, ...]:
    return tuple(s.id for s in surfaces())


def get(surface_id: str) -> Optional[SurfaceSpec]:
    sid = (surface_id or "").strip().lower()
    for s in surfaces():
        if s.id == sid:
            return s
    return None


def owner_seat_ids() -> Tuple[str, ...]:
    """Surfaces that can reach the owner, in fan-out order."""
    return tuple(s.id for s in surfaces() if s.owner_seat)


def owner_chat_ids() -> Tuple[str, ...]:
    """Owner seats whose inbound sender may BE the owner (not forgeable)."""
    return tuple(s.id for s in surfaces() if s.owner_seat and not s.forgeable)


def forgeable_ids() -> frozenset:
    return frozenset(s.id for s in surfaces() if s.forgeable)


def alias_owner_ids() -> frozenset:
    return frozenset(s.id for s in surfaces() if s.alias_owner and not s.forgeable)


def owner_env_by_surface() -> Dict[str, str]:
    return {s.id: s.owner_env for s in surfaces() if s.owner_seat and s.owner_env}


def cron_target_ids() -> Tuple[str, ...]:
    return tuple(s.id for s in surfaces() if s.cron_target)


def router_target_ids() -> Tuple[str, ...]:
    """Cron targets delivered through the outbound router (no dedicated sink)."""
    return tuple(s.id for s in surfaces() if s.cron_target and not s.dedicated_sink)


def cli_commands(*, core_only: bool = False) -> Dict[str, str]:
    """``{command name: "module:attr"}`` for every surface with a standalone command.

    Includes withheld pack rows (the server-process guard must still see a
    running ``polyrob <id>``). ``core_only`` = only core rows: a pack row's
    command is a pack CLI command (``[cli] commands``, ``cli/commands/pack.py``),
    served through the pack's own gating, never imported by the root group."""
    rows = SURFACES if core_only else all_surfaces()
    return {s.id: s.cli_command for s in rows if s.cli_command}


def state_dbs() -> Tuple[str, ...]:
    """Every row's state files, withheld pack rows included (the backup set)."""
    out = []
    for s in all_surfaces():
        for name in s.state_dbs:
            if name not in out:
                out.append(name)
    return tuple(out)
