# Agents Package - AI Agent System

_Last reviewed: 2026-06-30. For the authoritative architecture see ../AGENTS.md; for env flags see ../docs/CONFIGURATION.md._

## Overview

The `agents` package provides the POLYROB platform's agent system. As of the 2026 chat consolidation,
there is a **single** primary agent — the Task agent (`TaskAgent`, exported from `agents/__init__.py`)
— which handles both task automation and conversational chat (the former `ChatAgent` was removed and
its chat path folded into `TaskAgent.chat_once`). The package also provides the personality/character
system and the system-prompt support used by the Task agent.

## Architecture Philosophy

- **Single front-door agent**: one Task agent core serves both task automation and chat
- **Mixin composition over god-files**: the four large classes (`Agent`, `SessionOrchestrator`,
  `MessageManager`, `Controller`) each compose focused mixins rather than growing one file; new
  behavior gets its own mixin/module (see ../AGENTS.md "Decomposition note")
- **Personality-driven**: a `Character` system feeds personality/style into the system prompt
- **Provider flexibility**: multiple LLM providers with intelligent fallback (native LLM layer)
- **Preserve LLM content, never synthesize**: brain state is extracted from preserved content
- **Single source of truth per concern**: e.g. `ToolCallTracker` for tool-call IDs

## Package Structure

Only directories/files that exist are listed; the agent core is mixin-based, so the file count under
`task/agent/core/` is large — representative files are shown.

