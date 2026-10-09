"""032 — the owner-owned reconcile loop (``polyrob apps supervise``).

Turns registry rows into hardened containers and back. Runs in ITS OWN process
(``polyrob-apps.service``) with the docker/nginx/nft privilege the agent never
holds; the agent only writes rows. Every privileged input is validated twice
(the registry on the way in, the renderers on the way out) and no agent text is
ever executed through a shell: ``cmd`` is an argv list, the stanza has two
substitutions, the nft script has two.

Every tick also RE-ASSERTS each live app's egress rules (``reconcile_egress``):
nft rules are ephemeral kernel state while containers carry ``--restart
unless-stopped``, so a reboot or a firewall flush would otherwise bring an app
back with no outbound restriction while the health probe still said "live".

Consults the 031 pause record every tick (``allows("app_serve")``): on the
paused edge every live container is stopped and its row parked ``paused``; the
first allowed tick moves it back to ``approved`` and redeploys from the same
snapshot. Nothing here imports ``agents.*`` or ``tools.*`` (layering).

H07 — this process is ROOT and the registry is group-writable, so no row field
is trusted: ``slug`` / ``user_id`` / ``workspace_digest`` / ``host_port`` are
re-validated before any filesystem op (:func:`core.app_service.config.
row_field_error`), an ``approved`` row must carry an owner-seat MAC over its
current configuration (:mod:`core.app_service.approval`) or it goes back to
``pending``, the container name is always DERIVED (never read from the row),
and every write under ``<data>/apps`` is descriptor-relative and no-follow
(:func:`core.app_service.snapshot.open_owned_dir`).
"""
import asyncio
import logging
import os
import secrets
import shutil
import stat
import time
import urllib.request
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from core.app_service.config import app_dir, apps_root, row_field_error, safe_tenant
from core.app_service.egress import EgressApplier, resolve_allowlist
from core.app_service.nginx import NginxApplier, render_stanza
from core.app_service.registry import (
    STATUS_APPROVED, STATUS_DEPLOYING, STATUS_FAILED, STATUS_LIVE, STATUS_PAUSED,
    STATUS_PENDING, STATUS_STOPPED, AppServiceRegistry,
)
from core.app_service.snapshot import open_owned_dir, snapshot_tree
from core.autonomy_control import allows
from core.container_hardening import hardening_flags
from core.event_kinds import APP_FAILED, APP_LIVE, APP_STOPPED

logger = logging.getLogger(__name__)

APP_LABEL = "polyrob.app=1"
_MAX_UNHEALTHY_TICKS = 3


