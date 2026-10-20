"""The ``skill_manage`` action: the agent's write path to its own skills (081 WS-5.1).

``action_registration.py`` registers the closure (no ``from __future__`` there —
the registry introspects the closure's first-param model); the closure only calls
:func:`run_skill_manage`. All safety stays in ``modules.skills.writer``.

Read-before-write without session state: ``patch``, ``edit`` and ``delete`` carry
``expected_sha256`` — the ``sha256`` that ``load_skill`` returned for the body the
agent read. A missing one refuses; a stale one refuses and names the current one.
``create`` never overwrites: an existing id is edited, not re-created.
"""
from typing import Literal, Optional

from pydantic import BaseModel

from agents.task.loop_types.controller_types import ActionResult

SKILL_MANAGE_DESCRIPTION = (
	"Author your own durable SKILLS (procedures you can reload in future sessions). "
	"create: a new SKILL.md (markdown, a # heading) plus when = the index line that "
	"says when to load it (<= 60 chars). patch: exact-string replace. "
	"edit: replace the whole body. delete: archive. patch/edit/delete need "
	"expected_sha256 = the sha256 load_skill returned for the body you read. "
	"rollback: restore an archived revision (to_sha, default the one the last change "
	"replaced). A background delete names absorbed_into. promote is owner-only. New and "
	"edited skills wait for review before they activate; a bundled skill's fix waits "
	"for the owner."
)


class SkillManageAction(BaseModel):
	action: Literal["create", "patch", "edit", "delete", "rollback", "promote"]
	skill_id: str
	content: Optional[str] = None          # create/edit: full SKILL.md
	old_string: Optional[str] = None       # patch: exact text to replace
	new_string: Optional[str] = None       # patch: replacement
	replace_all: bool = False
	description: str = ""
	expected_sha256: Optional[str] = None  # patch/edit/delete: sha256 of the body you read
	to_sha: Optional[str] = None           # rollback: the revision (8+ char prefix)
	absorbed_into: Optional[str] = None    # delete: the skill that now holds this one
	when: Optional[str] = None             # create: the index line (<= 60 chars)


_NEEDS_READ = ("patch", "edit", "delete")


def _keep_frontmatter(current: str, content: str) -> str:
	"""``edit`` replaces the body the model saw; ``load_skill`` strips the
	frontmatter, so an edit without one keeps the current frontmatter."""
	if content.lstrip().startswith("---") or not current.startswith("---"):
		return content
	end = current.find("\n---", 3)
	if end < 0:
		return content
	close = current.find("\n", end + 4)
	head = current[: (close + 1) if close >= 0 else len(current)]
	return head + ("" if head.endswith("\n") else "\n") + content.lstrip("\n")


def _with_when(skill_id: str, content: str, when: Optional[str], description: str) -> str:
	"""081 WS-4.3: a new skill is an index line, so ``create`` needs one: the
	content's ``metadata.polyrob-when``, or ``when`` written into its frontmatter.
	Raises ``ValueError`` (the refusal text) when there is none."""
	import json
	from core.skills.frontmatter import parse_frontmatter, when_line
	if when_line(parse_frontmatter(content)[0]):
		return content
	when = (when or "").strip()
	if not when:
		raise ValueError(
			"create needs `when`: one line of <= 60 chars that says when to load the "
			"skill (it is the skill's line in the index).")
	line = f"  polyrob-when: {json.dumps(when)}\n"
	if not content.startswith("---"):
		desc = json.dumps((description or when).strip())
		return f"---\nname: {skill_id}\ndescription: {desc}\nmetadata:\n{line}---\n{content}"
	end = content.find("\n---", 3)
	if end < 0:
		raise ValueError("the frontmatter has no closing `---` line")
	head, tail = content[:end + 1], content[end + 1:]
	if "\nmetadata:" in head:
		at = head.index("\nmetadata:") + len("\nmetadata:")
		at = head.index("\n", at) + 1
		return head[:at] + line + head[at:] + tail
	return head + "metadata:\n" + line + tail


def _refusal(res) -> str:
	text = f"Skill rejected: {'; '.join(res.errors)}"
	if res.revision and any("revision conflict" in e for e in res.errors):
		text += (f". The current sha256 is {res.revision}: load_skill it again, "
		         f"rebuild the change from that body, and pass that sha256.")
	return text