```
agents/
├── __init__.py                     # Lazy package exports (TaskAgent, BaseAgent, managers) + registry
├── README.md                       # This documentation
├── base_agent.py                   # Abstract base class (BaseAgent) for agents
├── task_agent_lite.py              # TaskAgent wrapper + SessionRequest (incl. chat_once)
│
├── personality/                    # Character and personality system
│   ├── character.py                # Character (class Character(BaseComponent))
│   ├── character_manager.py        # Character lifecycle management
│   ├── persona_render.py           # Persona/style rendering helpers
│   └── characters/                 # Character definition files
│       ├── rob.character.json      # Default POLYROB character
│       └── trump.character.json    # Example character
│
├── prompt/                         # System-prompt support
│   ├── __init__.py
│   ├── base_prompt.py              # BasePromptManager (file-based prompt storage)
│   └── system.py                   # SystemPromptManager (prompt orchestration)
│
└── task/                           # Task automation subsystem
    ├── __init__.py
    ├── config.py                   # Task configuration
    ├── constants.py                # Task constants + AutonomyConfig / local-mode flags
    ├── logging_config.py           # Task logging configuration
    ├── path.py                     # Centralized path manager (pm())
    ├── tool_defaults.py            # Default tool_ids resolution
    ├── templates.py                # Agent/session templates
    ├── surface_config.py           # Per-surface config
    ├── session_registry.py         # In-process session→orchestrator map (SessionRegistry)
    ├── sqlite_session_registry.py  # SQLite-backed cross-process variant (opt-in)
    ├── session_route.py            # Session routing classification (LOCAL/REMOTE/MISSING)
    ├── workspace_context.py        # Workspace context
    ├── runtime_safety.py           # Safety controls
    ├── utils.py / utils_json.py / utils_webview.py / robust_parse_config.py
    │
    ├── agent/                      # Task agent implementation (mixin-based)
    │   ├── __init__.py
    │   ├── service.py              # class Agent(... mixins ...) — step/run loop core
    │   ├── orchestrator.py         # class SessionOrchestrator(... mixins ...)
    │   ├── session.py              # SessionStatus enum + SessionManager
    │   ├── agent_state.py          # Agent state tracking
    │   ├── views.py                # View models (AgentHistoryList, AgentError, etc.)
    │   ├── prompts.py              # Task-specific prompt construction
    │   ├── tool_call_tracker.py    # ToolCallTracker — SINGLE source of truth for call IDs
    │   ├── hitl_manager.py         # Human-in-the-loop
    │   ├── profile_manager.py / profile_registry.py / scenario_registry.py
    │   ├── conversation.py         # Conversation / Turn (chat plumbing)
    │   ├── skill_manager.py        # SkillManager(SkillWriterMixin) — skill match/load
    │   ├── skill_writer.py         # SkillWriterMixin — create/patch/delete/promote (writable skills)
    │   ├── sub_agent_manager.py    # SubAgentManager — delegation (run_subtask / parallel)
    │   ├── async_delegation.py     # AsyncDelegationRegistry — background delegation results
    │   ├── log_sanitize.py
    │   │
    │   ├── core/                   # Step loop, construction, and agent-intelligence concerns (mixins)
    │   │   ├── construction.py     # AgentConstructionMixin — wiring at session start
    │   │   ├── run_loop.py         # RunLoopMixin.run() — the multi-step run loop
    │   │   ├── step.py             # StepMixin — _prepare_step / _call_llm / _record_step / _finalize_step
    │   │   ├── step_execution.py   # StepExecutionMixin — _execute_actions
    │   │   ├── result_processing.py# ResultProcessingMixin — _process_action_results
    │   │   ├── step_telemetry.py   # StepTelemetryMixin
    │   │   ├── llm_runner.py       # LLMRunnerMixin — LLM invocation + provider fallback
    │   │   ├── llm_provisioning.py # LLMProvisioningMixin — main/aux/judge LLM provisioning
    │   │   ├── memory_writer.py    # MemoryWriterMixin — H-MEM writes + summaries
    │   │   ├── memory_prefetch.py  # MemoryPrefetchMixin — recall injection
    │   │   ├── output_validation.py# OutputValidationMixin — _validate_output (judge)
    │   │   ├── error_recovery.py   # ErrorRecoveryMixin — _handle_step_error / billing failover
    │   │   ├── loop_detection.py   # LoopDetectionMixin
    │   │   ├── conversational_exit.py # 2-reply-only-step turn end
    │   │   ├── correspondent_gate.py  # Capability gate for correspondent-tainted sessions
    │   │   ├── untrusted_wrap.py   # <untrusted_tool_result> framing
    │   │   ├── background_review.py# BackgroundReviewMixin — post-turn aux reviewer fork
    │   │   ├── self_wake.py        # Self-wake re-entry rail
    │   │   ├── curator.py          # Skill curator (stale/archive/reactivate)
    │   │   ├── secret_guard.py / safety_lifecycle.py / project_context.py
    │   │   ├── history_io.py / logging_io.py / session_metadata.py / resources.py
    │   │   ├── turn_input.py / user_ingress.py / next_action_internal.py
    │   │   └── model_introspection.py
    │   │
    │   ├── messages/               # MessageManager concern mixins
    │   │   ├── token_counter.py    # TokenCounterMixin
    │   │   ├── compactor.py        # CompactorMixin — context compaction / LLM synthesis
    │   │   ├── persistence.py      # PersistenceMixin — checkpoint/disk (JSON source of truth)
    │   │   ├── sqlite_persistence.py # SqlitePersistenceMixin — opt-in durable write-mirror
    │   │   ├── filters.py          # FiltersMixin — sensitive-data scrub, tool-sequence repair
    │   │   ├── guidance.py         # GuidanceMixin — injected guidance/control messages
    │   │   ├── builders.py         # MessageBuildersMixin
    │   │   ├── retrieval.py        # MessageRetrievalMixin — get_messages_for_llm
    │   │   └── context_references.py # @-context references
    │   │
    │   └── message_manager/        # MessageManager façade + tool-call plumbing
    │       ├── service.py          # class MessageManager(... messages/ mixins ...)
    │       ├── config.py / views.py
    │       ├── tool_call_builder.py # ToolCallBuilder — format normalization only
    │       └── tool_message_repair.py
    │
    ├── session/                    # SessionOrchestrator concern mixins
    │   ├── browser_pool.py         # BrowserPoolMixin
    │   ├── multi_agent.py          # MultiAgentMixin
    │   ├── feed.py                 # FeedMixin
    │   ├── workspace.py            # WorkspaceMixin
    │   ├── execution.py            # SessionExecutionMixin (run_session)
    │   ├── cleanup.py              # SessionCleanupMixin
    │   ├── hitl_ingress.py         # HITLIngressMixin
    │   └── hooks.py                # SessionHooksMixin — session/subagent lifecycle hooks
    │
    ├── goals/                      # Durable goal board (autonomy W4)
    │   ├── board.py                # Goal + GoalBoard (data/goals.db, atomic CAS claim)
    │   └── dispatcher.py           # GoalDispatcher + GoalTicker
    │
    ├── runtime/                    # Shared run-as-session entrypoint
    │   └── run_as_session.py       # run_task_as_session() (used by cron/goals)
    │
    └── telemetry/                  # Task telemetry system
        ├── service.py / manager.py / formatters.py / sequence.py / views.py
```