class DeployError(RuntimeError):
    """A deploy step failed; the row goes ``failed`` and everything is torn down."""


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """A health probe answers for the app's OWN loopback port; a 3xx is an
    answer, never an instruction to fetch somewhere else as root."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D401
        return None


_PROBE_OPENER = urllib.request.build_opener(_NoRedirect)


async def _default_http_probe(url: str, timeout: float) -> bool:
    def _get() -> bool:
        with _PROBE_OPENER.open(url, timeout=timeout) as resp:  # nosec — loopback only
            return 200 <= int(getattr(resp, "status", 200)) < 300
    try:
        return bool(await asyncio.to_thread(_get))
    except Exception:
        return False


class AppSupervisor:
    def __init__(
        self,
        registry: AppServiceRegistry,
        *,
        data_dir: str,
        base_domain: str,
        allow_public: bool,
        docker_runner: Callable,
        sys_runner: Callable,
        nginx: NginxApplier,
        egress: EgressApplier,
        image: str,
        memory_mb: int,
        cpus: float,
        pids: int,
        snapshot_max_mb: int,
        port_range: Tuple[int, int],
        health_timeout_sec: int,
        cert_dir: str,
        source_roots: Sequence[str],
        allows_fn: Optional[Callable[[str], Any]] = None,
        http_probe: Optional[Callable] = None,
        resolver: Optional[Callable] = None,
        now: Callable[[], float] = time.time,
        retain_days: int = 7,
        log_tail: int = 200,
        poll_sec: float = 3.0,
        event_log: Any = None,
    ):
        self.registry = registry
        self.data_dir = data_dir
        self.base_domain = (base_domain or "").strip().lower()
        self.allow_public = bool(allow_public)
        self._docker = docker_runner
        self._sys = sys_runner
        self.nginx = nginx
        self.egress = egress
        self.image = image
        self.memory_mb = int(memory_mb)
        self.cpus = float(cpus)
        self.pids = int(pids)
        self.snapshot_max_mb = int(snapshot_max_mb)
        self.port_range = (int(port_range[0]), int(port_range[1]))
        self.health_timeout_sec = int(health_timeout_sec)
        self.cert_dir = cert_dir
        self.source_roots = [os.path.realpath(r) for r in source_roots if r]
        self._allows = allows_fn or allows
        self._probe = http_probe or _default_http_probe
        self._resolver = resolver
        self._now = now
        self.retain_days = int(retain_days)
        self.log_tail = int(log_tail)
        self.poll_sec = float(poll_sec)
        self._event_log = event_log

    # --- naming ------------------------------------------------------------

    def container_name(self, row: Dict[str, Any]) -> str:
        return f"polyrob-app-{safe_tenant(row['user_id'])}-{row['slug']}"

    def _unsafe(self, row: Dict[str, Any]) -> Optional[str]:
        """Why this row must not drive a privileged op, or ``None`` (H07)."""
        err = row_field_error(row)
        if err:
            return err
        hp = row.get("host_port")
        if hp is not None:
            lo, hi = self.port_range
            if not isinstance(hp, int) or not (lo <= hp <= hi):
                return f"host_port {hp!r} outside APP_SERVICE_PORT_RANGE"
        hpath = row.get("health_path") or "/"
        if not isinstance(hpath, str) or not hpath.startswith("/") or len(hpath) > 512:
            return "invalid health_path"
        return None

    def _refuse_row(self, row: Dict[str, Any], why: str) -> None:
        """A tampered/malformed row: record it, never act on its fields."""
        logger.warning("app_service: refusing registry row: %s", why)
        try:
            self.registry.set_status(row["slug"], row["user_id"], STATUS_FAILED,
                                     error=f"refused by supervisor: {why}")
        except Exception:
            logger.debug("app_service: could not mark refused row", exc_info=True)

    def network_name(self, row: Dict[str, Any]) -> str:
        return self.container_name(row)

    def allocate_port(self) -> int:
        used = self.registry.used_host_ports()
        lo, hi = self.port_range
        for p in range(lo, hi + 1):
            if p not in used:
                return p
        raise DeployError(f"APP_SERVICE_PORT_RANGE {lo}-{hi} exhausted")

    # --- the tick ----------------------------------------------------------

    async def tick(self) -> Dict[str, int]:
        counts = dict(paused=0, resumed=0, deployed=0, failed=0, stopped=0, healthy=0,
                      unhealthy=0)
        dec = self._allows("app_serve")
        if not getattr(dec, "allowed", False):
            reason = getattr(dec, "reason", "paused")
            for row in self.registry.list_by_status((STATUS_LIVE, STATUS_DEPLOYING)):
                why = self._unsafe(row)
                if why:
                    self._refuse_row(row, why)
                    continue
                await self._teardown(row)
                self.registry.set_status(row["slug"], row["user_id"], STATUS_PAUSED,
                                         error=f"paused: {reason}")
                self._emit(APP_STOPPED, row, {"by": "pause", "reason": str(reason)[:200]})
                counts["paused"] += 1
            return counts
        for row in self.registry.list_by_status((STATUS_PAUSED,)):
            self.registry.set_status(row["slug"], row["user_id"], STATUS_APPROVED)
            counts["resumed"] += 1
        for row in self.registry.list_by_status((STATUS_STOPPED,)):
            if row.get("container_name"):
                why = self._unsafe(row)
                if why:
                    self._refuse_row(row, why)
                    continue
                await self._teardown(row)
                self.registry.record_stopped(row["slug"], row["user_id"])
                counts["stopped"] += 1
        for row in self.registry.list_by_status((STATUS_APPROVED,)):
            why = self._unsafe(row)
            if why:
                self._refuse_row(row, why)
                counts["failed"] += 1
                continue
            unsigned = self.registry.verify_approval(row)
            if unsigned:
                # Not an owner decision (or the config moved since one): back to
                # the owner ask, never deployed.
                logger.warning("app_service: %s/%s approval refused: %s",
                               row["user_id"], row["slug"], unsigned)
                self.registry.set_status(row["slug"], row["user_id"], STATUS_PENDING,
                                         error=unsigned)
                counts["failed"] += 1
                continue
            if not self.registry.claim(row["slug"], row["user_id"], STATUS_APPROVED, STATUS_DEPLOYING):
                continue
            if await self.deploy_one(row):
                counts["deployed"] += 1
            else:
                counts["failed"] += 1
        for row in self.registry.list_by_status((STATUS_LIVE,)):
            why = self._unsafe(row)
            if why:
                self._refuse_row(row, why)
                counts["unhealthy"] += 1
                continue
            if await self.check_one(row):
                counts["healthy"] += 1
            else:
                counts["unhealthy"] += 1
            await self._refresh_logs(row)
        self._prune_snapshots()
        return counts

    # --- deploy ------------------------------------------------------------

    def _resolve_source(self, row: Dict[str, Any]) -> str:
        approved = os.path.normpath(str(row.get("source_dir") or ""))
        src = os.path.realpath(approved)
        if not os.path.isabs(approved) or src != approved:
            # The tool records the resolved directory and the owner MAC covers
            # that string; a path that now resolves elsewhere is a swap (IO-A1).
            raise DeployError(f"app dir {approved} no longer resolves to itself (symlink in the path)")
        if not any(src == root or src.startswith(root + os.sep) for root in self.source_roots):
            raise DeployError(f"app dir {src} is outside the allowed source roots")
        if not os.path.isdir(src):
            raise DeployError(f"app dir {src} does not exist")
        return src

    async def deploy_one(self, row: Dict[str, Any]) -> bool:
        slug, uid = row["slug"], row["user_id"]
        name, net = self.container_name(row), self.network_name(row)
        row = dict(row, container_name=name)
        try:
            why = self._unsafe(row)
            if why:
                raise DeployError(f"refused by supervisor: {why}")
            src = self._resolve_source(row)
            digest = str(row["workspace_digest"])[:12]
            snap_dir, total, files, skipped = self._snapshot(row, src, digest)
            from core.ship_tree import tree_digest
            if not tree_digest(snap_dir).startswith(str(row["workspace_digest"])):
                # ship == tested: publish only the bytes the tests ran on.
                raise DeployError("the snapshot does not match the tested workspace digest "
                                  "— run the tests and request the deploy again")
            host_port = int(row["host_port"]) if row.get("host_port") else self.allocate_port()
            await self._docker(["rm", "-f", name], timeout=60)
            code, _out, err = await self._docker(["network", "create", "--driver", "bridge", net],
                                                 timeout=60)
            if code != 0 and "already exists" not in (err or ""):
                raise DeployError(f"docker network create failed: {(err or '').strip()[:200]}")
            code, out, err = await self._docker(
                ["network", "inspect", "-f", "{{(index .IPAM.Config 0).Subnet}}", net], timeout=30)
            if code != 0 or not (out or "").strip():
                raise DeployError(f"docker network inspect failed: {(err or '').strip()[:200]}")
            subnet = out.strip()
            allow = await resolve_allowlist(row.get("egress_allow") or [], resolver=self._resolver)
            ok, e = await self.egress.apply(slug=slug, subnet=subnet, mode=row["egress"],
                                            allow_addrs=allow.addrs)
            if not ok:
                raise DeployError(f"egress rules failed: {e}")
            if allow.refused:
                logger.warning("app_service: %s/%s egress allow-hosts refused: %s",
                               uid, slug, ", ".join(allow.refused))
            argv = ["run", "-d", "--name", name, "--label", APP_LABEL,
                    "--label", f"polyrob.tenant={safe_tenant(uid)}", "--label", f"polyrob.slug={slug}",
                    "--restart", "unless-stopped",
                    "-p", f"127.0.0.1:{host_port}:{int(row['container_port'])}"]
            argv += hardening_flags(
                network=net, workdir_host=snap_dir, pids_limit=self.pids, memory_mb=self.memory_mb,
                shm_size_mb=64, cpus=self.cpus, user="65534:65534", mount_target="/app",
                read_only_mount=True, workdir="/app")
            for k, v in (row.get("env") or {}).items():
                argv += ["-e", f"{k}={v}"]
            argv += ["-e", f"PORT={int(row['container_port'])}"] if "PORT" not in (row.get("env") or {}) else []
            argv += [self.image] + list(row["cmd"])
            code, _out, err = await self._docker(argv, timeout=180)
            if code != 0:
                raise DeployError(f"docker run failed: {(err or '').strip()[:300]}")
            healthy = await self._wait_healthy(host_port, row.get("health_path") or "/")
            if not healthy:
                raise DeployError(
                    f"health check failed: http://127.0.0.1:{host_port}{row.get('health_path') or '/'} "
                    f"did not answer 2xx within {self.health_timeout_sec}s")
            public_url = f"http://127.0.0.1:{host_port}"
            if self.allow_public and self.base_domain:
                text = render_stanza(slug=slug, host_port=host_port, base_domain=self.base_domain,
                                     cert_dir=self.cert_dir)
                ok, e = await self.nginx.apply(slug, text)
                if not ok:
                    raise DeployError(f"nginx refused the stanza: {e}")
                public_url = f"https://{slug}.{self.base_domain}"
            self.registry.record_live(slug, uid, host_port=host_port, container_name=name,
                                      public_url=public_url)
            self._link_artifact(uid, src, public_url, row.get("cmd"))
            live_attrs = {"url": public_url, "digest": digest, "files": files,
                          "bytes": total, "skipped": len(skipped)}
            if allow.refused:
                live_attrs["egress_refused"] = allow.refused[:10]
            self._emit(APP_LIVE, row, live_attrs)
            # 033: going live IS the outward act — the agent only wrote a registry
            # row; this process made it reachable. Its direct effect seam (the
            # supervisor has no Controller and may import only core.*).
            from core.effects import record_external_write
            record_external_write(
                effect="public", tool="app_service", action="app_serve_live",
                target="open" if public_url.startswith("https://") else "unknown",
                surface="supervisor", autonomous=True, confidence="action",
                outcome="ok", user_id=str(uid or ""), fingerprint=public_url,
                event_log=self._event_log)
            return True
        except Exception as e:  # every failure is a typed, visible row state
            tail = await self._logs_tail(name, 20)
            self.registry.record_failed(slug, uid, error=str(e)[:500])
            if not self._unsafe(row):
                await self._teardown(row)
            self._emit(APP_FAILED, row, {"error": str(e)[:300], "log_tail": tail[-1500:]})
            logger.warning("app_service: deploy of %s/%s failed: %s", uid, slug, e)
            return False

    def _snapshot(self, row: Dict[str, Any], src: str, digest: str):
        """Copy the tested tree to ``<apps>/<tenant>/<slug>/snapshots/<digest>``
        through descriptors only, then prove the PATH docker will mount resolves
        to the directory just written, inside the apps root (H07)."""
        parts = ["apps", safe_tenant(row["user_id"]), row["slug"], "snapshots"]
        snap_dir = os.path.join(app_dir(self.data_dir, row["user_id"], row["slug"]),
                                "snapshots", digest)
        fd = open_owned_dir(self.data_dir, parts)
        try:
            total, files, skipped = snapshot_tree(src, digest, max_mb=self.snapshot_max_mb,
                                                  parent_fd=fd)
            st_fd = os.stat(digest, dir_fd=fd, follow_symlinks=False)
        finally:
            os.close(fd)
        root = os.path.realpath(apps_root(self.data_dir))
        real = os.path.realpath(snap_dir)
        if not real.startswith(root + os.sep):
            raise DeployError("snapshot dir escapes the apps root")
        st_path = os.stat(real)
        if (st_path.st_dev, st_path.st_ino) != (st_fd.st_dev, st_fd.st_ino) \
                or not stat.S_ISDIR(st_path.st_mode):
            raise DeployError("snapshot dir changed underneath the supervisor")
        return real, total, files, skipped

    async def _wait_healthy(self, host_port: int, health_path: str) -> bool:
        url = f"http://127.0.0.1:{host_port}{health_path}"
        deadline = self._now() + self.health_timeout_sec
        while True:
            if await self._probe(url, 5.0):
                return True
            if self._now() >= deadline:
                return False
            await asyncio.sleep(self.poll_sec)

    # --- live checks -------------------------------------------------------

    async def reconcile_egress(self, row: Dict[str, Any]) -> Tuple[bool, str]:
        """Re-assert one live app's egress rules. Idempotent and cheap: the
        table is listed first and re-applied only when the kernel has lost it.

        nft rules are EPHEMERAL kernel state while the container carries
        ``--restart unless-stopped``, so after a host reboot or a ``docker``
        daemon restart the app comes back with no egress restriction at all.
        Nothing else re-applies them — ``deploy_one`` runs once. Called from
        :meth:`check_one` for every ``live`` row on every tick, the first tick
        being the supervisor's boot reconcile; ``deploying`` rows are mid-deploy
        (``deploy_one`` applies the rules itself) and ``stopped`` / ``paused`` /
        ``failed`` rows have their container torn down on that edge."""
        slug = row["slug"]
        mode = row.get("egress") or "none"
        if mode == "open":
            return True, ""  # approved with no restriction: no table to assert
        try:
            if await self.egress.table_present(slug):
                return True, ""
        except Exception as e:
            return False, f"cannot read the nft table: {e}"
        net = self.network_name(row)
        code, out, err = await self._docker(
            ["network", "inspect", "-f", "{{(index .IPAM.Config 0).Subnet}}", net], timeout=30)
        if code != 0 or not (out or "").strip():
            return False, f"docker network inspect failed: {(err or '').strip()[:200]}"
        allow = await resolve_allowlist(row.get("egress_allow") or [], resolver=self._resolver)
        ok, e = await self.egress.apply(slug=slug, subnet=out.strip(), mode=mode,
                                        allow_addrs=allow.addrs)
        if not ok:
            return False, f"nft re-apply failed: {e}"
        logger.warning("app_service: re-applied missing egress rules for %s/%s%s",
                       row["user_id"], slug,
                       (" (allow-hosts refused: " + ", ".join(allow.refused) + ")")
                       if allow.refused else "")
        return True, ""

    async def check_one(self, row: Dict[str, Any]) -> bool:
        slug, uid = row["slug"], row["user_id"]
        name = self.container_name(row)
        code, out, _err = await self._docker(["inspect", "-f", "{{.State.Running}}", name], timeout=30)
        if code != 0 or (out or "").strip().lower() != "true":
            self.registry.record_failed(slug, uid, error="container not running")
            await self._teardown(dict(row, container_name=name))
            self._emit(APP_FAILED, row, {"error": "container not running"})
            return False
        egress_ok, egress_err = await self.reconcile_egress(row)
        if not egress_ok:
            # An app whose egress rules are not asserted is NOT healthy, whatever
            # the health endpoint says — it is a publicly reachable container with
            # unknown outbound reach. Say so on the row + the event stream and let
            # the breaker open on the third tick.
            error = f"egress rules not asserted: {egress_err}"
            self.registry.record_health(slug, uid, False)
            self.registry.set_status(slug, uid, STATUS_LIVE, error=error)
            self._emit(APP_FAILED, row, {"error": error})
            await self._open_breaker_if_exhausted(row, name, error)
            return False
        ok = await self._probe(f"http://127.0.0.1:{row['host_port']}{row.get('health_path') or '/'}", 5.0)
        self.registry.record_health(slug, uid, ok)
        if ok:
            return True
        await self._open_breaker_if_exhausted(row, name, "health checks failed")
        return False

    async def _open_breaker_if_exhausted(self, row: Dict[str, Any], name: str,
                                         reason: str) -> None:
        """Third consecutive bad tick (unhealthy OR unprotected) opens the
        circuit: the row goes ``failed`` and everything is torn down until the
        agent redeploys."""
        fresh = self.registry.get(row["slug"], row["user_id"]) or row
        if int(fresh.get("consecutive_failures") or 0) < _MAX_UNHEALTHY_TICKS:
            return
        self.registry.record_failed(
            row["slug"], row["user_id"],
            error=f"{reason} on {_MAX_UNHEALTHY_TICKS} consecutive ticks")
        await self._teardown(dict(row, container_name=name))
        self._emit(APP_FAILED, row, {"error": f"{reason} for {_MAX_UNHEALTHY_TICKS} ticks"})

    async def _refresh_logs(self, row: Dict[str, Any]) -> None:
        """``<apps>/<tenant>/<slug>/logs.txt``, written as a fresh O_EXCL temp
        then renamed over — descriptor-relative, never through a symlink (H07)."""
        name = self.container_name(row)
        try:
            text = await self._logs_tail(name, self.log_tail)
            fd = open_owned_dir(self.data_dir, ["apps", safe_tenant(row["user_id"]), row["slug"]])
            try:
                tmp = f".logs.{secrets.token_hex(8)}.tmp"
                out = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o640,
                              dir_fd=fd)
                try:
                    os.fchmod(out, 0o640)
                    os.write(out, text.encode("utf-8", errors="replace"))
                finally:
                    os.close(out)
                os.replace(tmp, "logs.txt", src_dir_fd=fd, dst_dir_fd=fd)
            finally:
                os.close(fd)
        except Exception:
            logger.debug("app_service: log refresh failed for %s", name, exc_info=True)

    async def _logs_tail(self, name: str, lines: int) -> str:
        try:
            code, out, err = await self._docker(["logs", "--tail", str(int(lines)), name], timeout=30)
        except Exception:
            return ""
        if code != 0:
            return ""
        return (out or "") + (("\n[stderr]\n" + err) if err else "")

    # --- teardown / pruning ------------------------------------------------

    async def _teardown(self, row: Dict[str, Any]) -> None:
        slug = row["slug"]
        name = self.container_name(row)  # derived, never the row's own text
        net = self.network_name(row)
        for step in (
            lambda: self._docker(["rm", "-f", name], timeout=60),
            lambda: self.nginx.remove(slug),
            lambda: self.egress.remove(slug),
            lambda: self._docker(["network", "rm", net], timeout=60),
        ):
            try:
                await step()
            except Exception:
                logger.debug("app_service: teardown step failed for %s", name, exc_info=True)

    def _prune_snapshots(self) -> None:
        if self.retain_days <= 0:
            return
        cutoff = self._now() - self.retain_days * 86400
        for row in self.registry.list_by_status((STATUS_STOPPED, STATUS_FAILED)):
            if self._unsafe(row):
                continue
            parts = ["apps", safe_tenant(row["user_id"]), row["slug"], "snapshots"]
            try:
                fd = open_owned_dir(self.data_dir, parts, create=False)
            except OSError:
                continue
            try:
                for d in os.listdir(fd):
                    try:
                        st = os.stat(d, dir_fd=fd, follow_symlinks=False)
                        if stat.S_ISDIR(st.st_mode) and st.st_mtime < cutoff:
                            shutil.rmtree(d, dir_fd=fd, ignore_errors=True)
                    except OSError:
                        continue
            finally:
                os.close(fd)

    # --- bookkeeping -------------------------------------------------------

    def _link_artifact(self, user_id: str, src: str, url: str,
                       cmd: Optional[Sequence[str]] = None) -> None:
        """Close the 021 loop: every ledger row under the app dir (the files the
        agent built) plus the entry file(s) named by ``cmd`` get the URL, so the
        completion notice hands the owner a link. The ledger records FILES only
        (an artifact is a thing on disk), hence no row for the dir itself. Fail-open."""
        try:
            from core.artifacts import get_artifact_ledger, record_artifact
            from core.sqlite_util import execute_retry
            src_real = os.path.realpath(src)
            for tok in list(cmd or []):
                cand = os.path.realpath(os.path.join(src_real, str(tok)))
                if cand.startswith(src_real + os.sep) and os.path.isfile(cand):
                    record_artifact(user_id, cand, kind="app")
            ledger = get_artifact_ledger()
            execute_retry(
                ledger.db_path,
                "UPDATE artifacts SET url=? WHERE user_id=? AND path LIKE ?",
                (url, user_id, src_real + os.sep + "%"))
        except Exception:
            logger.debug("app_service: artifact link skipped", exc_info=True)

    def _emit(self, kind: str, row: Dict[str, Any], attrs: Dict[str, Any]) -> None:
        try:
            log = self._event_log
            if log is None:
                from core.event_log import event_log_enabled, get_event_log
                if not event_log_enabled():
                    return
                log = get_event_log()
            log.record(kind, user_id=row["user_id"], source="app_supervisor",
                       attrs={"slug": row["slug"], **(attrs or {})})
        except Exception:
            logger.debug("app_service: event emit skipped", exc_info=True)


async def run_forever(supervisor: AppSupervisor, interval_sec: int, *, once: bool = False,
                      stop_event: Optional[asyncio.Event] = None) -> None:
    """The unit's loop: tick, sleep, repeat; SIGTERM-clean via *stop_event*."""
    while True:
        try:
            counts = await supervisor.tick()
            if any(counts.values()):
                logger.info("app_service tick: %s", counts)
        except Exception:
            logger.warning("app_service tick failed", exc_info=True)
        if once:
            return
        if stop_event is not None:
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=interval_sec)
                return
            except asyncio.TimeoutError:
                continue
        await asyncio.sleep(interval_sec)
