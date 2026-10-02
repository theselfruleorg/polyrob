"""060 WS-8 test helper: make ``core.prefs.resolve`` return an owner pref for
one key without touching a prefs file. Repeated calls in one test merge."""


def set_owner_pref(monkeypatch, key, value):
	import core.prefs as P
	current = P.resolve
	orig = getattr(current, "_orig", current)
	ovr = dict(getattr(current, "_ovr", {}))
	ovr[key] = value

	def fake(k, *args, **kwargs):
		return ovr[k] if k in ovr else orig(k, *args, **kwargs)

	fake._ovr = ovr
	fake._orig = orig
	monkeypatch.setattr(P, "resolve", fake)
