from core.surfaces.media import Media, coerce_media


def test_media_defaults():
    m = Media(kind="voice")
    assert m.kind == "voice" and m.data is None and m.url is None


def test_coerce_media_from_dicts_and_objects():
    out = coerce_media([{"kind": "image", "url": "http://x/y.jpg"}, Media(kind="voice", data=b"x")])
    assert [m.kind for m in out] == ["image", "voice"]
    assert out[0].url == "http://x/y.jpg"
    assert out[1].data == b"x"


def test_coerce_media_ignores_garbage():
    assert coerce_media([None, 123, {"no_kind": 1}]) == []
