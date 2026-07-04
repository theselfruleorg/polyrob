import ast
import pathlib

FORBIDDEN_PREFIXES = ("modules.x402", "modules.payments", "api.")


def test_core_wallet_imports_no_server_tier_module():
    root = pathlib.Path("core/wallet")
    for py in root.glob("*.py"):
        tree = ast.parse(py.read_text())
        for node in ast.walk(tree):
            mod = None
            if isinstance(node, ast.ImportFrom):
                mod = node.module or ""
            elif isinstance(node, ast.Import):
                mod = node.names[0].name if node.names else ""
            if mod and mod.startswith(FORBIDDEN_PREFIXES):
                raise AssertionError(f"{py} imports server-tier module {mod}")