## Core Agent System

### BaseAgent (`base_agent.py`)

Abstract base class (`class BaseAgent(BaseComponent)`) providing standardized agent lifecycle and
common LLM/character plumbing. `TaskAgent` subclasses it.

**Selected interface** (see the source for the full surface):
```python
class BaseAgent(BaseComponent):
    def __init__(self, *, config: BotConfig, container: DependencyContainer, name: str): ...
    async def process_input(self, input_text: str, context_id: str, **kwargs) -> str: ...
    async def start_conversation(self, user_id: str, **kwargs) -> bool: ...
    async def set_character(self, character: "Character") -> None: ...
    async def set_llm_client(self, client_name: str) -> bool: ...
    async def generate_response(self, messages, **kwargs) -> str: ...
```

**Lifecycle states** (inherited from `BaseComponent`): uninitialized → initializing → ready →
processing → error / cleaning-up. These are the *component* lifecycle states, distinct from a
running task session's `SessionStatus` (below).

### Conversational chat

There is no longer a separate `ChatAgent` class. Conversational chat is served by the Task agent
via `TaskAgent.chat_once(...)` (`task_agent_lite.py`) — a single-turn entry point used by the
OpenAI-compatible `/v1/chat/completions` surface and other chat front-doors. This collapsed the
previously separate chat code path into one agent core.

### TaskAgent (`task_agent_lite.py`)

The platform's single primary agent: a session manager that creates `SessionOrchestrator`s and
handles both complex multi-step task automation and conversational chat. Active orchestrators are
held behind **SessionRegistry** (`task/session_registry.py`) — use
`get_orchestrator`/`register_orchestrator`/`remove_orchestrator`, never the dict directly.

**Features**:
- Session management for long-running tasks (`create_session` / `run_session`)
- Browser automation via Playwright (opt-in tool)
- Service integration (email, social media, documents)
- Task decomposition and planning
- Conversational chat via `chat_once`
- Human-in-the-loop controls

## Task Automation Subsystem (`task/`)

### SessionRequest (`task_agent_lite.py`)

The request shape passed to `TaskAgent.create_session(...)` (defined alongside `TaskAgent`, **not**
in `orchestrator.py`):
```python
@dataclass
class SessionRequest:
    task: str                                 # Task description
    model: str = "gpt-5"                       # LLM model (overridden by env/key resolution)
    provider: str = "openai"                   # LLM provider
    tools: List[str] = None                    # → ["browser", "filesystem", "task"] if None
    max_steps: int = 50                        # Maximum automation steps
    use_vision: bool = True                    # Enable vision capabilities
```
Note: actual provider/model are resolved at chat/session time from whichever API key is present
(see `_resolve_chat_provider_model` and the shared runtime resolver), so the `gpt-5`/`openai`
defaults rarely win in practice.

### SessionStatus (`task/agent/session.py`)

Session lifecycle is tracked by `SessionStatus` (an `Enum`). The valid states are:
- `CREATED` — initial state after creation
- `RUNNING` — currently executing
- `COMPLETED` — finished successfully (waiting for a possible follow-up)
- `RESUMED` — continuous-chat resume (transitional)
- `SUSPENDED` — evicted from memory, persisted to disk
- `FAILED` — execution failed
- `CANCELLED` — user cancelled (terminal)

There is **no** `PENDING` or `PAUSED` state. `PAUSED` was removed in favor of `CANCELLED` for user
interruption; follow-up messages use the `COMPLETED → RESUMED` flow. Transitions are enforced by
`SessionManager` (`session.py`).

### Agent (`task/agent/service.py`)

The task-execution core is `class Agent`, composed from many focused mixins (run loop, step phases,
LLM runner, memory, error recovery, output validation, loop detection, etc.) via MRO:

```python
class Agent(AgentConstructionMixin, RunLoopMixin, StepMixin, StepExecutionMixin,
            StepTelemetryMixin, ResultProcessingMixin, LLMRunnerMixin,
            NextActionInternalMixin, ErrorRecoveryMixin, OutputValidationMixin,
            MemoryWriterMixin, MemoryPrefetchMixin, BackgroundReviewMixin,
            HistoryIOMixin, LoggingIOMixin, SafetyLifecycleMixin, UserIngressMixin,
            TurnInputMixin, LLMProvisioningMixin, ModelIntrospectionMixin,
            LoopDetectionMixin, ResourceMixin, SessionMetadataMixin):
    ...
```

