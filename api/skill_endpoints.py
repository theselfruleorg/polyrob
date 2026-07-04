"""
User Skills API - CRUD operations for user-created skills.

Users can create custom skills that get loaded alongside system skills.
User skills are stored in: data/prompts/skills/user_{user_id}/
"""

import json
import shutil
import logging
from pathlib import Path
from typing import List, Optional, Dict, Any

from fastapi import APIRouter, HTTPException, Depends, Request
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/skills", tags=["skills"])


# =============================================================================
# Models
# =============================================================================

class SkillTriggers(BaseModel):
    """Triggers that activate a skill."""
    tool_ids: List[str] = Field(default_factory=list, description="Tool IDs that trigger this skill")
    keywords: List[str] = Field(default_factory=list, description="Keywords in task that trigger this skill")
    task_patterns: List[str] = Field(default_factory=list, description="Regex patterns to match task")


class SkillCreate(BaseModel):
    """Create a new skill."""
    id: str = Field(..., description="Skill ID (lowercase, hyphenated: my-custom-skill)")
    name: str = Field(..., description="Human-readable name")
    description: str = Field(..., description="Short description")
    content: str = Field(..., description="Markdown content for the skill")
    triggers: SkillTriggers = Field(default_factory=SkillTriggers)
    priority: int = Field(default=5, ge=1, le=10, description="Priority (1=highest)")


class SkillUpdate(BaseModel):
    """Update an existing skill."""
    name: Optional[str] = None
    description: Optional[str] = None
    content: Optional[str] = None
    triggers: Optional[SkillTriggers] = None
    priority: Optional[int] = Field(default=None, ge=1, le=10)


class SkillResponse(BaseModel):
    """Skill details response."""
    id: str
    name: str
    description: str
    type: str  # "system" or "user"
    priority: int
    triggers: Dict[str, Any] = {}
    content: Optional[str] = None  # Only included when fetching single skill


class SkillListResponse(BaseModel):
    """List of skills."""
    system: List[SkillResponse]
    user: List[SkillResponse]


# =============================================================================
# Helpers
# =============================================================================

def get_skills_base_dir() -> Path:
    """Get base (SYSTEM/builtin) skills directory (anchored to the
    install/repo root, not CWD). Unchanged by Task 9 - the shipped
    system-skill library stays read-only on the package tree; only per-tenant
    USER storage moves to data-home (see get_user_skills_dir).
    """
    return Path(__file__).resolve().parents[1] / "data" / "prompts" / "skills"


def get_user_skills_dir(user_id: str) -> Path:
    """Get user's skills directory, creating if needed.

    Task 9 (REST-layer data-home fix, folded in from the Task 8 review): this
    used to hardcode ``get_skills_base_dir()`` (the installed package tree),
    which split-brained from ``SkillManager``/``SkillWriterMixin`` the moment
    Task 8 moved THEIR user-scope reads/writes to data-home
    (``skill_store.skills_data_home()``) - post-``polyrob update``,
    ``GET /api/skills`` would show a phantom-empty user list, ``PUT``/
    ``DELETE`` would false-404, and the ``POST`` 409 conflict-precheck would
    go blind.

    Delegates to the LIVE ``SkillManager`` singleton's ``_user_dirs_root()``
    (the same "where do user writes actually land" resolver
    ``SkillWriterMixin._user_root`` uses) rather than re-deriving the
    data-home path independently - this guarantees the REST root can never
    drift from wherever SkillManager actually writes, INCLUDING the
    pre-existing test-only single-root override contract
    (``SkillManager(skills_dir=...)`` / ``sm.skills_dir = ...``) that several
    tests in this module rely on. In production (no override) that resolver
    returns exactly ``skill_store.skills_data_home()``.
    """
    from agents.task.agent.skill_manager import get_skill_manager

    base = get_skill_manager()._user_dirs_root()
    user_dir = base / f"user_{user_id}"
    user_dir.mkdir(parents=True, exist_ok=True)
    return user_dir


def get_user_rules(user_id: str) -> Dict[str, Any]:
    """Load user's skill rules."""
    user_dir = get_user_skills_dir(user_id)
    rules_file = user_dir / "rules.json"
    
    if rules_file.exists():
        try:
            with open(rules_file) as f:
                return json.load(f)
        except Exception as e:
            logger.warning(f"Failed to load user rules for {user_id}: {e}")
    
    return {}


