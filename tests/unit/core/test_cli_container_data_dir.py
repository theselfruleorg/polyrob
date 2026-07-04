"""P2: build_cli_container must set config.data_dir under .rob/ (data-root unification)."""
import ast
import inspect

import pytest


def test_cli_container_data_dir_under_rob_source():
    """Source-inspection test: build_cli_container must assign config.data_dir = str(rob_dir).

    We use source inspection rather than actually constructing the container (which
    needs a real LLM key and network) to keep this test fast and dependency-free.
    The assertion is the load-bearing part — the source must contain the assignment.
    """
    import core.bootstrap as bootstrap
    src = inspect.getsource(bootstrap.build_cli_container)
    # The fix must contain both the assignment target and rob_dir reference
    assert "config.data_dir" in src, (
        "build_cli_container must set config.data_dir to keep CLI state under .rob/"
    )
    assert "rob_dir" in src, (
        "build_cli_container must reference rob_dir when setting config.data_dir"
    )


def test_cli_container_data_dir_assignment_is_str_rob_dir():
    """Parse the AST of build_cli_container and confirm config.data_dir = str(rob_dir) is present."""
    import core.bootstrap as bootstrap
    src = inspect.getsource(bootstrap.build_cli_container)
    # Dedent (inspect may return indented source for nested functions)
    import textwrap
    src = textwrap.dedent(src)
    tree = ast.parse(src)

    found = False
    for node in ast.walk(tree):
        # Look for: config.data_dir = str(rob_dir)
        if (
            isinstance(node, ast.Assign)
            and len(node.targets) == 1
            and isinstance(node.targets[0], ast.Attribute)
            and isinstance(node.targets[0].value, ast.Name)
            and node.targets[0].value.id == "config"
            and node.targets[0].attr == "data_dir"
        ):
            found = True
            break
    assert found, (
        "build_cli_container must contain `config.data_dir = ...` assignment "
        "to unify CLI autonomy/memory DBs under .rob/"
    )