Construction uses two dataclasses — `Agent.__init__(self, config: AgentConfig, deps: AgentDeps)`
(use `Agent.from_params(**kwargs)` for the legacy kwarg form). There are **no** `run_task()` /
`pause()` / `resume()` / `cancel()` methods on `Agent`; execution is the run loop plus per-step
phases.

**Run loop** — `RunLoopMixin.run(max_steps=100, _continue_session=False)` (`core/run_loop.py`)
drives the session, returning an `AgentHistoryList`. It calls `step()` repeatedly until the agent is
done, an error halts it, or a guard (conversational-exit, loop-detection, max-steps) fires.

**Step phases** — a single step (`StepMixin._step_impl`, `core/step.py`) is split into phases:
`_prepare_step` → `_call_llm` → `_validate_and_intervene` → `_execute_actions` →
`_process_action_results` → `_record_step` → `_finalize_step`. `_execute_actions` lives in
`StepExecutionMixin` (`core/step_execution.py`); `_process_action_results` in `ResultProcessingMixin`
(`core/result_processing.py`).

**Tool-call flow** (native tools): LLM returns `tool_calls` → `ToolCallBuilder.normalize_tool_call()`
(format only) → `ToolCallTracker.register_tool_calls()` (ID tracking, single source of truth) →
`Registry.tool_calls_to_actions()` (Pydantic validation) → `Controller.multi_act()` (execution) →
`MessageManager.add_tool_response()` → `ToolCallTracker.complete_step()`.

### SessionOrchestrator (`task/agent/orchestrator.py`)

Coordinates a session's agents, services and browser contexts across the lifecycle. Like `Agent`,
it composes its concerns from mixins under `task/session/`:

```python
class SessionOrchestrator(WorkspaceMixin, FeedMixin, MultiAgentMixin, BrowserPoolMixin,
                          HITLIngressMixin, SessionCleanupMixin, SessionExecutionMixin,
                          SessionHooksMixin):
    ...
```

`SessionExecutionMixin.run_session` runs the agent loop for a session; `SessionHooksMixin` provides
fail-open session + sub-agent start/end lifecycle hooks.

### MessageManager (`task/agent/message_manager/service.py`)

Message storage + retrieval, composed from the `task/agent/messages/` mixins:

```python
class MessageManager(TokenCounterMixin, CompactorMixin, PersistenceMixin, FiltersMixin,
                     GuidanceMixin, MessageBuildersMixin, MessageRetrievalMixin):
    ...
```

Token math, compaction (synthesis), persistence and filters live in the mixins, not inline. JSON
(`message_history.json`) is the source of truth; `MESSAGE_STORE_BACKEND=sqlite` adds a write-only
durable mirror.

### Telemetry System (`task/telemetry/`)

Per-step telemetry: step tracking, token usage, cost calculation, performance metrics and session
analytics (`service.py`, `manager.py`, `formatters.py`, `sequence.py`, `views.py`).

### Autonomy: goals & runtime

- `task/goals/board.py` — `Goal` + `GoalBoard`: a durable cross-session backlog (`data/goals.db`,
  WAL + jitter) with atomic CAS `claim` (safe under `workers>1`), a circuit breaker, and tenant
  scoping. `task/goals/dispatcher.py` — `GoalDispatcher` / `GoalTicker` run claimed goals via
  `create_session` + `run_session`.
- `task/runtime/run_as_session.py` — `run_task_as_session()`, the shared entrypoint used by the
  cron and goal executors to run a one-off task as a full session.

These tickers are wired by the shared autonomy runtime (`core/autonomy_runtime.py`), not directly by
this package; see ../AGENTS.md "Shared autonomy runtime".

## Personality System (`personality/`)

### Character Model (`character.py`)

Rich character/personality definition. It is a `BaseComponent` subclass (**not** a `@dataclass`):

```python
class Character(BaseComponent):
    def __init__(self, name: str, config: BotConfig, container=None): ...
    # attributes initialized in _initialize_attributes():
    #   name, modelProvider ("anthropic"), settings, bio, lore,
    #   knowledge, topics, adjectives, style
```

