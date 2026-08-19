"""The tool-schema shape must follow the provider actually SERVING the request.

Regression for a live prod failure (2026-08-13): a session pinned to
`zai-coding` (Anthropic-messages transport) serving model `glm-5` sent
OpenAI-shaped tool schemas and z.ai answered HTTP 422
``body.tools[0].name: Field required``. The agent then degraded to emitting
tool calls as raw text and looped until max_steps without executing anything.

Root cause: the agent derived the provider from the MODEL NAME via the model
registry, which records `glm-5` under OpenRouter — the vendor that originated
the id, not the row serving this session.
"""
import pytest

from agents.task.utils import detect_llm_provider, resolve_serving_provider


class _Spec:
    def __init__(self, name):
        self.name = name


class _Client:
    def __init__(self, name):
        self._spec = _Spec(name)


class _Adapter:
    def __init__(self, name):
        self._client = _Client(name)


class _PlainClient:
    """A built-in client (anthropic/openai) — carries no ProviderSpec."""


class _PlainAdapter:
    def __init__(self):
        self._client = _PlainClient()


def test_spec_backed_client_wins_over_model_name_detection():
    # glm-5 is an OpenRouter row in the model registry, but this session is
    # served by zai-coding. The serving provider is what the schema follows.
    assert resolve_serving_provider(_Adapter("zai-coding"), "glm-5") == "zai-coding"


def test_falls_back_to_model_name_when_client_has_no_spec():
    # Built-in providers keep their existing behaviour exactly.
    llm = _PlainAdapter()
    assert resolve_serving_provider(llm, "glm-5") == detect_llm_provider(None, "glm-5")


@pytest.mark.parametrize("llm", [None, object()])
def test_missing_or_foreign_llm_falls_back(llm):
    assert resolve_serving_provider(llm, "glm-5") == detect_llm_provider(None, "glm-5")


def test_subscription_seat_bills_zero_only_when_correctly_attributed():
    """The billing half of the same misattribution.

    Observed on prod: a zai-coding session's usage_records carried
    ``"provider": "openrouter"`` and $0.0273 of fabricated spend, because
    billing read the same model-name-derived provider. The z.ai GLM Coding
    Plan is flat-rate, so the marginal cost of a call is genuinely $0.
    """
    from modules.credits.pricing import compute_llm_cost

    usage = {"prompt_tokens": 35659, "completion_tokens": 107}
    assert compute_llm_cost("glm-5", usage, provider="zai-coding") == 0.0
    # Misattributed to a metered provider, the same call invents spend.
    assert compute_llm_cost("glm-5", usage, provider="openrouter") > 0.0


def test_zai_coding_routes_to_the_anthropic_schema_generator():
    """The end-to-end consequence: Anthropic transport => Anthropic schemas."""
    from tools.controller.registry.schema_generators import get_schema_generator

    provider = resolve_serving_provider(_Adapter("zai-coding"), "glm-5")
    generator = type(get_schema_generator(provider)).__name__
    assert generator == "AnthropicSchemaGenerator", (
        f"zai-coding is an anthropic_messages row; got {generator}. "
        "OpenAI-shaped tools at that endpoint are a hard 422."
    )


def test_aux_metering_attributes_the_serving_provider():
    """Aux calls (judge/compaction/reflection) bill the SEAT that served them.

    Live prod: the run_outcome judge ran on the main zai-coding client, but its
    usage rows recorded `provider=openrouter` with `model=glm-4.6` — an
    impossible pair — and invented ~$0.004 per judged goal. The adapter carries
    no `llm_provider` attribute, so identity fell through to model-name
    detection, which credits glm-4.6 to OpenRouter.
    """
    from agents.task.agent.core.aux_metering import _llm_identity

    class _Adapter:
        model_name = "glm-4.6"

        def __init__(self):
            self._client = _Client("zai-coding")

    model, provider = _llm_identity(_Adapter(), None)
    assert model == "glm-4.6"
    assert provider == "zai-coding", (
        f"aux metering credited {provider!r}; a flat-rate seat then bills as metered"
    )


def test_display_cost_estimate_respects_a_flat_rate_seat():
    """The CLI's run-summary line must not invent spend on a flat-rate plan.

    Live prod: `polyrob run` printed `$0.0224` for a zai-coding run whose
    ledger row correctly recorded $0.00. The display path
    (`calculate_cost_from_tokens`) takes only a model name, so the flat-rate
    rule that `compute_llm_cost` applies could never reach it — the owner is
    shown fabricated spend on every run.
    """
    from modules.credits.cost_utils import calculate_cost_from_tokens

    tokens = dict(input_tokens=35659, output_tokens=107)
    # Metered provider (or unknown): estimate as before.
    assert calculate_cost_from_tokens("glm-4.6", **tokens) > 0.0
    # Flat-rate seat: the marginal cost of this call is genuinely zero.
    assert calculate_cost_from_tokens("glm-4.6", provider="zai-coding", **tokens) == 0.0


def test_telemetry_accepts_the_serving_provider_from_either_route():
    """The display estimate must reach the flat-rate rule however it is called.

    Two producers feed telemetry: the step loop calls with an explicit
    `provider=`, while `agents.task.capture_llm_request` is a **kwargs wrapper
    that funnels unknown keys into `parameters`. Both must land on the same
    provider, or the CLI keeps printing metered spend for a $0 seat.
    """
    import inspect
    from agents.task.telemetry.manager import TelemetryManager
    from agents.task.telemetry.service import ProductTelemetry

    # The manager must be able to forward it (a missing kwarg raises TypeError
    # into a swallowing `except`, which silently drops telemetry entirely).
    for name in ("capture_llm_usage", "capture_llm_call"):
        sig = inspect.signature(getattr(TelemetryManager, name))
        assert "provider" in sig.parameters, f"TelemetryManager.{name} drops provider"

    sig = inspect.signature(ProductTelemetry.capture_llm_usage)
    assert "provider" in sig.parameters


def test_a_spec_seat_serving_an_unknown_model_id_still_resolves_the_seat():
    """The construction-time residual of c60e3dc5: Agent.__init__ reconciles
    use_native_tools BEFORE the step loop, and a False there forces
    JSON-fallback for the whole session — the step-time resolution sites
    cannot recover it. Model-name detection maps an unknown id to 'generic'
    (no native tools); the serving spec row must win at that seam too."""
    assert resolve_serving_provider(_Adapter("zai-coding"),
                                    "some-future-model") == "zai-coding"
    # Whatever the registry guesses for the id, it is not the serving seat —
    # reconciling against the guess is reconciling the wrong row's capability.
    assert detect_llm_provider(None, "some-future-model") != "zai-coding"
