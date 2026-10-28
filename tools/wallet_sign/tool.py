"""The ``wallet_sign`` tool — sign a login or proof-of-wallet text (proposal 090 P0).

⚠️ This module deliberately does NOT ``from __future__ import annotations``: the
Registry introspects each action's first-parameter annotation to route the
validated param model, and stringized annotations break that.

Every request goes through ``core.wallet.sign_policy.classify`` and only class A
is signed: a Sign-In With Ethereum/Solana message, or a service's challenge text,
bound to the site it is for. Two forms:

* ``wallet_sign_challenge`` (preferred) — the tool itself asks the HTTPS
  challenge URL (POST or GET), signs the exact message the site returned, and
  POSTs the signature back to the same site in the SAME call (``submit_url``).
  The origin is proven by construction; a session cookie the challenge set rides
  along to the submit. A credential the site returns (a JSON field or a
  ``Set-Cookie``) is encrypted into the store and shown only as a fingerprint.
  A call with no ``submit_url`` returns the signature AND the challenge response,
  says plainly that nothing was sent, and a second such call for the same site
  within ``UNSENT_WINDOW_SEC`` refuses (prod 2026-10-28: 14 unsent signatures).
* ``wallet_sign_message`` — the caller hands over the text and names the site.
  Only a genuine owner turn may use it (an autonomous run uses the fetch-bound
  form), and the text must name the site.

Gates, in order: the turn (owner, or an autonomous goal/cron run on the main
agent — a sub-agent, a self-wake and a delegation result are refused), the
class, the replay log (a sign-in nonce signs once), and the autonomous rate
(``AUTONOMOUS_PER_ORIGIN_DAY`` per site per day). A correspondent-tainted
session never reaches the tool (``core/verb_policy_rows.py``). The key is the
operational wallet's (090 D8); no parameter picks another.
"""
import json
import logging
import re
import time
import types
from datetime import datetime, timezone
from urllib.parse import quote
from typing import Any, Dict, List, Literal, Optional, Tuple

from pydantic import BaseModel, ConfigDict, Field, field_validator

from tools.base_tool import BaseTool
from tools.wallet_holder import WalletHolderMixin

logger = logging.getLogger(__name__)

_NULL_CONFIG = types.SimpleNamespace()
#: 090 D2: class-A signatures an autonomous run may make per site per day,
#: and across every site.
AUTONOMOUS_PER_ORIGIN_DAY = 10
AUTONOMOUS_ALL_ORIGINS_DAY = 30
_MAX_RESPONSE = 256 * 1024
_SHOWN_RESPONSE = 4000
#: A secret-named string field shorter than this is not a credential.
_MIN_SECRET_LEN = 8
#: Field names a site returns a credential under that ``is_secret_key`` misses.
_CREDENTIAL_WORDS = ("session", "auth", "bearer", "cookie", "access", "refresh", "jwt")
#: A field named for an error ("auth_error", "access_denied") is the site's answer,
#: never a credential: it stays readable.
_ERROR_WORDS = ("error", "denied", "fail", "invalid", "reason", "message", "detail")
#: A signature made with no submit_url is sent nowhere; a second one for the same
#: site inside this window refuses and names the one-call form.
UNSENT_WINDOW_SEC = 600
#: ``{address}``, ``{signature}``, ``{message}``, ``{challenge.<path>}`` — whole
#: value or inside a longer string.
_SLOT_RE = re.compile(r"\{(address|signature|message|issued_at|expires_at|"
                      r"challenge\.[A-Za-z0-9_.\-]+)\}")
_COOKIE_NAME_RE = r"^[!#$%&'*+\-.^_`|~0-9A-Za-z]{1,128}$"
#: The only headers a stored credential may ride in.
_CREDENTIAL_HEADER_RE = r"^(?:Authorization|X-[A-Za-z0-9-]*(?:Key|Token|Auth)[A-Za-z0-9-]*|Api-Key)$"

_FAMILY = Field("evm", description="'evm' (EIP-191) or 'solana' (ed25519).")


def _json_text(value):
    """The tool-call layer deep-parses a JSON-looking string into an object
    (``tool_call_builder`` "parsed args (deep)"), so a ``*_json`` field arrives as
    either. Keep the schema a plain string and accept both."""
    if isinstance(value, (dict, list)):
        return json.dumps(value)
    return value


