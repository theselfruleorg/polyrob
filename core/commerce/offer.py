"""The offer manifest (099 §3.1) — validation only, no I/O.

An offer is DATA the agent authors: what it sells, at what price, which inputs a
buyer gives, what it delivers, how one order is fulfilled. :func:`validate`
turns an untrusted dict (the model wrote it) into a frozen
:class:`OfferManifest` or raises :class:`OfferError` with one sentence a model
can act on. Nothing here names a product type.

Roles bind buyer data to authority:

* ``recipient`` — the ONLY address a fulfillment run may send value or an asset
  to (the money kernel's offer step reads it);
* ``refund``    — the ONLY refund target (never ``Transfer.from``, 099 F3);
* ``untrusted_text`` — always wrapped as DATA in the fulfillment task.

v1 limits (stated, not silent): ``pricing.kind`` is ``fixed`` or ``quote``
(``band`` and ``recurring`` refuse — 087 subscriptions are deleted by U26 and
nothing renews an order yet); a refund is always the owner's (U40).

Tier-0: stdlib only.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Any, Dict, Mapping, Optional, Tuple

SPEC = "polyrob.offer/1"

ID_RE = re.compile(r"^[a-z][a-z0-9-]{1,40}$")
SKILL_RE = re.compile(r"^[a-z][a-z0-9-]*$")
TOOL_RE = re.compile(r"^[a-z][a-z0-9_]{1,63}$")
EVM_RE = re.compile(r"^0x[0-9a-fA-F]{40}$")
FIELD_RE = re.compile(r"^[a-z][a-z0-9_]{0,31}$")

PRICING_KINDS = ("fixed", "quote")
PRICING_LATER = ("band", "recurring")
RAIL_MODES = ("transfer", "x402_exact")
DELIVERABLE_KINDS = ("onchain_asset", "onchain_tx", "file", "message", "link", "access",
                     "service")
PROOF_KINDS = ("tx_receipt", "file_hash", "url", "none")
ROLES = ("recipient", "refund", "untrusted_text")
INPUT_TYPES = ("string", "integer", "number")
INPUT_FORMATS = ("evm_address", "email", "url")
REFUND_TERMS = ("never", "on_failure")
MACHINE_LISTINGS = ("a2a", "mcp", "http402")
MARKET_RE = re.compile(r"^market:[a-z][a-z0-9-]{1,30}$")

#: Sanity ceilings: a manifest is untrusted model output.
MAX_PRICE_USD = Decimal("10000")
MAX_ORDER_SPEND_USD = 100.0
MAX_ORDER_LLM_USD = 20.0
MAX_ATTEMPTS = 5
MAX_DEADLINE_MIN = 24 * 60
MAX_INPUTS = 12
MAX_TOOLS = 12


def native_surfaces() -> Tuple[str, ...]:
    """The chat surfaces a ``native:<surface>`` listing may name: every catalog surface
    whose sender is NOT forgeable. Email is forgeable, so it is never a buying channel
    (owner, 2026-11-03); the owner picks which of these an offer lists, on its live card."""
    from core.surfaces import catalog
    forgeable = catalog.forgeable_ids()
    return tuple(s for s in catalog.surface_ids() if s not in forgeable)


class OfferError(ValueError):
    """The manifest breaks a rule. The message names the field."""


@dataclass(frozen=True)
class InputSpec:
    name: str
    type: str = "string"
    required: bool = True
    role: Optional[str] = None
    format: Optional[str] = None
    max_length: int = 280
    description: str = ""


@dataclass(frozen=True)
class OfferManifest:
    id: str
    title: str
    summary: str
    description: str
    pricing_kind: str
    amount_usd: Optional[str]                      # fixed: a decimal string; quote: None
    rails: Tuple[Tuple[str, str], ...]             # (asset_id, mode)
    inputs: Tuple[InputSpec, ...]
    deliverable_kind: str
    proof: str
    playbook: str
    tools: Tuple[str, ...]
    spend_usd_per_order: float
    llm_usd_per_order: float
    deadline_min: int
    max_attempts: int
    caps: Mapping[str, int] = field(default_factory=dict)
    refund: str = "never"
    acceptance_days: int = 0                       # 0 = accepted on delivery
    listings: Tuple[str, ...] = ()
    spec: str = SPEC

    # -- derived ---------------------------------------------------------------

    def role_field(self, role: str) -> Optional[str]:
        for i in self.inputs:
            if i.role == role:
                return i.name
        return None

    def native_surfaces(self) -> Tuple[str, ...]:
        return tuple(x.split(":", 1)[1] for x in self.listings if x.startswith("native:"))

    def price_label(self) -> str:
        return f"${Decimal(self.amount_usd):,.2f}" if self.amount_usd else "quoted per order"

    def to_dict(self) -> Dict[str, Any]:
        return {
            "spec": self.spec, "id": self.id, "title": self.title, "summary": self.summary,
            "description": self.description,
            "pricing": {"kind": self.pricing_kind, "amount_usd": self.amount_usd},
            "rails": [{"asset": a, "mode": m} for a, m in self.rails],
            "inputs": {i.name: {k: v for k, v in (
                ("type", i.type), ("required", i.required), ("role", i.role),
                ("format", i.format), ("maxLength", i.max_length),
                ("description", i.description)) if v not in (None, "")}
                for i in self.inputs},
            "deliverable": {"kind": self.deliverable_kind, "proof": self.proof},
            "fulfillment": {"playbook": self.playbook, "tools": list(self.tools),
                            "budget_per_order": {"spend_usd": self.spend_usd_per_order},
                            "llm_budget_per_order": {"usd": self.llm_usd_per_order},
                            "deadline_min": self.deadline_min,
                            "max_attempts": self.max_attempts},
            "caps": dict(self.caps),
            "terms": {"refund": self.refund, "acceptance_days": self.acceptance_days},
            "listings": list(self.listings),
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), sort_keys=True, separators=(",", ":"))


# -- validation -----------------------------------------------------------------

def _text(d: Mapping[str, Any], key: str, limit: int, *, required: bool = True) -> str:
    v = d.get(key)
    if v is None or str(v).strip() == "":
        if required:
            raise OfferError(f"{key} is required")
        return ""
    s = " ".join(str(v).split()) if limit <= 300 else str(v).strip()
    if len(s) > limit:
        raise OfferError(f"{key} is longer than {limit} characters")
    return s


def _num(v: Any, key: str, lo: float, hi: float) -> float:
    try:
        n = float(v)
    except (TypeError, ValueError):
        raise OfferError(f"{key} must be a number") from None
    if not lo <= n <= hi:
        raise OfferError(f"{key} must be between {lo:g} and {hi:g}")
    return n


def _int(v: Any, key: str, lo: int, hi: int) -> int:
    if isinstance(v, bool):
        raise OfferError(f"{key} must be a whole number")
    try:
        n = int(v)
    except (TypeError, ValueError):
        raise OfferError(f"{key} must be a whole number") from None
    if float(v) != n or not lo <= n <= hi:
        raise OfferError(f"{key} must be a whole number between {lo} and {hi}")
    return n


def _minutes(v: Any) -> int:
    """``30`` or ``"PT30M"`` / ``"PT2H"`` -> minutes."""
    if isinstance(v, str):
        m = re.fullmatch(r"PT(?:(\d+)H)?(?:(\d+)M)?", v.strip().upper())
        if not m or not (m.group(1) or m.group(2)):
            raise OfferError("fulfillment.deadline must be minutes or PT<n>H<n>M")
        v = int(m.group(1) or 0) * 60 + int(m.group(2) or 0)
    return _int(v, "fulfillment.deadline", 1, MAX_DEADLINE_MIN)


def _price(pricing: Mapping[str, Any]) -> Tuple[str, Optional[str]]:
    kind = str(pricing.get("kind") or "").strip().lower()
    if kind in PRICING_LATER:
        raise OfferError(f"pricing.kind {kind!r} is not supported yet — use fixed or quote")
    if kind not in PRICING_KINDS:
        raise OfferError("pricing.kind must be fixed or quote")
    if kind == "quote":
        return kind, None
    try:
        amt = Decimal(str(pricing.get("amount_usd")))
    except (InvalidOperation, TypeError):
        raise OfferError("pricing.amount_usd must be a decimal number of dollars") from None
    if not (amt.is_finite() and Decimal("0.01") <= amt <= MAX_PRICE_USD):
        raise OfferError(f"pricing.amount_usd must be between 0.01 and {MAX_PRICE_USD}")
    return kind, str(amt.quantize(Decimal("0.01")))


def _inputs(raw: Any) -> Tuple[InputSpec, ...]:
    if raw in (None, {}):
        return ()
    if not isinstance(raw, Mapping):
        raise OfferError("inputs must be an object of field -> spec")
    if len(raw) > MAX_INPUTS:
        raise OfferError(f"at most {MAX_INPUTS} inputs")
    out, roles = [], set()
    for name, spec in raw.items():
        if not FIELD_RE.match(str(name)):
            raise OfferError(f"input name {name!r} must be lower_snake_case")
        spec = spec if isinstance(spec, Mapping) else {}
        typ = str(spec.get("type") or "string")
        if typ not in INPUT_TYPES:
            raise OfferError(f"input {name}: type must be one of {', '.join(INPUT_TYPES)}")
        role = spec.get("role")
        if role is not None:
            role = str(role)
            if role not in ROLES:
                raise OfferError(f"input {name}: role must be one of {', '.join(ROLES)}")
            if role in roles and role != "untrusted_text":
                raise OfferError(f"only one input may carry role {role}")
            roles.add(role)
        fmt = spec.get("format")
        if fmt is not None and str(fmt) not in INPUT_FORMATS:
            raise OfferError(f"input {name}: format must be one of {', '.join(INPUT_FORMATS)}")
        if role in ("recipient", "refund") and fmt != "evm_address":
            raise OfferError(f"input {name}: a {role} must have format evm_address")
        max_len = _int(spec.get("maxLength", spec.get("max_length", 280)),
                       f"input {name}.maxLength", 1, 2000)
        out.append(InputSpec(name=str(name), type=typ,
                             required=bool(spec.get("required", True)), role=role,
                             format=str(fmt) if fmt else None, max_length=max_len,
                             description=" ".join(str(spec.get("description") or "")
                                                  .split())[:200]))
    return tuple(out)


def _listings(raw: Any) -> Tuple[str, ...]:
    out = []
    for x in raw or ():
        s = str(x or "").strip().lower()
        if s.startswith("native:"):
            surface = s.split(":", 1)[1]
            allowed = native_surfaces()
            if surface not in allowed:
                raise OfferError(f"native:{surface} cannot list an offer (email is never a "
                                 f"buying channel); use one of "
                                 f"{', '.join('native:' + n for n in allowed)}")
        elif s not in MACHINE_LISTINGS and not MARKET_RE.match(s):
            raise OfferError(f"unknown listing {s!r} (native:<surface>, a2a, mcp, http402, "
                             f"market:<name>)")
        if s not in out:
            out.append(s)
    return tuple(out)


def validate(raw: Any) -> OfferManifest:
    """An untrusted manifest dict (or its JSON) -> :class:`OfferManifest`."""
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except ValueError:
            raise OfferError("the manifest is not valid JSON") from None
    if not isinstance(raw, Mapping):
        raise OfferError("the manifest must be an object")
    spec = str(raw.get("spec") or SPEC)
    if spec != SPEC:
        raise OfferError(f"spec must be {SPEC}")
    oid = str(raw.get("id") or "").strip().lower()
    if not ID_RE.match(oid):
        raise OfferError("id must be 2-41 characters: a-z, 0-9 and '-', starting with a letter")
    kind, amount = _price(raw.get("pricing") or {})
    rails = []
    for r in raw.get("rails") or ():
        r = r if isinstance(r, Mapping) else {}
        asset = str(r.get("asset") or "").strip().lower()
        mode = str(r.get("mode") or "transfer").strip().lower()
        if not re.fullmatch(r"[a-z0-9][a-z0-9_.-]{1,40}", asset):
            raise OfferError("each rail needs an asset id, e.g. usdc-base")
        if mode not in RAIL_MODES:
            raise OfferError(f"rail mode must be one of {', '.join(RAIL_MODES)}")
        rails.append((asset, mode))
    if not rails:
        raise OfferError("an offer needs at least one payment rail")
    inputs = _inputs(raw.get("inputs"))
    deliv = raw.get("deliverable") or {}
    dkind = str(deliv.get("kind") or "").strip().lower()
    if dkind not in DELIVERABLE_KINDS:
        raise OfferError(f"deliverable.kind must be one of {', '.join(DELIVERABLE_KINDS)}")
    proof = str(deliv.get("proof") or "none").strip().lower()
    if proof not in PROOF_KINDS:
        raise OfferError(f"deliverable.proof must be one of {', '.join(PROOF_KINDS)}")
    if dkind.startswith("onchain_") and proof != "tx_receipt":
        raise OfferError("an on-chain deliverable is proven by a tx_receipt")
    if dkind == "onchain_asset" and not any(i.role == "recipient" for i in inputs):
        raise OfferError("an on-chain asset needs an input with role recipient")
    ful = raw.get("fulfillment") or {}
    playbook = str(ful.get("playbook") or "").strip().lower()
    if not SKILL_RE.match(playbook):
        raise OfferError("fulfillment.playbook must be a skill id (a-z, 0-9, '-')")
    tools = []
    for t in ful.get("tools") or ():
        t = str(t or "").strip()
        if not TOOL_RE.match(t):
            raise OfferError(f"fulfillment.tools: {t!r} is not a tool id")
        if t not in tools:
            tools.append(t)
    if not tools or len(tools) > MAX_TOOLS:
        raise OfferError(f"fulfillment.tools must list 1-{MAX_TOOLS} tool ids")
    spend = _num(((ful.get("budget_per_order") or {}).get("spend_usd", 0)),
                 "fulfillment.budget_per_order.spend_usd", 0, MAX_ORDER_SPEND_USD)
    llm = _num(((ful.get("llm_budget_per_order") or {}).get("usd", 0.5)),
               "fulfillment.llm_budget_per_order.usd", 0, MAX_ORDER_LLM_USD)
    deadline = _minutes(ful.get("deadline", ful.get("deadline_min", 30)))
    attempts = _int(ful.get("max_attempts", 2), "fulfillment.max_attempts", 1, MAX_ATTEMPTS)
    caps_raw = raw.get("caps") or {}
    caps = {}
    for k in ("per_day", "open_per_buyer", "supply"):
        if caps_raw.get(k) is not None:
            caps[k] = _int(caps_raw[k], f"caps.{k}", 1, 1_000_000)
    caps.setdefault("per_day", 20)
    caps.setdefault("open_per_buyer", 1)
    terms = raw.get("terms") or {}
    refund = str(terms.get("refund") or "never").strip().lower()
    if refund not in REFUND_TERMS:
        raise OfferError("terms.refund must be never or on_failure")
    acc = terms.get("acceptance", terms.get("acceptance_days", 0))
    if isinstance(acc, str) and acc.strip().lower() == "auto":
        acc = 0
    if isinstance(acc, str):
        m = re.fullmatch(r"P(\d+)D", acc.strip().upper())
        if not m:
            raise OfferError("terms.acceptance must be auto or P<n>D")
        acc = int(m.group(1))
    acceptance = _int(acc, "terms.acceptance_days", 0, 30)
    listings = _listings(raw.get("listings"))
    if not listings:
        raise OfferError("list the offer somewhere (native:telegram, a2a, mcp, http402, …)")
    return OfferManifest(
        id=oid, title=_text(raw, "title", 80), summary=_text(raw, "summary", 200),
        description=_text(raw, "description", 4000, required=False),
        pricing_kind=kind, amount_usd=amount, rails=tuple(rails), inputs=inputs,
        deliverable_kind=dkind, proof=proof, playbook=playbook, tools=tuple(tools),
        spend_usd_per_order=spend, llm_usd_per_order=llm, deadline_min=deadline,
        max_attempts=attempts, caps=caps, refund=refund, acceptance_days=acceptance,
        listings=listings)


def validate_inputs(m: OfferManifest, given: Mapping[str, Any]) -> Dict[str, Any]:
    """A buyer's inputs -> the clean dict the order stores. Raises :class:`OfferError`."""
    given = dict(given or {})
    unknown = sorted(set(given) - {i.name for i in m.inputs})
    if unknown:
        raise OfferError(f"unknown input(s): {', '.join(unknown)}")
    out: Dict[str, Any] = {}
    for spec in m.inputs:
        v = given.get(spec.name)
        if v is None or str(v).strip() == "":
            if spec.required:
                raise OfferError(f"{spec.name} is required")
            continue
        if spec.type == "integer":
            v = _int(v, spec.name, -10**12, 10**12)
        elif spec.type == "number":
            v = _num(v, spec.name, -1e12, 1e12)
        else:
            v = str(v).strip()
            if len(v) > spec.max_length:
                raise OfferError(f"{spec.name} is longer than {spec.max_length} characters")
            if spec.format == "evm_address":
                if not EVM_RE.match(v) or int(v, 16) == 0:
                    raise OfferError(f"{spec.name} must be a 0x address")
                v = v.lower()
            elif spec.format == "email" and not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", v):
                raise OfferError(f"{spec.name} must be an email address")
            elif spec.format == "url" and not re.fullmatch(r"https://\S+", v):
                raise OfferError(f"{spec.name} must be an https URL")
        out[spec.name] = v
    return out


__all__ = ["InputSpec", "OfferError", "OfferManifest", "SPEC", "validate", "validate_inputs"]
