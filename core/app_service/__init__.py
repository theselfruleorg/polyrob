"""032 — the durable app service.

The agent writes a tenant-scoped row through the ``app_service`` tool
(``tools/app_service``); the owner-owned supervisor (``polyrob apps
supervise`` → :mod:`core.app_service.supervisor`) turns rows into hardened
containers behind ``https://<slug>.<APP_SERVICE_BASE_DOMAIN>``. Nothing in this
package imports ``agents.*`` or ``tools.*`` (layering ratchet): the supervisor
runs in its own process and must never depend on the agent tree's runtime.
"""
