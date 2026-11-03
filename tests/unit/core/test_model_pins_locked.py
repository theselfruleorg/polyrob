"""098 CL-05: model_pins read-modify-write is locked across writers."""
import threading

from core import model_pins


def test_concurrent_set_alias_and_set_default_keep_both(tmp_path):
    home = str(tmp_path)
    errs = []

    def aliases():
        try:
            for i in range(20):
                model_pins.set_alias("u1", f"a{i}", "x/model", home_dir=home)
        except Exception as e:  # pragma: no cover
            errs.append(e)

    def defaults():
        try:
            for i in range(20):
                model_pins.set_default("u1", "openrouter", f"m{i}", home_dir=home)
        except Exception as e:  # pragma: no cover
            errs.append(e)
    t1, t2 = threading.Thread(target=aliases), threading.Thread(target=defaults)
    t1.start(); t2.start(); t1.join(); t2.join()
    assert not errs
    table, err = model_pins.aliases_read("u1", home_dir=home)
    assert not err and len(table) == 20
    assert model_pins.owner_default("u1", home_dir=home)["model"] == "m19"