class ChallengeParams(BaseModel):
    model_config = ConfigDict(extra="forbid")
    challenge_url: str = Field(..., max_length=2048, description=(
        "HTTPS URL that issues the challenge; '{address}' in it becomes this wallet's address."))
    challenge_method: Literal["POST", "GET"] = Field("POST", description=(
        "POST (body = challenge_body_json) or GET (no body)."))
    challenge_body_json: str = Field("{}", max_length=8192, description=(
        "JSON object POSTed to challenge_url; '{address}' becomes this wallet's address."))
    message_field: str = Field("message", max_length=200, description=(
        "Dot path of the text to sign in the challenge response; '' = the whole response "
        "body is the text (a text/plain challenge)."))
    message_template: Optional[str] = Field(None, max_length=4096, description=(
        "Only for a site that returns a nonce and expects the client to build a Sign-In With "
        "Ethereum/Solana message: the full EIP-4361 text with '{address}', "
        "'{challenge.<path>}', '{issued_at}', '{expires_at}' filled in. Anything that does "
        "not parse as a sign-in message for this site is refused."))
    submit_url: Optional[str] = Field(None, max_length=2048, description=(
        "HTTPS URL on the same site to POST the signature to. Give it: without it the "
        "signature is sent nowhere and nothing is enrolled or logged in."))
    submit_body_json: str = Field("{}", max_length=8192, description=(
        "JSON object POSTed to submit_url. Filled in: '{signature}', '{address}', "
        "'{message}', and '{challenge.<path>}' = a field of the challenge response. Example "
        "for a challenge that returns {\"challengeId\": ..., \"message\": ...}: "
        "{\"wallet\": \"{address}\", \"challengeId\": \"{challenge.challengeId}\", "
        "\"signature\": \"{signature}\"}"))
    credential_label: str = Field("", max_length=80, description=(
        "Name for a credential the site returns (stored encrypted; default: its field name)."))
    family: Literal["evm", "solana"] = _FAMILY

    _bodies = field_validator("challenge_body_json", "submit_body_json", mode="before")(
        classmethod(lambda cls, v: _json_text(v)))


class MessageParams(BaseModel):
    model_config = ConfigDict(extra="forbid")
    text: str = Field(..., min_length=1, max_length=4096, description=(
        "The exact text; it must name the site in `origin`."))
    origin: str = Field(..., min_length=3, max_length=255, description=(
        "The site the signature is for (host or https URL)."))
    family: Literal["evm", "solana"] = _FAMILY


class ApiCallParams(BaseModel):
    model_config = ConfigDict(extra="forbid")
    url: str = Field(..., max_length=2048, description="HTTPS URL on the credential's own host.")
    credential_id: int = Field(..., ge=1)
    method: Literal["GET", "POST"] = "GET"
    body_json: str = Field("{}", max_length=65536)
    header: str = Field("Authorization", max_length=64, pattern=_CREDENTIAL_HEADER_RE)
    prefix: str = Field("Bearer ", max_length=32)
    cookie_name: Optional[str] = Field(None, max_length=128, pattern=_COOKIE_NAME_RE, description=(
        "Send the credential as the cookie '<cookie_name>=<secret>' instead of a header "
        "(a session cookie the site set)."))

    _body = field_validator("body_json", mode="before")(classmethod(lambda cls, v: _json_text(v)))


class StatusParams(BaseModel):
    model_config = ConfigDict(extra="forbid")
    limit: int = Field(10, ge=1, le=50)


def _https_host(url: str) -> Tuple[Optional[str], str]:
    """``(error, host)`` — only an https URL with a host passes."""
    from urllib.parse import urlsplit
    try:
        parts = urlsplit(str(url or "").strip())
    except ValueError:
        return "not a URL", ""
    if parts.scheme != "https":
        return "only https:// URLs are accepted (a signature over plain http is a gift)", ""
    host = (parts.hostname or "").lower().rstrip(".")
    if not host:
        return "the URL has no host", ""
    return None, host


def _secretish(key: str) -> bool:
    from core.secrets import is_secret_key
    low = str(key).lower()
    if any(w in low for w in _ERROR_WORDS):
        return False
    return is_secret_key(str(key)) or low == "sk" or any(w in low for w in _CREDENTIAL_WORDS)


def _dig(obj: Any, path: str) -> Any:
    cur = obj
    for part in [p for p in str(path or "").split(".") if p]:
        if isinstance(cur, dict) and part in cur:
            cur = cur[part]
        elif isinstance(cur, list) and part.isdigit() and int(part) < len(cur):
            cur = cur[int(part)]
        else:
            return None
    return cur


def _json_object(text: str, what: str) -> Dict[str, Any]:
    try:
        value = json.loads(text or "{}")
    except ValueError as exc:
        raise ValueError(f"{what} is not valid JSON ({exc})") from exc
    if not isinstance(value, dict):
        raise ValueError(f"{what} must be a JSON object")
    return value


