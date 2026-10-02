"""060 WS-8 — three owner knobs move from env flags into PREF_SCHEMA.

Owner directive (2026-09-10): "I should be able to do ANYTHING FROM THE CHAT."
The first batch is three owner-communication knobs; each env flag is retired in
the same commit, so the flag count falls (763 -> 760). An unset pref resolves to
the old env default, byte-identical.
"""
import pytest

from core import prefs
from core.flags_catalog import CATALOG

KNOBS = {
	"voice.transcript_echo": ("VOICE_TRANSCRIPT_ECHO", True),
	"stream.telegram": ("TELEGRAM_INCREMENTAL_STREAM", False),
	"delivery.lifecycle_daily_cap": ("USER_DELIVERY_LIFECYCLE_DAILY_CAP", 10),
}


@pytest.fixture
def home(tmp_path, monkeypatch):
	monkeypatch.setattr("core.runtime_paths.prefs_home_dir", lambda *a, **k: str(tmp_path))
	monkeypatch.setattr("core.instance.resolve_owner_principal", lambda *a, **k: "owner1")
	monkeypatch.setenv("POLYROB_INSTANCE_ID", "rob")
	return tmp_path


def test_the_knobs_are_prefs_and_the_flags_are_gone():
	names = {row[0] for row in CATALOG}
	for key, (flag, default) in KNOBS.items():
		spec = prefs.PREF_SCHEMA[key]
		assert spec.env_flag is None and spec.default_display == default
		assert spec.sensitivity == prefs.SENSITIVITY_SAFE
		assert flag not in names, f"{flag} must be retired from the catalog"


def test_unset_pref_is_the_old_default_and_env_is_ignored(home, monkeypatch):
	from core.surfaces.config import SurfaceConfig
	monkeypatch.setenv("VOICE_TRANSCRIPT_ECHO", "false")      # retired: no effect
	monkeypatch.setenv("TELEGRAM_INCREMENTAL_STREAM", "true")  # retired: no effect
	assert SurfaceConfig.voice_transcript_echo_enabled() is True
	assert SurfaceConfig.telegram_incremental_stream() is False


def test_the_owner_sets_them_from_chat(home):
	from core.surfaces.config import SurfaceConfig
	from core.surfaces.user_delivery import _lifecycle_daily_cap
	for key, value in (("voice.transcript_echo", False), ("stream.telegram", True),
	                   ("delivery.lifecycle_daily_cap", 3)):
		ok = prefs.write_preference(str(home), "owner1", key, value, instance_id="rob")
		assert getattr(ok, "ok", ok) not in (False, None), (key, ok)
	assert SurfaceConfig.voice_transcript_echo_enabled() is False
	assert SurfaceConfig.telegram_incremental_stream() is True
	assert _lifecycle_daily_cap("owner1", str(home)) == 3
	assert _lifecycle_daily_cap("someone-else", str(home)) == 10


def test_an_unreadable_prefs_home_falls_back_to_the_default(monkeypatch):
	from core.surfaces.config import SurfaceConfig

	def boom(*a, **k):
		raise OSError("no home")
	monkeypatch.setattr("core.runtime_paths.prefs_home_dir", boom)
	assert prefs.owner_pref_scope() == (None, "")
	assert SurfaceConfig.voice_transcript_echo_enabled() is True
	assert SurfaceConfig.telegram_incremental_stream() is False