def save_user_rules(user_id: str, rules: Dict[str, Any]) -> None:
    """Save user's skill rules."""
    user_dir = get_user_skills_dir(user_id)
    rules_file = user_dir / "rules.json"
    
    with open(rules_file, "w") as f:
        json.dump(rules, f, indent=2)


def get_current_user(request: Request) -> str:
    """Extract user ID from request (via JWT middleware)."""
    from api.dependencies import get_user_id
    return get_user_id(request)


def get_current_user_optional(request: Request) -> Optional[str]:
    """Extract user ID if authenticated, else None (no 401).

    E10 (A6 gap 9): normalizes the previously-bespoke
    `getattr(request.state, 'user_id', None)` duplicated at list_skills/get_skill
    onto the SAME canonical identity resolution get_current_user /
    api.dependencies.get_user_id uses, minus the raise. Anonymous reads still
    degrade to the base catalog only — no behavior change (A6 gap 9 found this
    to be an inconsistency, not an exploitable leak).
    """
    from api.dependencies import get_user_id
    try:
        return get_user_id(request)
    except HTTPException:
        return None


def validate_skill_id(skill_id: str) -> None:
    """Validate skill ID format."""
    import re
    if not re.match(r'^[a-z][a-z0-9-]*$', skill_id):
        raise HTTPException(
            400, 
            "Skill ID must be lowercase, start with a letter, and contain only letters, numbers, and hyphens"
        )
    if len(skill_id) > 50:
        raise HTTPException(400, "Skill ID must be 50 characters or less")


# =============================================================================
# Endpoints
# =============================================================================

@router.get("", response_model=SkillListResponse)
async def list_skills(request: Request, user_id: Optional[str] = Depends(get_current_user_optional)):
    """List all available skills (system + user's custom skills)."""
    # user_id resolved via get_current_user_optional (Depends) — unauthenticated
    # callers see only system skills, same as before.

    # Load system skills
    from agents.task.agent.skill_manager import get_skill_manager
    sm = get_skill_manager()
    sm._ensure_rules_loaded()

    system_skills = []
    for skill_id, rules in sm.skill_rules.items():
        # Route through the same catalog description resolver the agent uses
        # (frontmatter-preferred, rules.json fallback) so REST and the agent
        # catalog can never drift (P0 minor #5). _load_skill_content populates
        # the frontmatter meta cache that _resolve_skill_description reads.
        sm._load_skill_content(skill_id)
        system_skills.append(SkillResponse(
            id=skill_id,
            name=rules.get("name", skill_id.replace("-", " ").title()),
            description=sm._resolve_skill_description(skill_id, rules),
            type="system",
            priority=rules.get("priority", 5),
            triggers=rules.get("triggers", {}),
        ))

    # Load user skills
    user_skills = []
    if user_id:
        user_rules = get_user_rules(user_id)
        for skill_id, rules in user_rules.items():
            sm._load_skill_content(skill_id, user_id=user_id)
            user_skills.append(SkillResponse(
                id=skill_id,
                name=rules.get("name", skill_id),
                description=sm._resolve_skill_description(skill_id, rules, user_id=user_id),
                type="user",
                priority=rules.get("priority", 5),
                triggers=rules.get("triggers", {}),
            ))
    
    return SkillListResponse(system=system_skills, user=user_skills)


