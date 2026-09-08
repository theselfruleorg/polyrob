"""``/apps`` — the durable app service on the phone (032). Thin plumbing over
``core.app_service.owner_ops`` so every seat renders the same text."""
from typing import List


def apps_reply(user_id: str, data_dir: str, args: List[str]) -> str:
    from core.app_service import owner_ops
    from core.app_service.registry import AppServiceRegistry, default_app_services_db
    reg = AppServiceRegistry(default_app_services_db())
    sub = (args[0].lower() if args else "list")
    rest = args[1:]
    if sub in ("list", "ls"):
        return "\n".join(owner_ops.list_lines(reg, user_id))
    if sub in ("show", "approve", "reject", "kill", "logs") and not rest:
        return f"Usage: /apps {sub} <slug>"
    if sub == "show":
        return "\n".join(owner_ops.show_lines(reg, user_id, rest[0]))
    if sub == "approve":
        return owner_ops.approve(reg, rest[0], user_id, via="telegram")[1]
    if sub == "reject":
        return owner_ops.reject(reg, rest[0], user_id, via="telegram")[1]
    if sub == "kill":
        return owner_ops.kill(reg, rest[0], user_id, via="telegram")[1]
    if sub == "logs":
        n = 30
        if len(rest) > 1:
            try:
                n = max(1, min(200, int(rest[1])))
            except ValueError:
                pass
        return owner_ops.logs_tail(data_dir, user_id, rest[0], n)
    return "Usage: /apps [list|show|approve|reject|kill|logs <slug>]"