Loaded from `personality/characters/*.character.json`; the attributes drive the personality/style
injected into the agent's system prompt.

### CharacterManager (`character_manager.py`)

Central orchestrator for character lifecycle (load from file, role/default resolution, hot-reload).

### Character Configuration Example

```json
{
  "name": "POLYROB",
  "bio": "An advanced AI assistant with expertise in automation and problem-solving.",
  "modelProvider": "anthropic",
  "settings": { "temperature": 0.7, "maxTokens": 4096 },
  "adjectives": ["helpful", "knowledgeable", "patient", "efficient"],
  "style": { "speaking": ["clear", "concise", "professional"], "tone": "friendly yet focused" },
  "topics": ["automation", "productivity", "technology", "problem-solving"],
  "knowledge": [
    "Web automation and browser control",
    "Document processing and analysis",
    "API integration and data handling"
  ]
}
```

## Prompt Engineering System (`prompt/`)

### SystemPromptManager (`prompt/system.py`)

Orchestrates system-prompt generation with character integration. (Note: the *Task* agent builds its
own session system prompt via `task/agent/prompts.py`; the `prompt/` package provides the shared
prompt-manager components registered as services.)

### BasePromptManager (`prompt/base_prompt.py`)

Base class for prompt management with file-based prompt storage.

## Agent Registry

Exports in `agents/__init__.py` are **lazy** (PEP 562 `__getattr__`) so importing the package is
cheap; `TaskAgent` is only imported when actually resolved.

```python
# AGENT_METADATA (built lazily)
{
    'task_agent': {
        'class': TaskAgent,
        'description': 'Task agent',
        'is_core': False,
        'optional': True,
        'required_services': ['llm'],
        'optional_services': [
            'filesystem', 'perplexity', 'websearch',
            'twitter', 'email', 'cache_manager',
        ],
    }
}

# AGENT_COMPONENTS (built lazily): [('task_agent', TaskAgent, 'Task agent', True, {...})]
```

## Initialization

```python
async def initialize_shared_components(container: DependencyContainer):
    """Initialize shared components used by agents (system prompt + character managers)."""
    # registers 'system_prompt_manager' and 'character_manager' if absent,
    # then marks the 'shared_components' group initialized
```

## Usage Examples

### Conversational chat
```python
task_agent = container.get_service('task_agent')
response = await task_agent.chat_once(text="Help me with productivity", user_id="user123")
```

### Task agent session
```python
session = await task_agent.create_session(SessionRequest(
    task="Research AI trends and create a summary report",
    tools=["browser", "filesystem"],
    max_steps=30,
))
result = await task_agent.run_session(session.session_id)
```

### Character loading
```python
character = await character_manager.load_character("researcher")
# Characters drive the personality/style injected into the agent's system prompt
```

## Best Practices

### Agent / core development
1. **Add a mixin, don't grow a god-file**: new `Agent` / `SessionOrchestrator` / `MessageManager` /
   `Controller` behavior gets its own mixin module (see ../AGENTS.md "Decomposition note").
2. **Preserve LLM content**: extract brain state from preserved content; never synthesize it.
3. **Single source of truth**: tool-call IDs go through `ToolCallTracker`, sessions through
   `SessionRegistry`.
4. **Registry-closure landmine**: action-registration modules deliberately do **not** use
   `from __future__ import annotations` (it stringizes closure param annotations the Registry
   introspects).
5. **Fail fast with clear errors** — don't paper over problems with cascades of fallbacks.

### Task automation
1. **Step limits**: set an appropriate `max_steps`.
2. **Tool selection**: only enable necessary tools (MCP/browser/coding are opt-in, not defaults).
3. **Session cleanup**: clean up completed/failed sessions.
4. **Human-in-the-loop**: support HITL for critical actions.

### Character design
1. **Consistent personality** across interactions; **domain expertise** aligned with use cases.
2. **Clear style guidelines** and testing across scenarios.

## Exports

```python
__all__ = [
    'BaseAgent',
    'TaskAgent',
    'SystemPromptManager',
    'BasePromptManager',
    'CharacterManager',
    'initialize_shared_components',
    'AGENT_COMPONENTS',
    'AGENT_METADATA',
    'TASK_PACKAGE_AVAILABLE',
]
```