@router.post("", status_code=201)
async def create_skill(skill: SkillCreate, user_id: str = Depends(get_current_user)):
    """Create a new user skill."""
    validate_skill_id(skill.id)
    
    # Check if skill already exists
    user_dir = get_user_skills_dir(user_id)
    skill_dir = user_dir / skill.id
    
    if skill_dir.exists():
        raise HTTPException(409, f"Skill '{skill.id}' already exists")
    
    # Check system skills
    from agents.task.agent.skill_manager import get_skill_manager
    sm = get_skill_manager()
    sm._ensure_rules_loaded()
    if skill.id in sm.skill_rules:
        raise HTTPException(409, f"Skill '{skill.id}' conflicts with a system skill")
    
    # Route the write through the scanned, atomic SkillWriter choke-point (T5).
    from agents.task.agent.skill_writer import PROVENANCE_USER
    res = sm.create_skill(
        skill.id, skill.content, user_id=user_id,
        description=skill.description, created_by=PROVENANCE_USER, pending=False,
    )
    if not res.ok:
        raise HTTPException(400, f"skill rejected: {'; '.join(res.errors)}")

    # Preserve the REST contract fields the writer does not model (name/triggers/priority).
    rules = get_user_rules(user_id)
    entry = rules.get(skill.id, {})
    updates: dict = {
        "name": skill.name,
        "description": skill.description,
        "priority": skill.priority,
        "auto_activate": True,
    }
    # BUG 6 fix: only overwrite writer-derived keyword triggers when the caller
    # supplied at least one non-empty trigger list.  An all-empty SkillTriggers
    # (the default) would otherwise discard the keywords _upsert_rule derived
    # from the skill id/description, leaving the skill auto_activate=True but
    # unmatchable by get_skills_for_session.
    caller_triggers = skill.triggers.model_dump()
    if any(v for v in caller_triggers.values()):
        updates["triggers"] = caller_triggers
    entry.update(updates)
    rules[skill.id] = entry
    save_user_rules(user_id, rules)

    logger.info(f"User {user_id} created skill: {skill.id}")
    
    return {"id": skill.id, "message": "Skill created successfully"}


@router.get("/{skill_id}", response_model=SkillResponse)
async def get_skill(skill_id: str, request: Request, user_id: Optional[str] = Depends(get_current_user_optional)):
    """Get skill details including content."""
    validate_skill_id(skill_id)
    # user_id resolved via get_current_user_optional (Depends) — unauthenticated
    # callers fall through to the system-skill branch, same as before.

    # Check user skills first
    if user_id:
        user_dir = get_user_skills_dir(user_id)
        user_skill_file = user_dir / skill_id / "SKILL.md"

        if user_skill_file.exists():
            content = user_skill_file.read_text()
            # Warm the frontmatter meta cache (same lookup path, see
            # _load_skill_content) so _resolve_skill_description can prefer it;
            # `content` itself stays the raw file text (unchanged contract).
            from agents.task.agent.skill_manager import get_skill_manager
            sm = get_skill_manager()
            sm._load_skill_content(skill_id, user_id=user_id)
            rules = get_user_rules(user_id).get(skill_id, {})
            return SkillResponse(
                id=skill_id,
                name=rules.get("name", skill_id),
                description=sm._resolve_skill_description(skill_id, rules, user_id=user_id),
                type="user",
                priority=rules.get("priority", 5),
                triggers=rules.get("triggers", {}),
                content=content,
            )

    # Check system skills
    from agents.task.agent.skill_manager import get_skill_manager
    sm = get_skill_manager()
    sm._ensure_rules_loaded()

    if skill_id in sm.skill_rules:
        content = sm._load_skill_content(skill_id)
        rules = sm.skill_rules[skill_id]
        return SkillResponse(
            id=skill_id,
            name=rules.get("name", skill_id.replace("-", " ").title()),
            description=sm._resolve_skill_description(skill_id, rules),
            type="system",
            priority=rules.get("priority", 5),
            triggers=rules.get("triggers", {}),
            content=content,
        )

    raise HTTPException(404, f"Skill '{skill_id}' not found")