def _slot(name: str, slots: Dict[str, Any], challenge: Any) -> Any:
    if name in slots:
        return slots[name]
    if name.startswith("challenge."):
        if challenge is None:
            raise ValueError(f"{{{name}}} needs the challenge response, which is not known yet")
        found = _dig(challenge, name[len("challenge."):])
        if found is None:
            keys = sorted(challenge) if isinstance(challenge, dict) else "-"
            raise ValueError(f"the challenge response has no field {name[10:]!r} (keys: {keys})")
        return found
    return "{" + name + "}"


def _fill(value: Any, slots: Dict[str, Any], challenge: Any) -> Any:
    """Fill every slot in a JSON value. A string that IS one slot takes the slot's
    value as it is (a number stays a number); a slot inside a longer string is
    rendered as text."""
    if isinstance(value, dict):
        return {k: _fill(v, slots, challenge) for k, v in value.items()}
    if isinstance(value, list):
        return [_fill(v, slots, challenge) for v in value]
    if isinstance(value, str):
        whole = _SLOT_RE.fullmatch(value)
        if whole:
            return _slot(whole.group(1), slots, challenge)
        return _SLOT_RE.sub(lambda m: str(_slot(m.group(1), slots, challenge)), value)
    return value


def _fill_url(url: str, slots: Dict[str, Any], challenge: Any) -> str:
    return _SLOT_RE.sub(lambda m: quote(str(_slot(m.group(1), slots, challenge)), safe=""),
                        str(url or ""))


def _cookie_pairs(set_cookies) -> Dict[str, str]:
    """``name -> value`` from raw ``Set-Cookie`` headers (attributes dropped; a
    malformed or empty pair skipped)."""
    out: Dict[str, str] = {}
    for raw in set_cookies or ():
        pair = str(raw).split(";", 1)[0]
        name, sep, value = pair.partition("=")
        name, value = name.strip(), value.strip().strip('"')
        if sep and value and re.match(_COOKIE_NAME_RE, name) and not re.search(r"[\s,;\\]", value):
            out[name] = value
    return out


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat(timespec="seconds").replace(
        "+00:00", "Z")


#: A challenge key named like one of these holds a URL the site may name for the submit.
_URL_KEY_WORDS = ("url", "uri", "endpoint", "submit", "verify", "callback", "href", "link")
#: A challenge key named like one of these is a reference the submit usually echoes back.
_REF_KEY_WORDS = ("id", "nonce", "challenge", "ref")


def _submit_urls(challenge: Any, host: str, *, depth: int = 0) -> List[str]:
    """HTTPS URLs on the challenge's own site that the challenge response names
    (a full URL anywhere, or a path under a URL-named key). Never a guess.

    093 review: a secret-named key (and everything under it) is withheld from
    the hint like it is from the shown challenge, and a suggested URL carries
    no query or fragment — either can hold a token
    (``access_token_url: https://site/cb?access_token=…``)."""
    from urllib.parse import urlsplit, urlunsplit
    from core.wallet.sign_policy import site_of
    found: List[str] = []
    if depth > 3:
        return found
    items = (challenge.items() if isinstance(challenge, dict)
             else enumerate(challenge) if isinstance(challenge, list) else ())
    for key, value in items:
        if isinstance(key, str) and _secretish(key):
            continue
        if isinstance(value, str):
            text = value.strip()
            if text.startswith("/") and not text.startswith("//") and any(
                    w in str(key).lower() for w in _URL_KEY_WORDS):
                text = f"https://{host}{text}"
            err, url_host = _https_host(text) if text.startswith("https://") else ("-", "")
            if err or site_of(url_host) != site_of(host):
                continue
            try:
                parts = urlsplit(text)
                text = urlunsplit((parts.scheme, parts.netloc.rsplit("@", 1)[-1], parts.path, "", ""))
            except ValueError:
                continue
            if text not in found:
                found.append(text)
        elif isinstance(value, (dict, list)):
            found += [u for u in _submit_urls(value, host, depth=depth + 1) if u not in found]
    return found[:3]


def _example_submit_body(keys: List[str], message_field: str) -> str:
    """A filled ``submit_body_json`` for this challenge's real top-level keys (prod
    2026-10-28: the run claimed the tool never returns the challenge body)."""
    usable = [k for k in keys if k != message_field and re.fullmatch(r"[A-Za-z0-9_\-]+", k)]
    refs = [k for k in usable if any(w in k.lower() for w in _REF_KEY_WORDS)
            and not any(w in k.lower() for w in _URL_KEY_WORDS)]
    body: Dict[str, str] = {"wallet": "{address}"}
    for k in (refs or usable)[:6]:
        body[k] = "{challenge." + k + "}"
    if not keys:
        body["message"] = "{message}"
    body["signature"] = "{signature}"
    return json.dumps(body, separators=(",", ":"))


