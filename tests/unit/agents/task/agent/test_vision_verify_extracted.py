"""P7 finalization: the ~93-line post-response vision-verification block was extracted
verbatim from the ~1100-line _get_next_action_internal god-method into
_verify_vision_post_response. Guard the extraction: the helper exists, is a no-op when
no images were expected, and resets the counter when they were.
"""
import logging
import types

from agents.task.agent.core.next_action_internal import NextActionInternalMixin


def _obj(expected=None):
    o = types.SimpleNamespace()
    o.logger = logging.getLogger("test-vision")
    if expected is not None:
        o._expected_image_count = expected
    # Bind the unbound method to our stand-in.
    o._verify_vision_post_response = types.MethodType(
        NextActionInternalMixin._verify_vision_post_response, o
    )
    return o


def test_noop_when_no_images_expected():
    o = _obj(expected=0)
    o._verify_vision_post_response(response=types.SimpleNamespace(content="hi"))  # no raise
    assert o._expected_image_count == 0


def test_noop_when_attr_absent():
    o = _obj(expected=None)  # no _expected_image_count at all
    o._verify_vision_post_response(response=types.SimpleNamespace(content="hi"))  # no raise


def test_resets_counter_when_images_expected():
    o = _obj(expected=3)
    o._verify_vision_post_response(
        response=types.SimpleNamespace(content="The image shows a red button in the top-left corner.")
    )
    assert o._expected_image_count == 0, "counter must be cleared after verification"


def test_method_is_on_the_mixin():
    assert callable(getattr(NextActionInternalMixin, "_verify_vision_post_response"))
