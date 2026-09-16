"""Curated per-tenant notes (the optional `memory` tool + the C1 notes substrate: create/update/archive/list/get/backlinks/consolidate) over the ``curated_memory`` table.

Split out of ``modules/memory/sqlite_memory_provider.py`` (S5, 2026-08-29). A mixin: it
relies on the host provider for ``db_path``, ``_norm_user``/``_anon_blocked``,
``_run_blocking``, ``_row_cap``/``_clamp_limit``/``_fts_match``/``_query_terms`` and the
module ``logger``; ``SqliteMemoryProvider`` composes it and calls ``_init_curated_schema`` from
``_init_schema`` inside the same connection.
"""
import logging
from core.sqlite_util import execute_retry, wal_connect
import json
import os
import time

from modules.memory.wikilinks import parse_wikilinks

logger = logging.getLogger("modules.memory.sqlite_memory_provider")


class CuratedNotesStoreMixin:
    def _init_curated_schema(self, conn) -> None:
        """Create/migrate this store's tables on an open connection (no commit)."""
        # UP-09: curated per-tenant notes for the optional `memory` tool. Plain
        # table (no FTS) — small, read-in-full, agent-curated. Tenant-scoped by user_id.
        conn.execute(
            "CREATE TABLE IF NOT EXISTS curated_memory ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, user_id TEXT, content TEXT)"
        )
        # C1 (2026-07-11): promote curated_memory to a notes substrate — additive
        # column migration (plain table, ALTERable; a pre-C1 table is widened in
        # place, existing rows read as active legacy notes via COALESCE).
        note_cols = (
            ("title", "TEXT"),
            ("tags", "TEXT"),            # JSON list
            ("links", "TEXT"),           # JSON list of [[wikilink]] targets
            ("source", "TEXT"),          # provenance: session/episode/skill id
            ("created_ts", "INTEGER"),
            ("updated_ts", "INTEGER"),
            ("access_count", "INTEGER DEFAULT 0"),
            ("status", "TEXT DEFAULT 'active'"),   # active|pending|archived
            ("created_by", "TEXT"),
        )
        have = {r[1] for r in
                conn.execute("PRAGMA table_info(curated_memory)").fetchall()}
        for col, decl in note_cols:
            if col not in have:
                conn.execute(f"ALTER TABLE curated_memory ADD COLUMN {col} {decl}")

    # ---- curated per-tenant store (UP-09 `memory` tool) ----------------------
    @staticmethod
    def _curated_caps():
        try:
            max_entries = int(os.getenv("MEMORY_TOOL_MAX_ENTRIES", "50"))
        except ValueError:
            max_entries = 50
        try:
            max_chars = int(os.getenv("MEMORY_TOOL_MAX_CHARS", "2000"))
        except ValueError:
            max_chars = 2000
        return max_entries, max_chars
    async def curated_add(self, user_id, content: str) -> bool:
        """Legacy verb: add an active, untitled note. Returns False on anon-refusal,
        empty content, over-char-cap, or over-entry-cap (so the tool can report
        the reason). Delegates to note_create (C1)."""
        return (await self.note_create(user_id, content)) is not None
    async def curated_read(self, user_id) -> str:
        """Return this tenant's ACTIVE curated notes as newline-joined "- {content}"
        (or ""). Pre-C1 rows have status NULL and read as active."""
        if self._anon_blocked(user_id):
            return ""
        try:
            rows = execute_retry(
                self.db_path,
                "SELECT content FROM curated_memory WHERE user_id = ? "
                "AND COALESCE(status, 'active') = 'active' ORDER BY id",
                (self._norm_user(user_id),), fetch="all",
            )
        except Exception as e:
            logger.warning("curated_read failed: %s", e)
            return ""
        return "\n".join(f"- {r['content']}" for r in (rows or []))
    @staticmethod
    def _note_row_to_dict(r) -> dict:
        def _json_list(v):
            try:
                out = json.loads(v) if v else []
                return out if isinstance(out, list) else []
            except Exception:
                return []
        return {
            "id": r["id"], "title": r["title"], "content": r["content"],
            "tags": _json_list(r["tags"]), "links": _json_list(r["links"]),
            "source": r["source"], "created_ts": r["created_ts"],
            "updated_ts": r["updated_ts"],
            "access_count": r["access_count"] or 0,
            "status": r["status"] or "active",
            "created_by": r["created_by"] or "agent",
        }
    @staticmethod
    def _tags_list(tags) -> list:
        if not tags:
            return []
        if isinstance(tags, str):
            return [t.strip() for t in tags.split(",") if t.strip()]
        return [str(t).strip() for t in tags if str(t).strip()]
    async def note_create(self, user_id, content: str, *, title: str = None,
                          tags=None, source: str = None, created_by: str = "agent",
                          status: str = "active"):
        """Create a note; returns its id or None (anon/empty/over-cap/error).
        [[wikilinks]] in the body are parsed into the links column at write."""
        if self._anon_blocked(user_id):
            return None
        content = (content or "").strip()
        if not content:
            return None
        max_entries, max_chars = self._curated_caps()
        if len(content) > max_chars:
            return None
        norm = self._norm_user(user_id)
        try:
            rows = execute_retry(
                self.db_path,
                "SELECT COUNT(*) AS n FROM curated_memory WHERE user_id = ? "
                "AND COALESCE(status, 'active') IN ('active', 'pending')",
                (norm,), fetch="all")
            if rows and rows[0]["n"] >= max_entries:
                return None
            now = int(time.time())
            return execute_retry(
                self.db_path,
                "INSERT INTO curated_memory (user_id, content, title, tags, links, "
                "source, created_ts, updated_ts, access_count, status, created_by) "
                "VALUES (?,?,?,?,?,?,?,?,0,?,?)",
                (norm, content, (title or "").strip() or None,
                 json.dumps(self._tags_list(tags)),
                 json.dumps(parse_wikilinks(content)),
                 source, now, now, status, created_by),
                fetch="lastrowid")
        except Exception as e:
            logger.warning("note_create failed: %s", e)
            return None
    #: The status values a note may hold (schema comment on the column). A
    #: `pending` note comes from a forged/autonomous turn (see
    #: ``action_registration.py``) and stays dark until an owner promotes it to
    #: `active`; `archived` is the reject / soft-delete sink (A27).
    _NOTE_STATUSES = ("active", "pending", "archived")

    async def note_update(self, user_id, note_id, *, content: str = None,
                          title: str = None, tags=None, status: str = None) -> bool:
        """Update a note's content/title/tags/status (tenant-scoped). Content
        updates recompute links. ``status`` (A27) promotes a pending note to
        ``active`` or archives it — only ``active``|``pending``|``archived`` are
        accepted; an unknown value is refused. Returns False when the note isn't
        this tenant's (or the status is invalid)."""
        if self._anon_blocked(user_id):
            return False
        sets, args = ["updated_ts = ?"], [int(time.time())]
        if content is not None:
            content = content.strip()
            if not content:
                return False
            _, max_chars = self._curated_caps()
            if len(content) > max_chars:
                return False
            sets += ["content = ?", "links = ?"]
            args += [content, json.dumps(parse_wikilinks(content))]
        if title is not None:
            sets.append("title = ?"); args.append(title.strip() or None)
        if tags is not None:
            sets.append("tags = ?"); args.append(json.dumps(self._tags_list(tags)))
        if status is not None:
            if status not in self._NOTE_STATUSES:
                return False
            sets.append("status = ?"); args.append(status)
        try:
            n = execute_retry(
                self.db_path,
                f"UPDATE curated_memory SET {', '.join(sets)} "
                f"WHERE id = ? AND user_id = ?",
                tuple(args) + (note_id, self._norm_user(user_id)))
            return bool(n)
        except Exception as e:
            logger.warning("note_update failed: %s", e)
            return False
    async def note_archive(self, user_id, note_id) -> bool:
        """Archive (never delete) a note. Returns False when not this tenant's."""
        if self._anon_blocked(user_id):
            return False
        try:
            n = execute_retry(
                self.db_path,
                "UPDATE curated_memory SET status = 'archived', updated_ts = ? "
                "WHERE id = ? AND user_id = ?",
                (int(time.time()), note_id, self._norm_user(user_id)))
            return bool(n)
        except Exception as e:
            logger.warning("note_archive failed: %s", e)
            return False
    async def note_list(self, user_id, *, status: str = "active", tag: str = None,
                        limit: int = 200) -> list:
        """List this tenant's notes by status (newest updated first), optionally
        filtered by tag. [] on anon-block or error."""
        if self._anon_blocked(user_id):
            return []
        where = ["user_id = ?", "COALESCE(status, 'active') = ?"]
        args = [self._norm_user(user_id), status]
        if tag:
            where.append("tags LIKE ?")
            args.append(f'%{json.dumps(str(tag))[1:-1]}%')
        try:
            rows = execute_retry(
                self.db_path,
                f"SELECT {self._NOTE_FIELDS} FROM curated_memory "
                f"WHERE {' AND '.join(where)} "
                f"ORDER BY COALESCE(updated_ts, 0) DESC, id DESC LIMIT ?",
                tuple(args) + (max(1, min(1000, int(limit or 200))),), fetch="all")
        except Exception as e:
            logger.warning("note_list failed: %s", e)
            return []
        return [self._note_row_to_dict(r) for r in (rows or [])]
    async def note_get(self, user_id, note_id, *, bump_access: bool = True):
        """Fetch one note (tenant-scoped). By default bumps access_count — the
        AGENT-reuse signal the consolidation pass keys staleness on. Passive
        viewers (the read-only webview) pass ``bump_access=False`` so browsing
        the wiki never exempts an agent-unused note from the staleness archive.
        None when absent."""
        if self._anon_blocked(user_id):
            return None
        norm = self._norm_user(user_id)
        try:
            row = execute_retry(
                self.db_path,
                f"SELECT {self._NOTE_FIELDS} FROM curated_memory "
                f"WHERE id = ? AND user_id = ?",
                (note_id, norm), fetch="one")
            if row is None:
                return None
            out = self._note_row_to_dict(row)
            if bump_access:
                execute_retry(
                    self.db_path,
                    "UPDATE curated_memory SET access_count = COALESCE(access_count,0)+1 "
                    "WHERE id = ? AND user_id = ?",
                    (note_id, norm))
                out["access_count"] = (out["access_count"] or 0) + 1
            return out
        except Exception as e:
            logger.warning("note_get failed: %s", e)
            return None
    def consolidate_notes(self, *, stale_before_ts: int) -> dict:
        """Mechanical consolidation (C4), across ALL tenants, curator-tick only:

        - **stale**: archive ACTIVE agent-authored notes never read
          (access_count 0) and not updated since the cutoff. Owner-authored
          (created_by user/owner) and legacy rows (NULL created_by/updated_ts —
          unknowable) are exempt.
        - **dupes**: within (user_id, content) groups of active notes, archive the
          agent-authored copies, keeping the group's single oldest row (an
          owner-authored copy always survives).

        Archive-only (recoverable), fail-open. Returns
        ``{"archived_stale": [(user_id, id)...], "archived_dupes": [...]}`` so the
        curator can emit per-note audit events.
        """
        out = {"archived_stale": [], "archived_dupes": []}
        now = int(time.time())
        try:
            stale = execute_retry(
                self.db_path,
                "SELECT id, user_id FROM curated_memory "
                "WHERE COALESCE(status, 'active') = 'active' "
                "AND created_by IS NOT NULL AND created_by NOT IN ('user', 'owner') "
                "AND COALESCE(access_count, 0) = 0 "
                "AND updated_ts IS NOT NULL AND updated_ts < ?",
                (int(stale_before_ts),), fetch="all") or []
            for r in stale:
                execute_retry(
                    self.db_path,
                    "UPDATE curated_memory SET status = 'archived', updated_ts = ? "
                    "WHERE id = ?", (now, r["id"]))
                out["archived_stale"].append((r["user_id"], r["id"]))
        except Exception as e:
            logger.warning("consolidate_notes stale pass failed: %s", e)
        try:
            groups = execute_retry(
                self.db_path,
                "SELECT user_id, content, MIN(id) AS keep_id FROM curated_memory "
                "WHERE COALESCE(status, 'active') = 'active' "
                "GROUP BY user_id, content HAVING COUNT(*) > 1",
                fetch="all") or []
            for g in groups:
                dupes = execute_retry(
                    self.db_path,
                    "SELECT id, user_id FROM curated_memory "
                    "WHERE user_id = ? AND content = ? AND id != ? "
                    "AND COALESCE(status, 'active') = 'active' "
                    "AND created_by IS NOT NULL AND created_by NOT IN ('user', 'owner')",
                    (g["user_id"], g["content"], g["keep_id"]), fetch="all") or []
                for r in dupes:
                    execute_retry(
                        self.db_path,
                        "UPDATE curated_memory SET status = 'archived', updated_ts = ? "
                        "WHERE id = ?", (now, r["id"]))
                    out["archived_dupes"].append((r["user_id"], r["id"]))
        except Exception as e:
            logger.warning("consolidate_notes dupe pass failed: %s", e)
        return out
    async def note_backlinks(self, user_id, title: str) -> list:
        """Notes whose links list contains `title` (the wiki backlink set)."""
        if self._anon_blocked(user_id) or not (title or "").strip():
            return []
        try:
            rows = execute_retry(
                self.db_path,
                f"SELECT {self._NOTE_FIELDS} FROM curated_memory "
                f"WHERE user_id = ? AND links LIKE ? "
                f"AND COALESCE(status, 'active') != 'archived' ORDER BY id",
                (self._norm_user(user_id),
                 f'%{json.dumps(str(title).strip())}%'), fetch="all")
        except Exception as e:
            logger.warning("note_backlinks failed: %s", e)
            return []
        # LIKE over the JSON is a pre-filter; confirm against the parsed list.
        out = [self._note_row_to_dict(r) for r in (rows or [])]
        return [n for n in out if str(title).strip() in n["links"]]
    async def curated_remove(self, user_id, substring: str) -> int:
        """Delete this tenant's curated notes containing `substring`. Returns count."""
        if self._anon_blocked(user_id):
            return 0
        substring = (substring or "").strip()
        if not substring:
            return 0
        try:
            rows = execute_retry(
                self.db_path,
                "SELECT id FROM curated_memory WHERE user_id = ? AND content LIKE ?",
                (self._norm_user(user_id), f"%{substring}%"), fetch="all",
            )
            ids = [r["id"] for r in (rows or [])]
            for _id in ids:
                execute_retry(self.db_path,
                              "DELETE FROM curated_memory WHERE id = ?", (_id,))
            return len(ids)
        except Exception as e:
            logger.warning("curated_remove failed: %s", e)
            return 0