def _submit_hint(keys: List[str], urls: List[str], message_field: str, host: str, *,
                 with_url: bool = True) -> str:
    """How to finish in ONE call: the challenge's keys, a filled example body, and
    the submit URL when the challenge named one."""
    parts = []
    if keys:
        parts.append(f"The challenge response's top-level keys: {', '.join(keys[:20])}.")
    parts.append(
        f"Example: submit_body_json='{_example_submit_body(keys, message_field)}'. The tool "
        f"itself fills each '{{challenge.<key>}}' from the challenge it fetches in that same "
        f"call, '{{signature}}' with the new signature and '{{address}}' with this wallet; "
        f"copy no value by hand.")
    if not with_url:
        pass
    elif urls:
        parts.append(f"submit_url: {urls[0]} (named in the challenge response).")
    else:
        parts.append(f"submit_url: the challenge response names none; use the verify/enroll "
                     f"URL on {host} that the site's docs give.")
    return " ".join(parts)


class WalletSignTool(WalletHolderMixin, BaseTool):
    """Sign a login / proof-of-wallet text with the agent's own wallet (class A only)."""

    def __init__(self, name: str = "wallet_sign", config=None, container=None, *,
                 wallet=None, store=None, fetch_fn=None, now_fn=None):
        super().__init__(name=name, config=config if config is not None else _NULL_CONFIG,
                         container=container)
        self._wallet = wallet
        self._store = store
        self._fetch_fn = fetch_fn
        self._now_fn = now_fn or time.time
        #: site -> when a signature for it was made with no submit_url (sent nowhere).
        self._unsent: Dict[str, float] = {}
        #: site -> the hint for that unsent challenge (its keys, example body, submit URL).
        self._unsent_hint: Dict[str, str] = {}

    async def _initialize(self):
        return True

    # -- plumbing -------------------------------------------------------------

    def _get_store(self):
        if self._store is None:
            from tools.wallet_sign.store import WalletSignStore
            self._store = WalletSignStore()
        return self._store

    async def _post(self, url: str, body: Optional[Dict[str, Any]], *, method: str = "POST",
                    headers: Optional[Dict[str, str]] = None):
        if self._fetch_fn is not None:
            return await self._fetch_fn(url, method=method, json_body=body,
                                        extra_headers=headers)
        from tools.web_fetch.fetcher import safe_fetch
        # No redirect, ever: a redirected POST would re-send a signature, and a redirected
        # GET would carry the credential header to whatever host the Location names.
        return await safe_fetch(url, method=method, json_body=body, extra_headers=headers,
                                max_bytes=_MAX_RESPONSE, max_redirects=0, timeout_sec=20.0)

    def _turn(self, execution_context) -> Tuple[Optional[str], bool]:
        """``(refusal, autonomous)`` from the money kernel's turn bars."""
        from core.money.authorize import SpendIntent, authorize_spend
        from tools.controller.turn_origin import (_is_autonomous_goal_turn,
                                                  _is_forged_or_autonomous_turn)
        verdict = authorize_spend(
            # pause=True: the owner's pause stops an autonomous run's signature; a
            # genuine owner turn always passes it (authorize_spend step 4, 090 D3).
            SpendIntent(tool="wallet_sign", what="sign with the wallet", turn=True),
            execution_context, tool_self=self,
            forged_fn=lambda ctx, tool: _is_forged_or_autonomous_turn(ctx, tool),
            autonomous_ok_fn=lambda ctx, tool: _is_autonomous_goal_turn(ctx, tool))
        if verdict.refused:
            if verdict.step == "turn":
                return ("refused: a self-wake, a delegation result or a sub-agent cannot sign "
                        "with the wallet — only the owner, or an autonomous goal/cron run on "
                        "the main agent"), False
            return verdict.reason, False
        return None, verdict.autonomous

    def _signer(self, family: str):
        wallet = self._get_wallet()
        if wallet is None:
            raise ValueError("no agent wallet is configured")
        if family == "solana":
            return wallet.solana_signer(0)
        return wallet.operational_signer()

    def _sign(self, req, signer, *, autonomous: bool, sign_in_only: bool = False):
        """``(signature, verdict)`` or raises naming the refusal. The replay and rate
        row is reserved BEFORE the key is used and dropped if signing fails."""
        from core.wallet.sign_policy import (SignContext, SignRefused, classify,
                                             sign_classified)
        now = self._now_fn()
        verdict = classify(req, SignContext(holder=signer.address, now=now))
        if not verdict.signable:
            raise SignRefused(verdict)
        if verdict.owner_turn_only and autonomous:
            raise ValueError(f"{verdict.reason} — this is an autonomous run")
        if sign_in_only and verdict.decoded.get("kind") not in ("siwe", "siws"):
            raise ValueError("message_template must build a Sign-In With Ethereum/Solana "
                             "message (EIP-4361 header first); a built text that is not one "
                             "could say anything")
        store = self._get_store()
        origin = verdict.decoded.get("domain") or ""
        nonce = verdict.replay_key[1] if verdict.replay_key else None
        row = store.reserve(scheme=req.scheme, origin=origin,
                            kind=str(verdict.decoded.get("kind") or ""), cls=verdict.cls,
                            digest=verdict.digest, address=signer.address,
                            autonomous=autonomous, nonce=nonce, now=now,
                            per_origin_day=AUTONOMOUS_PER_ORIGIN_DAY,
                            all_origins_day=AUTONOMOUS_ALL_ORIGINS_DAY)
        try:
            signature, verdict = sign_classified(signer, req, now=now)
        except BaseException:
            store.release(row)
            raise
        return signature, verdict

    @staticmethod
    def _refusal_text(exc: Exception) -> str:
        from core.wallet.sign_policy import SignRefused
        if isinstance(exc, SignRefused):
            v = exc.verdict
            tail = {
                "C": " It needs the owner (an approval card for this class is not built yet).",
                "D": " This is never signed.",
            }.get(v.cls, "")
            return f"not signed (class {v.cls}): {v.reason}.{tail}"
        return f"not signed: {exc}"

    # -- actions --------------------------------------------------------------

    @BaseTool.action(
        "Sign in to / enroll on a site with the agent's wallet in ONE call: ask its HTTPS "
        "challenge URL, sign the returned text, and POST the signature to submit_url on the "
        "same site. The tool fills submit_body_json from the challenge it fetched: "
        "'{challenge.challengeId}' = that field of the challenge, '{signature}' = the new "
        "signature. A key or session the site returns is stored encrypted. Signs "
        "login/challenge text only — never a hash, transaction, permit or order.",
        param_model=ChallengeParams)
    async def wallet_sign_challenge(self, params: ChallengeParams, execution_context=None):
        from core.wallet.sign_policy import site_of
        refusal, autonomous = self._turn(execution_context)
        if refusal:
            return self._ar(error=refusal)
        err, host = _https_host(params.challenge_url)
        if err:
            return self._ar(error=f"challenge_url: {err}")
        if autonomous and not params.submit_url:
            return self._ar(error=("an autonomous run must give submit_url: the signature goes "
                                   "to the site that issued the challenge, never back into "
                                   "the run. " + _submit_hint([], [], params.message_field,
                                                              host)))
        if params.submit_url:
            err, submit_host = _https_host(params.submit_url)
            if err:
                return self._ar(error=f"submit_url: {err}")
            if site_of(submit_host) != site_of(host):
                return self._ar(error=(f"submit_url is on {submit_host!r}, the challenge on "
                                       f"{host!r}: a signature goes back only to the site "
                                       f"that issued the challenge"))
        else:
            unsent = self._unsent.get(site_of(host))
            if unsent is not None and self._now_fn() - unsent < UNSENT_WINDOW_SEC:
                return self._ar(error=(
                    f"not signed: a challenge for {host} was already signed "
                    f"{int(self._now_fn() - unsent)} s ago with no submit_url, and that "
                    f"signature was sent nowhere. Each call makes a NEW challenge, so its "
                    f"signature only works when the SAME call submits it. Call again with "
                    f"submit_url (the site's verify/enroll URL) and submit_body_json using "
                    f"'{{challenge.<field>}}' for the challenge's fields and '{{signature}}'. "
                    + self._unsent_hint.get(site_of(host), _submit_hint(
                        [], [], params.message_field, host))))
        try:
            signer = self._signer(params.family)
            slots = {"address": signer.address}
            challenge_url = _fill_url(params.challenge_url, slots, None)
            if _https_host(challenge_url)[1] != host:
                raise ValueError("challenge_url: a filled-in value may not change the host")
            body = None
            if params.challenge_method == "POST":
                body = _fill(_json_object(params.challenge_body_json, "challenge_body_json"),
                             slots, None)
            submit_template = _json_object(params.submit_body_json, "submit_body_json")
        except Exception as exc:  # noqa: BLE001 — a bad input is an error result
            return self._ar(error=str(exc))
        try:
            got = await self._post(challenge_url, body, method=params.challenge_method)
        except Exception as exc:  # noqa: BLE001
            return self._ar(error=f"the challenge request failed: {exc}. Nothing was signed.")
        if not (200 <= got.status < 300):
            return self._ar(error=(f"the challenge URL answered HTTP {got.status}: "
                                   f"{got.body[:500].decode('utf-8', 'replace')}. Nothing was "
                                   f"signed."))
        raw_text = got.body.decode("utf-8", "replace")
        try:
            challenge = json.loads(raw_text)
        except ValueError:
            challenge = None
        cookies = _cookie_pairs(getattr(got, "set_cookies", ()))
        now = self._now_fn()
        from core.wallet.sign_policy import SignRequest
        scheme = "solana_message" if params.family == "solana" else "eip191"
        if params.message_template:
            try:
                message = _SLOT_RE.sub(lambda m: str(_slot(m.group(1), dict(
                    slots, issued_at=_iso(now), expires_at=_iso(now + UNSENT_WINDOW_SEC)),
                    challenge)), params.message_template)
            except Exception as exc:  # noqa: BLE001
                return self._ar(error=f"message_template: {exc}. Nothing was signed.")
        elif params.message_field.strip() in ("", "."):
            message = raw_text
        elif challenge is None:
            return self._ar(error=("the challenge response is not JSON. If the whole body is "
                                   "the text to sign, set message_field=''. Nothing was "
                                   "signed."))
        else:
            message = _dig(challenge, params.message_field)
            if not isinstance(message, str):
                return self._ar(error=(
                    f"the challenge response has no text at {params.message_field!r} (keys: "
                    f"{sorted(challenge) if isinstance(challenge, dict) else '-'}). If it "
                    f"gives only a nonce for a Sign-In With Ethereum/Solana message, use "
                    f"message_template. Nothing was signed."))
        # A template is built here, not fetched: only a sign-in message (which names
        # its site, address, nonce and expiry itself) may come from one.
        req = SignRequest(scheme=scheme, payload=message, bound_origin=host,
                          fetch_bound=not params.message_template)
        try:
            signature, verdict = self._sign(req, signer, autonomous=autonomous,
                                             sign_in_only=bool(params.message_template))
        except Exception as exc:  # noqa: BLE001
            return self._ar(error=self._refusal_text(exc))
        lines = [f"Signed a {verdict.decoded.get('kind')} for {host} with {signer.address} "
                 f"({scheme}, digest {verdict.digest[:18]}…)."]
        meta = {"origin": host, "address": signer.address, "scheme": scheme,
                "digest": verdict.digest}
        keys = [str(k) for k in challenge] if isinstance(challenge, dict) else []
        hint = _submit_hint(keys, _submit_urls(challenge, host), params.message_field, host)
        if not params.submit_url:
            self._unsent[site_of(host)] = now
            self._unsent_hint[site_of(host)] = hint
            shown = self._shown_challenge(challenge, raw_text)
            fields = sorted(challenge) if isinstance(challenge, dict) else []
            lines += [
                "NOT SENT: no submit_url, so nothing is enrolled or logged in yet. This "
                "signature is for THIS challenge only; a new call makes a new challenge. To "
                "finish, call again with submit_url and submit_body_json"
                + (f" (challenge fields: {', '.join('{challenge.' + f + '}' for f in fields[:12])})"
                   if fields else "") + ". " + hint,
                f"signature: {signature}",
                "Challenge response:\n" + shown]
            return self._ar(content="\n".join(lines), metadata=meta)
        self._unsent.pop(site_of(host), None)
        self._unsent_hint.pop(site_of(host), None)
        try:
            submit_slots = {"signature": signature, "message": message,
                            "address": signer.address}
            submit_url = _fill_url(params.submit_url, submit_slots, challenge)
            err, filled_host = _https_host(submit_url)
            if err or site_of(filled_host) != site_of(host):
                raise ValueError("submit_url: a filled-in value may not change the site")
            submit_body = _fill(submit_template, submit_slots, challenge)
            headers = ({"Cookie": "; ".join(f"{k}={v}" for k, v in cookies.items())}
                       if cookies else None)
            sent = await self._post(submit_url, submit_body, headers=headers)
        except Exception as exc:  # noqa: BLE001
            return self._ar(error=(
                f"{lines[0]} Submitting it failed: {exc}. "
                + _submit_hint(keys, [], params.message_field, host, with_url=False)))
        ok = 200 <= sent.status < 300
        stored = []
        try:
            parsed = json.loads(sent.body[:_MAX_RESPONSE].decode("utf-8", "replace"))
        except json.JSONDecodeError:
            parsed = None
        try:
            if parsed is None:
                # A body that is not JSON cannot be scanned for a credential: withhold it.
                text = (f"[the response is not JSON ({len(sent.body)} bytes); withheld so no "
                        f"credential in it is printed]")
            else:
                parsed, stored = self._capture(parsed, origin=host, wallet=signer.address,
                                               label=params.credential_label, keep=ok)
                text = json.dumps(parsed, indent=1)
            if ok:
                stored += self._capture_cookies(getattr(sent, "set_cookies", ()), origin=host,
                                                wallet=signer.address,
                                                label=params.credential_label)
        except Exception as exc:  # noqa: BLE001 — never echo a credential we failed to store
            return self._ar(error=(f"{lines[0]} The site answered HTTP {sent.status}, but "
                                   f"its credential could not be stored ({exc}); the "
                                   f"response is withheld so the secret is not printed."))
        lines.append(f"Submitted to {submit_url}: HTTP {sent.status}.")
        for cred_id, label, fp in stored:
            how = (f"cookie_name={label.rsplit('cookie:', 1)[1]!r}, "
                   if "cookie:" in label else "")
            lines.append(f"Stored credential #{cred_id} '{label}' ({fp}) — use it with "
                         f"wallet_sign_api_call(credential_id={cred_id}, {how}url=...).")
        lines.append("Response:\n" + text[:_SHOWN_RESPONSE])
        meta.update(status=sent.status, credentials=[c[0] for c in stored])
        if not ok:
            return self._ar(error="\n".join(lines))
        return self._ar(content="\n".join(lines), metadata=meta)

    def _shown_challenge(self, challenge: Any, raw_text: str) -> str:
        """The challenge response for the run to read: secret-named fields withheld
        (never stored — a challenge issues no credential)."""
        if challenge is None:
            return raw_text[:_SHOWN_RESPONSE]
        shown, _ = self._capture(challenge, origin="", wallet="", label="", keep=False)
        return json.dumps(shown, indent=1)[:_SHOWN_RESPONSE]

    def _capture_cookies(self, set_cookies, *, origin: str, wallet: str, label: str):
        """Store each session cookie of a successful answer as a credential
        ``cookie:<name>``. ``[(id, label, fingerprint)]``."""
        stored = []
        store = None
        for name, value in _cookie_pairs(set_cookies).items():
            if len(value) < _MIN_SECRET_LEN:
                continue
            store = store or self._get_store()
            full = f"{label}:cookie:{name}" if label else f"cookie:{name}"
            fp = store.save_credential(origin=origin, wallet=wallet, label=full, secret=value)
            cred_id = next(c["id"] for c in store.credentials()
                           if c["origin"] == origin and c["wallet"] == wallet
                           and c["label"] == full)
            stored.append((cred_id, full, fp))
        return stored

    def _capture(self, value, *, origin: str, wallet: str, label: str, path: str = "",
                 keep: bool = True):
        """Replace every secret-named string field with its fingerprint after storing
        it encrypted. ``(value, [(id, label, fingerprint)])``. A failed answer
        (``keep=False``) stores nothing: its secret-named fields are only withheld."""
        stored = []
        if isinstance(value, dict):
            out = {}
            for k, v in value.items():
                here = f"{path}.{k}" if path else str(k)
                if isinstance(v, str) and len(v) >= _MIN_SECRET_LEN and _secretish(k) \
                        and not keep:
                    out[k] = "[withheld: a secret-named field, not stored]"
                elif isinstance(v, str) and len(v) >= _MIN_SECRET_LEN and _secretish(k):
                    name = f"{label}:{here}" if label else here
                    store = self._get_store()
                    fp = store.save_credential(origin=origin, wallet=wallet, label=name, secret=v)
                    cred_id = next(c["id"] for c in store.credentials()
                                   if c["origin"] == origin and c["wallet"] == wallet
                                   and c["label"] == name)
                    stored.append((cred_id, name, fp))
                    out[k] = f"[stored as credential #{cred_id}, {fp}]"
                else:
                    out[k], more = self._capture(v, origin=origin, wallet=wallet, label=label,
                                                 path=here, keep=keep)
                    stored += more
            return out, stored
        if isinstance(value, list):
            out_list = []
            for i, v in enumerate(value):
                item, more = self._capture(v, origin=origin, wallet=wallet, label=label,
                                           path=f"{path}.{i}", keep=keep)
                out_list.append(item)
                stored += more
            return out_list, stored
        return value, stored

    @BaseTool.action(
        "Sign a login/challenge text you already have (SIWE, Sign-In With Solana) for the site "
        "in `origin`, which the text must name. Owner turns only; prefer wallet_sign_challenge.",
        param_model=MessageParams)
    async def wallet_sign_message(self, params: MessageParams, execution_context=None):
        refusal, autonomous = self._turn(execution_context)
        if refusal:
            return self._ar(error=refusal)
        if autonomous:
            return self._ar(error=("an autonomous run signs only through "
                                   "wallet_sign_challenge, where the tool itself fetched the "
                                   "text from the site — a text handed over in a run could "
                                   "come from anywhere"))
        from core.wallet.sign_policy import SignRequest, origin_host
        scheme = "solana_message" if params.family == "solana" else "eip191"
        try:
            signer = self._signer(params.family)
            req = SignRequest(scheme=scheme, payload=params.text, bound_origin=params.origin)
            signature, verdict = self._sign(req, signer, autonomous=False)
        except Exception as exc:  # noqa: BLE001
            return self._ar(error=self._refusal_text(exc))
        host = origin_host(params.origin)
        return self._ar(
            content=(f"Signed a {verdict.decoded.get('kind')} for {host} with "
                     f"{signer.address} ({scheme}).\nsignature: {signature}"),
            metadata={"origin": host, "address": signer.address, "scheme": scheme,
                      "digest": verdict.digest})

    @BaseTool.action(
        "Call an HTTPS API with a credential wallet_sign stored; it goes only to the host that "
        "issued it and is never shown.",
        param_model=ApiCallParams)
    async def wallet_sign_api_call(self, params: ApiCallParams, execution_context=None):
        refusal, _autonomous = self._turn(execution_context)
        if refusal:
            return self._ar(error=refusal)
        err, host = _https_host(params.url)
        if err:
            return self._ar(error=f"url: {err}")
        try:
            cred = self._get_store().credential_secret(params.credential_id)
        except Exception as exc:  # noqa: BLE001 — unreadable is not empty
            return self._ar(error=f"the credential store could not be read ({exc})")
        if cred is None:
            return self._ar(error=f"no stored credential #{params.credential_id}")
        from core.wallet.sign_policy import site_of
        if site_of(host) != site_of(cred["origin"]):
            return self._ar(error=(f"credential #{params.credential_id} was issued by "
                                   f"{cred['origin']!r}; it is never sent to {host!r}"))
        if params.cookie_name:
            headers = {"Cookie": f"{params.cookie_name}={cred['secret']}"}
        else:
            headers = {params.header: params.prefix + cred["secret"]}
        try:
            body = _json_object(params.body_json, "body_json") if params.method == "POST" else None
            got = await self._post(params.url, body, method=params.method, headers=headers)
        except Exception as exc:  # noqa: BLE001
            return self._ar(error=f"the call failed: {exc}")
        text = got.body.decode("utf-8", "replace").replace(cred["secret"], "[credential]")
        out = f"HTTP {got.status} from {params.url}\n{text[:_SHOWN_RESPONSE]}"
        if not (200 <= got.status < 300):
            return self._ar(error=out)
        return self._ar(content=out, metadata={"status": got.status, "origin": host})

    @BaseTool.action(
        "Recent wallet signatures and stored credentials (ids and fingerprints only).",
        param_model=StatusParams)
    async def wallet_sign_status(self, params: StatusParams, execution_context=None):
        try:
            store = self._get_store()
            recent = store.recent(params.limit)
            creds = store.credentials()
        except Exception as exc:  # noqa: BLE001 — unreadable is not empty
            return self._ar(error=f"the signing log could not be read ({exc})")
        lines = [f"Signatures (last {params.limit}):" if recent else "No signatures yet."]
        for r in recent:
            when = time.strftime("%Y-%m-%d %H:%M", time.gmtime(r["ts"]))
            lines.append(f"- {when}Z {r['origin']} {r['kind']} ({r['scheme']}, class "
                         f"{r['cls']}{', autonomous' if r['autonomous'] else ''})")
        lines.append("Credentials:" if creds else "No stored credentials.")
        for c in creds:
            lines.append(f"- #{c['id']} {c['origin']} '{c['label']}' {c['fingerprint']} "
                         f"(wallet {c['wallet']})")
        return self._ar(content="\n".join(lines))