@router.put("/{skill_id}")
async def update_skill(
    skill_id: str,
    update: SkillUpdate,
    user_id: str = Depends(get_current_user)
):
    """Update a user skill. System skills cannot be modified."""
    validate_skill_id(skill_id)
    user_dir = get_user_skills_dir(user_id)
    skill_dir = user_dir / skill_id
    
    if not skill_dir.exists():
        # Check if it's a system skill
        from agents.task.agent.skill_manager import get_skill_manager
        sm = get_skill_manager()
        sm._ensure_rules_loaded()
        if skill_id in sm.skill_rules:
            raise HTTPException(403, "Cannot modify system skills. Fork it first.")
        raise HTTPException(404, f"Skill '{skill_id}' not found")
    
    # Update content if provided — route through the scanned writer (T5).
    if update.content is not None:
        from agents.task.agent.skill_manager import get_skill_manager
        from agents.task.agent.skill_writer import PROVENANCE_USER
        res = get_skill_manager().create_skill(
            skill_id, update.content, user_id=user_id,
            description=(update.description or get_user_rules(user_id).get(skill_id, {}).get("description", "")),
            created_by=PROVENANCE_USER, pending=False,
        )
        if not res.ok:
            raise HTTPException(400, f"skill rejected: {'; '.join(res.errors)}")
    
    # Update rules
    rules = get_user_rules(user_id)
    if skill_id in rules:
        if update.name is not None:
            rules[skill_id]["name"] = update.name
        if update.description is not None:
            rules[skill_id]["description"] = update.description
        if update.triggers is not None:
            rules[skill_id]["triggers"] = update.triggers.model_dump()
        if update.priority is not None:
            rules[skill_id]["priority"] = update.priority
        
        save_user_rules(user_id, rules)
    
    logger.info(f"User {user_id} updated skill: {skill_id}")
    
    return {"message": "Skill updated successfully"}


@router.delete("/{skill_id}")
async def delete_skill(skill_id: str, user_id: str = Depends(get_current_user)):
    """Delete a user skill. System skills cannot be deleted."""
    validate_skill_id(skill_id)
    user_dir = get_user_skills_dir(user_id)
    skill_dir = user_dir / skill_id

    # Check if it's a system skill before touching the filesystem.
    from agents.task.agent.skill_manager import get_skill_manager
    sm = get_skill_manager()
    sm._ensure_rules_loaded()
    if skill_id in sm.skill_rules:
        raise HTTPException(403, "Cannot delete system skills")

    if not skill_dir.exists():
        raise HTTPException(404, f"Skill '{skill_id}' not found")

    # Route deletion through the archiving writer (recoverable; no raw rmtree).
    from agents.task.agent.skill_writer import PROVENANCE_USER
    ok = sm.delete_skill(skill_id, user_id=user_id, created_by=PROVENANCE_USER)
    if not ok:
        raise HTTPException(404, f"Skill '{skill_id}' not found or could not be deleted")

    logger.info(f"User {user_id} deleted skill: {skill_id}")

    return {"message": "Skill deleted successfully"}


@router.post("/{skill_id}/fork")
async def fork_skill(skill_id: str, user_id: str = Depends(get_current_user)):
    """Fork a system skill to create a user copy that can be customized."""
    # Load system skill
    from agents.task.agent.skill_manager import get_skill_manager
    sm = get_skill_manager()
    sm._ensure_rules_loaded()
    
    if skill_id not in sm.skill_rules:
        raise HTTPException(404, f"System skill '{skill_id}' not found")
    
    # Create new ID
    new_id = f"{skill_id}-custom"
    user_dir = get_user_skills_dir(user_id)
    
    # Check if already exists
    if (user_dir / new_id).exists():
        raise HTTPException(409, f"Forked skill '{new_id}' already exists")
    
    # Copy content
    content = sm._load_skill_content(skill_id)
    rules = sm.skill_rules[skill_id]
    resolved_description = sm._resolve_skill_description(skill_id, rules)

    # Route through the scanned writer (T5).
    from agents.task.agent.skill_writer import PROVENANCE_USER
    res = sm.create_skill(
        new_id, content, user_id=user_id,
        description=resolved_description,
        created_by=PROVENANCE_USER, pending=False,
    )
    if not res.ok:
        raise HTTPException(400, f"fork rejected: {'; '.join(res.errors)}")

    # Save rules
    user_rules = get_user_rules(user_id)
    user_rules[new_id] = {
        "name": f"{rules.get('name', skill_id)} (Custom)",
        "description": resolved_description,
        "triggers": rules.get("triggers", {}),
        "priority": rules.get("priority", 5),
        "auto_activate": True,
        "forked_from": skill_id,
    }
    save_user_rules(user_id, user_rules)
    
    logger.info(f"User {user_id} forked skill {skill_id} -> {new_id}")
    
    return {"id": new_id, "message": f"Forked '{skill_id}' to '{new_id}'"}

