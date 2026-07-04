import inspect
import core.bootstrap as bootstrap


def test_build_cli_container_source_sets_rob_local():
    src = inspect.getsource(bootstrap.build_cli_container)
    assert "POLYROB_LOCAL" in src, "build_cli_container must set POLYROB_LOCAL for the terminal profile"