async def run_skill_manage(controller, params: SkillManageAction, execution_context=None) -> ActionResult:
	"""Dispatch one ``skill_manage`` call. Never raises."""
	from tools.controller import action_registration as _ar
	from tools.controller._helpers import forget_activated_skill, self_mod_emitter
	user_id = getattr(execution_context, 'user_id', None) or getattr(controller, 'user_id', None)
	if not user_id:
		return ActionResult(error="skill authoring requires a user (tenant scope).",
		                    include_in_memory=True)
	# T4-06: every effected skill mutation records a first-class self_modification event.
	_self_mod_ev = self_mod_emitter(execution_context, controller, user_id, kind="skill",
	                                source="skill_manage", item_id=params.skill_id, created_by="")
	# A sub-agent/leaf/forged/autonomous author is background_review: ALWAYS quarantined
	# (SK-F10, C7); a normal interactive turn is agent (follows SKILLS_WRITABLE_REQUIRE_REVIEW).
	from modules.skills.writer import PROVENANCE_AGENT, PROVENANCE_BACKGROUND
	is_forged = _ar._is_forged_or_autonomous_turn(execution_context, controller)
	created_by = PROVENANCE_BACKGROUND if is_forged else PROVENANCE_AGENT
	if params.action == "promote":
		return ActionResult(
			error="Promotion requires the owner to review the draft through /pending; a model tool call cannot approve it.",
			include_in_memory=True)
	if params.action in _NEEDS_READ and not params.expected_sha256:
		return ActionResult(
			error=(f"{params.action} needs expected_sha256: call load_skill('{params.skill_id}') "
			       f"and pass the sha256 it returns for the body you read."),
			include_in_memory=True)
	try:
		from agents.task.agent.skill_manager import get_skill_manager
		sm = get_skill_manager()
		if params.action == "create":
			if not params.content:
				return ActionResult(error="create requires `content`.", include_in_memory=True)
			try:
				content = _with_when(params.skill_id, params.content, params.when,
				                     params.description)
			except ValueError as exc:
				return ActionResult(error=str(exc), include_in_memory=True)
			if sm._find_skill_file(user_id, params.skill_id) is not None:
				return ActionResult(error=(
					f"Skill `{params.skill_id}` exists: load_skill it, then use action='edit' "
					f"or 'patch' with its sha256."), include_in_memory=True)
			res = sm.create_skill(params.skill_id, content, user_id=user_id,
			                      description=params.description, created_by=created_by)
		elif params.action == "patch":
			if params.old_string is None or params.new_string is None:
				return ActionResult(error="patch requires `old_string` and `new_string`.",
				                    include_in_memory=True)
			res = sm.patch_skill(params.skill_id, user_id=user_id,
			                     old_string=params.old_string, new_string=params.new_string,
			                     replace_all=params.replace_all, created_by=created_by,
			                     expected_revision=params.expected_sha256)
		elif params.action == "edit":
			if not params.content:
				return ActionResult(error="edit requires `content`.", include_in_memory=True)
			res = sm.edit_skill(params.skill_id, params.content, user_id=user_id,
			                    description=params.description, created_by=created_by,
			                    expected_revision=params.expected_sha256,
			                    keep_frontmatter=_keep_frontmatter)
		elif params.action == "rollback":
			res = sm.rollback_skill(params.skill_id, user_id=user_id, to_sha=params.to_sha,
			                        created_by=created_by)
		else:  # delete
			res = sm.archive_skill(params.skill_id, user_id=user_id, created_by=created_by,
			                       absorbed_into=params.absorbed_into,
			                       expected_revision=params.expected_sha256)
			if res.ok:
				forget_activated_skill(controller, params.skill_id)  # archived — never re-ack it
				_self_mod_ev("delete", created_by=created_by)
				return ActionResult(extracted_content=f"Archived skill `{params.skill_id}`.",
				                    include_in_memory=True)
	except Exception as e:
		controller.logger.debug(f"skill_manage failed: {e}")
		return ActionResult(error=f"skill_manage failed: {e}", include_in_memory=True)

	if not res.ok:
		return ActionResult(error=_refusal(res), include_in_memory=True)
	where = "pending review" if res.pending else "active"
	# The body just changed: let the next `load_skill` re-emit it.
	forget_activated_skill(controller, params.skill_id)
	_self_mod_ev(params.action, pending=bool(res.pending), created_by=created_by)
	if res.pending:  # §7.1: notify the owner (fail-open, gated)
		try:
			from core import self_evolution as _se
			from core.instance import resolve_instance_id as _rii
			from core.runtime_paths import data_dir_or_home
			_cfg = getattr(getattr(controller, 'container', None), 'config', None)
			await _se.maybe_notify_owner_pending(
				getattr(controller, 'container', None), user_id,
				home_dir=data_dir_or_home(getattr(_cfg, 'data_dir', None)),
				instance_id=_rii(), skill_manager=sm)
		except Exception as _e:
			controller.logger.debug(f"self-evolution notify skipped: {_e}")
	from tools.controller.doc_authoring import skill_saved_text
	text = skill_saved_text(params.skill_id, where)
	if res.revision:
		text += f"\nsha256: {res.revision}"
	if res.warnings:
		text += "\nLint (lines this write added): " + "; ".join(res.warnings)
	return ActionResult(extracted_content=text, include_in_memory=True)
