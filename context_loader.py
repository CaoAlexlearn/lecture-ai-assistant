"""Read-only loader for stable instructions and per-agent contracts.

Stable instruction files are human-controlled. This module deliberately exposes
no write API and does not load learned course memory.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

AgentName = Literal["agent1", "agent2", "agent3", "ppt_preparation"]

PROJECT_ROOT = Path(__file__).resolve().parent

_CONTRACT_FILES: dict[str, str] = {
    "agent1": "agent1.md",
    "agent2": "agent2.md",
    "agent3": "agent3.md",
    "ppt_preparation": "ppt_preparation.md",
}

# Agent contracts are always loaded separately. These entries describe only
# stable instruction access.
_INSTRUCTION_MATRIX: dict[str, tuple[str, ...]] = {
    "agent1": (),
    "agent2": ("faculty", "user", "course"),
    "agent3": ("course",),
    "ppt_preparation": ("faculty", "user", "course"),
}

_AGENT_LAB_IDS = {
    "agent1": "agent1",
    "agent2": "agent2",
    "agent3": "agent3",
}


class ContextLoaderError(ValueError):
    """Base error for invalid or unavailable scoped context."""


class UnknownAgentError(ContextLoaderError):
    """Raised when no contract exists for an agent name."""


class CourseContextRequiredError(ContextLoaderError):
    """Raised when an agent needs course context but no course ID was supplied."""


class CourseNotFoundError(ContextLoaderError):
    """Raised when the explicitly requested course does not exist."""


@dataclass(frozen=True, slots=True)
class LoadedContext:
    """Context selected by the least-context matrix.

    ``None`` means the layer was not loaded for this agent. An empty string means
    the layer was authorized and loaded from an empty Markdown file.
    """

    agent_name: AgentName
    agent_contract: str
    course_id: str | None
    lecture_id: str | None
    faculty: str | None = None
    user: str | None = None
    course: str | None = None
    loaded_paths: tuple[Path, ...] = ()


def _read_markdown(path: Path) -> str:
    if not path.is_file():
        raise ContextLoaderError(f"Required context file does not exist: {path}")
    return path.read_text(encoding="utf-8")


def _contract_path(project_root: Path, agent_name: AgentName) -> Path:
    """Prefer the active Agent Lab prompt, with legacy files as fallback."""

    registry_id = _AGENT_LAB_IDS.get(agent_name)
    if registry_id is not None:
        metadata_path = (
            project_root / "agent_lab" / "agents" / registry_id / "agent.json"
        )
        try:
            metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
            active = metadata.get("activePromptVersionId")
            if isinstance(active, str):
                prompt_path = metadata_path.parent / "prompts" / f"{active}.md"
                if prompt_path.is_file():
                    return prompt_path
        except (OSError, json.JSONDecodeError, AttributeError):
            pass
    return project_root / "agents" / _CONTRACT_FILES[agent_name]


def _resolve_course_directory(project_root: Path, course_id: str) -> Path:
    if not course_id or Path(course_id).parts != (course_id,) or course_id in {".", ".."}:
        raise CourseNotFoundError(f"Invalid course_id: {course_id!r}")

    courses_root = (project_root / "courses").resolve()
    course_directory = (courses_root / course_id).resolve()
    try:
        course_directory.relative_to(courses_root)
    except ValueError as exc:
        raise CourseNotFoundError(f"Invalid course_id: {course_id!r}") from exc

    if not course_directory.is_dir():
        raise CourseNotFoundError(
            f"Course {course_id!r} does not exist under {courses_root}"
        )
    return course_directory


def load_context(
    agent_name: AgentName,
    course_id: str | None = None,
    lecture_id: str | None = None,
    *,
    project_root: Path | str = PROJECT_ROOT,
) -> LoadedContext:
    """Load only the contract and stable instructions allowed for one agent.

    The loader never scans the courses directory, never falls back to another
    course, and never reads ``courses/{course_id}/memory``. ``lecture_id`` is
    retained as scope metadata; lecture artifacts are outside Instruction V1.
    """

    if agent_name not in _CONTRACT_FILES:
        raise UnknownAgentError(f"Unknown agent_name: {agent_name!r}")

    root = Path(project_root).resolve()
    contract_path = _contract_path(root, agent_name)
    contract = _read_markdown(contract_path)
    loaded_paths = [contract_path]
    allowed = _INSTRUCTION_MATRIX[agent_name]

    faculty: str | None = None
    user: str | None = None
    course: str | None = None

    if "faculty" in allowed:
        faculty_path = root / "instructions" / "faculty.md"
        faculty = _read_markdown(faculty_path)
        loaded_paths.append(faculty_path)

    if "user" in allowed:
        user_path = root / "instructions" / "user.md"
        user = _read_markdown(user_path)
        loaded_paths.append(user_path)

    resolved_course_id: str | None = None
    if "course" in allowed:
        if course_id is None:
            raise CourseContextRequiredError(
                f"course_id is required when loading context for {agent_name}"
            )
        course_directory = _resolve_course_directory(root, course_id)
        course_path = course_directory / "course.md"
        course = _read_markdown(course_path)
        loaded_paths.append(course_path)
        resolved_course_id = course_id
    elif course_id is not None:
        # Validate caller scope without granting Agent 1 course context.
        _resolve_course_directory(root, course_id)
        resolved_course_id = course_id

    return LoadedContext(
        agent_name=agent_name,
        agent_contract=contract,
        course_id=resolved_course_id,
        lecture_id=lecture_id,
        faculty=faculty,
        user=user,
        course=course,
        loaded_paths=tuple(loaded_paths),
    )
