from agents.task.agent.skill_validation import validate_authored, validate_consumed

def _errs(issues): return [i.code for i in issues if i.level == "error"]

def test_authored_strict_rejects_extra_top_level_and_name_mismatch():
    meta = {"name": "foo", "description": "d", "version": "1"}   # version is extra top-level
    e = _errs(validate_authored(meta, dirname="bar"))
    assert "extra_top_level_field" in e and "name_dir_mismatch" in e

def test_authored_accepts_clean_skill():
    meta = {"name": "foo", "description": "does X; use when Y", "license": "MIT",
            "metadata": {"polyrob-priority": "3"}}
    assert _errs(validate_authored(meta, dirname="foo")) == []

def test_consumed_is_lenient_warns_but_loads_on_name_mismatch():
    meta = {"name": "3d-modeling", "description": "d"}           # digit-leading, name!=dir
    issues = validate_consumed(meta, dirname="threed")
    assert _errs(issues) == []                                    # loadable
    assert any(i.level == "warn" for i in issues)

def test_consumed_skips_only_on_missing_description():
    assert _errs(validate_consumed({"name": "x"}, "x")) == ["missing_description"]
    assert _errs(validate_consumed({"name": "x", "description": ""}, "x")) == ["missing_description"]

# --- strict authored: one assertion per remaining error code (regression guard) ---

def test_authored_missing_name():
    assert "missing_name" in _errs(validate_authored({"description": "d"}, dirname="anything"))

def test_authored_name_too_long():
    name = "a" * 65   # valid kebab charset, so name_too_long is isolated
    assert "name_too_long" in _errs(validate_authored({"name": name, "description": "d"}, dirname=name))

def test_authored_name_charset():
    assert "name_charset" in _errs(validate_authored({"name": "Foo", "description": "d"}, dirname="Foo"))

def test_authored_description_too_long():
    meta = {"name": "foo", "description": "d" * 1025}
    assert "description_too_long" in _errs(validate_authored(meta, dirname="foo"))

def test_authored_compatibility_too_long():
    meta = {"name": "foo", "description": "d", "compatibility": "c" * 501}
    assert "compatibility_too_long" in _errs(validate_authored(meta, dirname="foo"))

def test_authored_metadata_not_string_map_bad_value():
    meta = {"name": "foo", "description": "d", "metadata": {"pri": 3}}   # non-string value
    assert "metadata_not_string_map" in _errs(validate_authored(meta, dirname="foo"))

def test_authored_metadata_not_string_map_bad_key():
    meta = {"name": "foo", "description": "d", "metadata": {3: "x"}}     # non-string key
    assert "metadata_not_string_map" in _errs(validate_authored(meta, dirname="foo"))

# --- type guards: non-string name/description must not crash len() (P0 minor #1) ---

def test_authored_type_guards_non_string_name_and_description_no_crash():
    issues = validate_authored({"name": 3, "description": []}, "x")   # must not raise
    errs = _errs(issues)
    assert "name_not_string" in errs
    assert "missing_description" in errs   # [] is falsy -> caught before the type check

def test_authored_non_string_description_nonempty_flagged():
    issues = validate_authored({"name": "foo", "description": ["not", "a", "string"]}, "foo")
    assert "description_not_string" in _errs(issues)

def test_consumed_type_guard_non_string_name_no_crash():
    issues = validate_consumed({"name": 3, "description": "d"}, "x")   # must not raise
    assert any(i.code == "name_not_string" for i in issues)
