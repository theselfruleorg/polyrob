"""Owner knowledge replies for the Telegram admin verbs: /kb and /files.

Moved out of ``surfaces/telegram/harness.py`` (W9, 2026-10-10 structural
review) unchanged; the harness re-exports every name. Imports nothing from the
harness (no import cycle).
"""


async def _kb_reply(user_id: str, args: list) -> str:
    """QW-4 (proposal 021): owner KB read from the phone — the same
    ``modules.memory.registry`` primitives ``polyrob kb`` uses. Before this
    verb, "ingested into KB" was write-only theatre from the owner's seat
    (assessment 2026-07-19 §2 touchpoint 4).

    CLI7 (audit 2026-10-03): ``args`` is parsed by the ONE grammar
    (``core.kb_grammar``) the REPL parses — `/kb` lists, `/kb list [coll]`,
    `/kb search <q>`, and a bare `/kb <words>` searches."""
    from core.kb_grammar import KB_USAGE, LIST, USAGE, parse_kb_args
    cmd = parse_kb_args(list(args or []))
    if cmd.action == USAGE:
        return KB_USAGE
    from core.config_policy import AutonomyConfig
    if not AutonomyConfig.kb_enabled():
        # D72: name what the owner can DO, not the env flag. He is on a phone;
        # a flag name is a thing he can only act on by opening a shell.
        return ("My knowledge base is switched off, so I have nothing to "
                "search. Turn it on with `polyrob config set KB_ENABLED true` "
                "(it applies on my next restart).")
    import modules.memory.registry as _reg
    if cmd.action == LIST:
        try:
            sources = await _reg.kb_list_sources(user_id=user_id,
                                                 collection=cmd.collection)
        except Exception as e:
            return f"KB list failed: {e}"
        where = f" in collection {cmd.collection!r}" if cmd.collection else ""
        if not sources:
            return f"No sources in the knowledge base{where}."
        shown = [f"• `{src}`" for src in list(sources)[:30]]
        more = len(sources) - len(shown)
        return "\n".join([f"KB sources ({len(sources)}){where}:"] + shown
                         + ([f"… and {more} more"] if more > 0 else [])
                         + ["Search: /kb <words>"])
    query = cmd.query
    try:
        result = await _reg.kb_search(query, user_id=user_id, limit=5)
    except Exception as e:
        return f"KB search failed: {e}"
    text = str(result or "").strip()
    if not text:
        return f"No results for {query!r}."
    return text[:3500] + ("…" if len(text) > 3500 else "")


async def _files_reply(user_id: str, args) -> str:
    """QW-4: recent run artifacts from the episode registry — the owner's file
    view over what background runs actually produced. Artifact rows may be
    JSON strings (older writes) or lists — both are handled."""
    try:
        n = max(1, min(int(args[0]), 30)) if args else 10
    except (TypeError, ValueError):
        n = 10
    import json
    import modules.memory.registry as _reg
    try:
        # window scales with the ask so /files 30 can actually find 30 distinct
        # files across episodes (review Minor #8)
        rows = await _reg.memory_recall_episodes(
            user_id=user_id, limit=max(30, min(n * 5, 100)), order="newest")
    except Exception as e:
        return f"Could not read episodes: {e}"
    seen: set = set()
    lines: list = []
    for r in rows or []:
        get = (r.get if isinstance(r, dict) else lambda k, d=None: getattr(r, k, d))
        arts = get("artifacts") or []
        if isinstance(arts, str):
            try:
                arts = json.loads(arts)
            except (ValueError, TypeError):
                arts = []
        sid = str(get("session_id") or "")
        for a in arts if isinstance(arts, list) else []:
            if not isinstance(a, dict) or not a.get("path"):
                continue
            path = str(a["path"])
            if path in seen:
                continue
            seen.add(path)
            size = a.get("bytes")
            size_s = (f" ({float(size) / 1024:.1f} KB)"
                      if isinstance(size, (int, float)) else "")
            # Backticked so the renderer emits <code>: a bare `report.md` is
            # auto-linked by Telegram as a Moldovan domain (G2).
            lines.append(f"• `{path}`{size_s}"
                         + (f" — session {sid[:8]}" if sid else ""))
            if len(lines) >= n:
                break
        if len(lines) >= n:
            break
    if not lines:
        return "No file artifacts recorded yet."
    out = ["Recent artifacts (newest first):"] + lines
    try:
        from core.surfaces.deep_link import webview_public_url
        base = webview_public_url()
        if base:
            out.append(f"Console: {base}")
    except Exception:
        pass
    return "\n".join(out)
