from core.surfaces.media import Media, coerce_media


def test_media_transcript_defaults_none():
    assert Media(kind="voice").transcript is None


def test_media_transcript_settable():
    assert Media(kind="voice", transcript="hello there").transcript == "hello there"


def test_coerce_media_passes_transcript():
    out = coerce_media([{"kind": "voice", "transcript": "hi"}])
    assert len(out) == 1 and out[0].transcript == "hi"
