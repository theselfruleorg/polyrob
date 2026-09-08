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
"""
import asyncio
import logging
import os
import shutil
import time
import urllib.request
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from core.app_service.config import app_dir, logs_path, safe_tenant
from core.app_service.egress import EgressApplier, resolve_allowlist
from core.app_service.nginx import NginxApplier, render_stanza
from core.app_service.registry import (
    STATUS_APPROVED, STATUS_DEPLOYING, STATUS_LIVE, STATUS_PAUSED, STATUS_STOPPED,
    AppServiceRegistry,
)
from core.app_service.snapshot import snapshot_tree
from core.autonomy_control import allows
from core.container_hardening import hardening_flags
from core.event_kinds import APP_FAILED, APP_LIVE, APP_STOPPED

logger = logging.getLogger(__name__)

APP_LABEL = "polyrob.app=1"
_MAX_UNHEALTHY_TICKS = 3


class DeployError(RuntimeError):
    """A deploy step failed; the row goes ``failed`` and everything is torn down."""


async def _default_http_probe(url: str, timeout: float) -> bool:
    def _get() -> bool:
        with urllib.request.urlopen(url, timeout=timeout) as resp:  # nosec — loopback only
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
                await self._teardown(row)
                self.registry.record_stopped(row["slug"], row["user_id"])
                counts["stopped"] += 1
        for row in self.registry.list_by_status((STATUS_APPROVED,)):
            if not self.registry.claim(row["slug"], row["user_id"], STATUS_APPROVED, STATUS_DEPLOYING):
                continue
            if await self.deploy_one(row):
                counts["deployed"] += 1
            else:
                counts["failed"] += 1
        for row in self.registry.list_by_status((STATUS_LIVE,)):
            if await self.check_one(row):
                counts["healthy"] += 1
            else:
                counts["unhealthy"] += 1
            await self._refresh_logs(row)
        self._prune_snapshots()
        return counts

    # --- deploy ------------------------------------------------------------

    def _resolve_source(self, row: Dict[str, Any]) -> str:
        src = os.path.realpath(str(row.get("source_dir") or ""))
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
            src = self._resolve_source(row)
            digest = str(row.get("workspace_digest") or "nodigest")[:12]
            snap_dir = os.path.join(app_dir(self.data_dir, uid, slug), "snapshots", digest)
            total, files, skipped = snapshot_tree(src, snap_dir, max_mb=self.snapshot_max_mb)
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
            return True
        except Exception as e:  # every failure is a typed, visible row state
            tail = await self._logs_tail(name, 20)
            self.registry.record_failed(slug, uid, error=str(e)[:500])
            await self._teardown(row)
            self._emit(APP_FAILED, row, {"error": str(e)[:300], "log_tail": tail[-1500:]})
            logger.warning("app_service: deploy of %s/%s failed: %s", uid, slug, e)
            return False

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
        name = row.get("container_name") or self.container_name(row)
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
        name = row.get("container_name") or self.container_name(row)
        try:
            text = await self._logs_tail(name, self.log_tail)
            path = logs_path(self.data_dir, row["user_id"], row["slug"])
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(text)
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
        name = row.get("container_name") or self.container_name(row)
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
        for row in self.registry.list_by_status((STATUS_STOPPED, "failed")):
            base = os.path.join(app_dir(self.data_dir, row["user_id"], row["slug"]), "snapshots")
            if not os.path.isdir(base):
                continue
            for d in os.listdir(base):
                full = os.path.join(base, d)
                try:
                    if os.path.isdir(full) and os.path.getmtime(full) < cutoff:
                        shutil.rmtree(full, ignore_errors=True)
                except OSError:
                    continue

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
