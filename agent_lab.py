"""Filesystem-backed Agent Lab prompt registry and isolated test service."""

from __future__ import annotations

import difflib
import json
import os
import re
import shutil
import threading
import time
import uuid
from collections.abc import Awaitable, Callable, Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

from agent1_boundary import BOUNDARY_TYPES
from agent_context import (
    build_agent1_boundary_context_blocks,
    build_agent1_context_blocks,
    build_pre_agent_context_blocks,
    model_messages,
)
from deepseek_client import DeepSeekClient, load_deepseek_config
from preagent_scaffold import PreAgentRun, QwenScaffoldModel
from qwen_chat_client import QwenChatClient
from scoped_instructions import (
    INSTRUCTION_POLICY_LABELS,
    InstructionResolver,
    instruction_context_blocks,
)

AGENT_IDS = {"pre-agent", "agent1-boundary", "agent1", "agent2", "agent3"}
TEST_TASK = "Prepare this lecture from its canonical normalized evidence."
DEFAULT_INSTRUCTION_POLICY = tuple(INSTRUCTION_POLICY_LABELS.values())
MODEL_NAME_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}")
AGENT1_MODEL_CONFIG_SOURCE = "agent-definition"


def _agent1_model_config() -> dict[str, Any]:
    # Shared by Agent Lab tests and both stages of the post-class runtime.
    return {
        "provider": "alibaba_cloud_model_studio",
        "model": "qwen3.8-max",
        "temperature": 0,
        "maxOutputTokens": None,
        "readOnly": False,
    }


class AgentLabError(ValueError):
    pass


class AgentLabConflictError(AgentLabError):
    pass


class ChatClient(Protocol):
    async def create_chat_completion(self, **kwargs: Any) -> Any: ...


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _write_json(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    temporary.write_text(
        json.dumps(dict(value), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, path)


def _write_text(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    temporary.write_text(value.rstrip() + "\n", encoding="utf-8")
    os.replace(temporary, path)


class AgentLabStore:
    """Own AgentDefinition, immutable versions, drafts, and test history."""

    def __init__(self, project_root: Path, *, source_root: Path | None = None) -> None:
        self.project_root = project_root.resolve()
        self.source_root = (source_root or project_root).resolve()
        self.root = self.project_root / "agent_lab"
        self._lock = threading.RLock()
        self.ensure_bootstrapped()

    def ensure_bootstrapped(self) -> None:
        definitions = (
            (
                "pre-agent",
                "Pre-Agent",
                "Build a source-grounded lecture knowledge scaffold before class.",
                "agent_lab/agents/pre-agent/prompts/v1.md",
                True,
                {
                    "provider": "alibaba_cloud_model_studio",
                    "model": os.getenv("PRE_AGENT_MODEL", "qwen3.8-max"),
                    "temperature": 0,
                    "maxOutputTokens": None,
                    "readOnly": True,
                },
                [
                    *DEFAULT_INSTRUCTION_POLICY,
                    "Production task",
                    "Slide descriptors and knowledge candidates",
                    "Bounded slide and node evidence",
                ],
            ),
            (
                "agent1-boundary",
                "Boundary Detector",
                "Select completed semantic prefixes from Agent1's rolling transcript buffer.",
                "agents/agent1_boundary.md",
                True,
                {
                    "provider": "deepseek",
                    "model": os.getenv("AGENT1_BOUNDARY_MODEL", "deepseek-flash"),
                    "temperature": 0,
                    "maxOutputTokens": None,
                    "readOnly": True,
                },
                ["Finalized source sentence snapshot", "Hard-max state"],
            ),
            (
                "agent1",
                "Agent1",
                "Incrementally extract dense classroom information while preserving the teacher's original meaning.",
                "agents/agent1.md",
                True,
                _agent1_model_config(),
                [
                    *DEFAULT_INSTRUCTION_POLICY,
                    "Previous transcript overlap",
                    "Current sentence-safe transcript chunk",
                ],
            ),
            (
                "agent2",
                "Agent2",
                "Lecture consolidation and study notes agent.",
                "agents/agent2.md",
                False,
                {},
                [],
            ),
            (
                "agent3",
                "Agent3",
                "Course memory indexer and integrator.",
                "agents/agent3.md",
                False,
                {},
                [],
            ),
        )
        with self._lock:
            for agent_id, name, description, source, executable, model, context in definitions:
                path = self._agent_path(agent_id)
                if path.is_file():
                    current = self._read_json(path)
                    changed = False
                    if agent_id == "agent1" and current.get("modelConfigSource") != AGENT1_MODEL_CONFIG_SOURCE:
                        # One-time retirement of the old standalone Lab model.
                        # Keep all prompt versions and immutable test snapshots.
                        current["modelConfig"] = _agent1_model_config()
                        current["modelConfigSource"] = AGENT1_MODEL_CONFIG_SOURCE
                        changed = True
                    if current.get("promptEditable") is not True:
                        current["promptEditable"] = True
                        changed = True
                    if not current.get("versions"):
                        content = (self.source_root / source).read_text(
                            encoding="utf-8"
                        )
                        now = _now()
                        active = "v1"
                        _write_text(self._prompt_path(agent_id, active), content)
                        current["activePromptVersionId"] = active
                        current["nextVersion"] = 2
                        current["versions"] = [
                            {
                                "id": active,
                                "agentId": agent_id,
                                "version": 1,
                                "status": "active",
                                "createdAt": now,
                                "updatedAt": now,
                                "note": "Imported from the production prompt.",
                            }
                        ]
                        changed = True
                    if agent_id in {"pre-agent", "agent1"}:
                        policy = list(current.get("contextPolicy", ()))
                        updated = [
                            label
                            for label in DEFAULT_INSTRUCTION_POLICY
                            if label not in policy
                        ]
                        if updated:
                            current["contextPolicy"] = [*updated, *policy]
                            changed = True
                    if agent_id == "pre-agent":
                        if current.get("description") != description:
                            current["description"] = description
                            changed = True
                        policy = [label for label in current.get("contextPolicy", ()) if label not in {"PipelineStatus and tool results", "Normalized evidence through workers"}]
                        for label in ("Slide descriptors and knowledge candidates", "Bounded slide and node evidence"):
                            if label not in policy:
                                policy.append(label)
                        if policy != current.get("contextPolicy"):
                            current["contextPolicy"] = policy
                            changed = True
                    if changed:
                        _write_json(path, current)
                    continue
                content = (self.source_root / source).read_text(encoding="utf-8")
                now = _now()
                active = "v1"
                _write_text(self._prompt_path(agent_id, active), content)
                versions: list[dict[str, Any]] = [
                    {
                        "id": active,
                        "agentId": agent_id,
                        "version": 1,
                        "status": "active",
                        "createdAt": now,
                        "updatedAt": now,
                        "note": "Imported from the production prompt.",
                    }
                ]
                _write_json(
                    path,
                    {
                        "schemaVersion": 1,
                        "agentId": agent_id,
                        "name": name,
                        "description": description,
                        "executable": executable,
                        "promptEditable": True,
                        "activePromptVersionId": active,
                        "nextVersion": 2,
                        "modelConfig": model,
                        **({"modelConfigSource": AGENT1_MODEL_CONFIG_SOURCE} if agent_id == "agent1" else {}),
                        "contextPolicy": context,
                        "versions": versions,
                    },
                )

    def list_agents(self) -> list[dict[str, Any]]:
        return [
            self.get_agent(agent_id, include_content=False)
            for agent_id in ("pre-agent", "agent1", "agent1-boundary", "agent2", "agent3")
        ]

    def get_agent(self, agent_id: str, *, include_content: bool = True) -> dict[str, Any]:
        with self._lock:
            agent = self._read_json(self._agent_path(agent_id))
            versions = [self._version_value(agent_id, item, include_content=include_content) for item in reversed(agent["versions"])]
            return {**agent, "versions": versions}

    def list_versions(self, agent_id: str) -> list[dict[str, Any]]:
        return self.get_agent(agent_id)["versions"]

    def get_version(self, agent_id: str, version_id: str) -> dict[str, Any]:
        agent = self._read_json(self._agent_path(agent_id))
        metadata = next((item for item in agent["versions"] if item["id"] == version_id), None)
        if metadata is None:
            raise AgentLabError("Prompt version does not exist")
        return self._version_value(agent_id, metadata, include_content=True)

    def get_active_version(self, agent_id: str) -> dict[str, Any]:
        agent = self._read_json(self._agent_path(agent_id))
        version_id = agent.get("activePromptVersionId")
        if not isinstance(version_id, str):
            raise AgentLabError("Agent has no active prompt")
        return self.get_version(agent_id, version_id)

    def update_model_config(self, agent_id: str, *, model: str) -> dict[str, Any]:
        """Persist the provider model used by future runs of an executable agent."""

        self._require_executable(agent_id)
        normalized = model.strip()
        if MODEL_NAME_PATTERN.fullmatch(normalized) is None:
            raise AgentLabError(
                "Model must be 1-128 characters using letters, numbers, '.', '_', ':', '/', or '-'."
            )
        with self._lock:
            agent = self._read_json(self._agent_path(agent_id))
            config = dict(agent.get("modelConfig") or {})
            if not config.get("provider"):
                raise AgentLabError("Agent has no model provider configured")
            config["model"] = normalized
            if agent_id in {"pre-agent", "agent1", "agent1-boundary"}:
                config["provider"] = (
                    "deepseek"
                    if normalized.lower().startswith("deepseek")
                    else "alibaba_cloud_model_studio"
                )
            config["readOnly"] = False
            agent["modelConfig"] = config
            agent["updatedAt"] = _now()
            _write_json(self._agent_path(agent_id), agent)
            return self.get_agent(agent_id)

    def create_draft(
        self, agent_id: str, *, source_version_id: str | None = None, note: str = ""
    ) -> dict[str, Any]:
        self._require_editable(agent_id)
        with self._lock:
            agent = self._read_json(self._agent_path(agent_id))
            existing = next((item for item in agent["versions"] if item["status"] == "draft"), None)
            if existing is not None:
                return self._version_value(agent_id, existing, include_content=True)
            source_id = source_version_id or agent["activePromptVersionId"]
            source = self.get_version(agent_id, source_id)
            number = int(agent["nextVersion"])
            version_id = f"v{number}"
            now = _now()
            metadata = {
                "id": version_id,
                "agentId": agent_id,
                "version": number,
                "status": "draft",
                "createdAt": now,
                "updatedAt": now,
                "note": note.strip()[:500],
            }
            _write_text(self._prompt_path(agent_id, version_id), source["content"])
            agent["versions"].append(metadata)
            agent["nextVersion"] = number + 1
            _write_json(self._agent_path(agent_id), agent)
            return self._version_value(agent_id, metadata, include_content=True)

    def save_draft(
        self,
        agent_id: str,
        version_id: str,
        *,
        content: str,
        expected_updated_at: str,
        note: str | None = None,
    ) -> dict[str, Any]:
        if not content.strip():
            raise AgentLabError("Prompt content cannot be empty")
        with self._lock:
            agent, metadata = self._mutable_version(agent_id, version_id)
            if metadata["status"] != "draft":
                raise AgentLabError("Only a draft can be edited")
            if metadata["updatedAt"] != expected_updated_at:
                raise AgentLabConflictError("Draft was updated in another window")
            metadata["updatedAt"] = _now()
            if note is not None:
                metadata["note"] = note.strip()[:500]
            _write_text(self._prompt_path(agent_id, version_id), content)
            _write_json(self._agent_path(agent_id), agent)
            return self._version_value(agent_id, metadata, include_content=True)

    def activate(self, agent_id: str, version_id: str) -> dict[str, Any]:
        with self._lock:
            agent, selected = self._mutable_version(agent_id, version_id)
            if selected["status"] != "draft":
                raise AgentLabError("Only a draft can be activated")
            now = _now()
            for item in agent["versions"]:
                if item["status"] == "active":
                    item["status"] = "archived"
                    item["updatedAt"] = now
            selected["status"] = "active"
            selected["updatedAt"] = now
            agent["activePromptVersionId"] = version_id
            _write_json(self._agent_path(agent_id), agent)
            return self.get_agent(agent_id)

    def restore_as_draft(self, agent_id: str, version_id: str) -> dict[str, Any]:
        return self.create_draft(agent_id, source_version_id=version_id, note=f"Restored from {version_id}.")

    def compare(self, agent_id: str, base_id: str, target_id: str) -> dict[str, Any]:
        base = self.get_version(agent_id, base_id)
        target = self.get_version(agent_id, target_id)
        lines = list(
            difflib.unified_diff(
                base["content"].splitlines(),
                target["content"].splitlines(),
                fromfile=base_id,
                tofile=target_id,
                lineterm="",
            )
        )
        return {
            "baseVersionId": base_id,
            "targetVersionId": target_id,
            "diff": "\n".join(lines),
            "added": sum(line.startswith("+") and not line.startswith("+++") for line in lines),
            "removed": sum(line.startswith("-") and not line.startswith("---") for line in lines),
        }

    def prompt_path(self, agent_id: str, version_id: str) -> Path:
        self.get_version(agent_id, version_id)
        return self._prompt_path(agent_id, version_id)

    def save_test_run(self, value: Mapping[str, Any]) -> dict[str, Any]:
        _write_json(self.root / "test_runs" / f"{value['id']}.json", value)
        return dict(value)

    def list_test_runs(self, agent_id: str | None = None, limit: int = 20) -> list[dict[str, Any]]:
        runs = []
        for path in (self.root / "test_runs").glob("*.json"):
            value = self._read_json(path)
            if agent_id is None or value.get("agentId") == agent_id:
                runs.append(value)
        return sorted(runs, key=lambda item: str(item.get("createdAt", "")), reverse=True)[:limit]

    def get_test_run(self, run_id: str) -> dict[str, Any]:
        if not run_id.startswith("test_") or not run_id.replace("_", "").isalnum():
            raise AgentLabError("Test run ID is invalid")
        return self._read_json(self.root / "test_runs" / f"{run_id}.json")

    def _mutable_version(self, agent_id: str, version_id: str) -> tuple[dict[str, Any], dict[str, Any]]:
        self._require_editable(agent_id)
        agent = self._read_json(self._agent_path(agent_id))
        metadata = next((item for item in agent["versions"] if item["id"] == version_id), None)
        if metadata is None:
            raise AgentLabError("Prompt version does not exist")
        return agent, metadata

    def _version_value(self, agent_id: str, metadata: Mapping[str, Any], *, include_content: bool) -> dict[str, Any]:
        value = dict(metadata)
        if include_content:
            value["content"] = self._prompt_path(agent_id, str(metadata["id"])).read_text(encoding="utf-8").rstrip()
        return value

    def _agent_path(self, agent_id: str) -> Path:
        if agent_id not in AGENT_IDS:
            raise AgentLabError("Agent does not exist")
        return self.root / "agents" / agent_id / "agent.json"

    def _prompt_path(self, agent_id: str, version_id: str) -> Path:
        if not version_id.startswith("v") or not version_id[1:].isdigit():
            raise AgentLabError("Prompt version ID is invalid")
        return self.root / "agents" / agent_id / "prompts" / f"{version_id}.md"

    def _require_editable(self, agent_id: str) -> None:
        agent = self._read_json(self._agent_path(agent_id))
        if agent.get("promptEditable") is not True:
            raise AgentLabError("Agent prompt is read-only")

    def _require_executable(self, agent_id: str) -> None:
        agent = self._read_json(self._agent_path(agent_id))
        if agent.get("executable") is not True:
            raise AgentLabError("Agent is not executable yet")

    @staticmethod
    def _read_json(path: Path) -> dict[str, Any]:
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise AgentLabError("Agent Lab metadata is unavailable") from exc
        if not isinstance(value, dict):
            raise AgentLabError("Agent Lab metadata is invalid")
        return value


PreAgentRunner = Callable[[Path, Path], Awaitable[dict[str, Any]]]


class AgentLabService:
    def __init__(
        self,
        store: AgentLabStore,
        catalog: Any,
        *,
        clients: Mapping[str, ChatClient] | None = None,
        pre_agent_runner: PreAgentRunner | None = None,
        instruction_resolver: InstructionResolver | None = None,
    ) -> None:
        self.store = store
        self.catalog = catalog
        self.clients = dict(clients or {})
        self.pre_agent_runner = pre_agent_runner
        self.instruction_resolver = instruction_resolver

    def resolve_context(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        agent_id = str(payload.get("agentId", ""))
        version_id = payload.get("promptVersionId")
        version = self.store.get_version(agent_id, str(version_id)) if version_id else self.store.get_active_version(agent_id)
        course_id = str(payload.get("courseId", ""))
        lecture_id = str(payload.get("lectureId", ""))
        lecture = self.catalog.get_lecture(course_id, lecture_id)
        agent_definition = self.store.get_agent(agent_id, include_content=False)
        instruction_snapshot = (
            self.instruction_resolver.resolve(
                agent_definition=agent_definition,
                course_id=course_id,
                lecture_id=lecture_id,
            )
            if self.instruction_resolver is not None
            else None
        )
        instruction_blocks = instruction_context_blocks(instruction_snapshot)
        if agent_id == "agent1":
            transcript = str(payload.get("transcript", "")).strip()
            if not transcript:
                raise AgentLabError("Agent1 test transcript is required")
            blocks = build_agent1_context_blocks(
                system_prompt=version["content"],
                previous_context=str(payload.get("previousContext", "")),
                transcript_chunk=transcript,
                instruction_blocks=instruction_blocks,
            )
            input_snapshot = {"transcript": transcript, "previousContext": str(payload.get("previousContext", ""))}
        elif agent_id == "agent1-boundary":
            transcript = str(payload.get("transcript", "")).strip()
            if not transcript:
                raise AgentLabError("Boundary Detector test transcript is required")
            hard_max_reached = bool(payload.get("hardMaxReached", False))
            blocks = build_agent1_boundary_context_blocks(
                system_prompt=version["content"],
                transcript=transcript,
                hard_max_reached=hard_max_reached,
            )
            input_snapshot = {
                "transcript": transcript,
                "hardMaxReached": hard_max_reached,
            }
        elif agent_id == "pre-agent":
            root = self.catalog.lecture_root(course_id, lecture_id)
            normalized = root / "normalized" / "normalized_slides.json"
            if not normalized.is_file():
                raise AgentLabError("Lecture has no reusable normalized Pre-Agent input")
            blocks = build_pre_agent_context_blocks(
                system_prompt=version["content"],
                task=TEST_TASK,
                normalized_path="normalized/normalized_slides.json",
                instruction_blocks=instruction_blocks,
            )
            input_snapshot = {
                "normalizedPath": str(normalized.relative_to(root)),
                "normalizedBytes": normalized.stat().st_size,
                "sourceFilename": lecture.get("material", {}).get("filename"),
            }
        else:
            raise AgentLabError("Agent is not executable yet")
        return {
            "agentId": agent_id,
            "promptVersionId": version["id"],
            "courseId": course_id,
            "lectureId": lecture_id,
            "blocks": [block.to_dict() for block in blocks],
            "messages": model_messages(blocks),
            "input": input_snapshot,
            "modelConfig": agent_definition["modelConfig"],
            "instructionSnapshot": instruction_snapshot,
        }

    async def run_test(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        resolved = self.resolve_context(payload)
        agent_id = resolved["agentId"]
        version = self.store.get_version(agent_id, resolved["promptVersionId"])
        run_id = f"test_{datetime.now(UTC).strftime('%Y%m%dT%H%M%S%fZ')}_{uuid.uuid4().hex[:8]}"
        started = time.perf_counter()
        if agent_id == "pre-agent":
            result = await self._run_pre_agent(
                resolved["courseId"],
                resolved["lectureId"],
                version,
                run_id,
                resolved["instructionSnapshot"],
            )
            output = str(result.get("output", ""))
            validation = result.get("validation", {"status": "unknown"})
            usage = result.get("usage")
        else:
            response = await self._client(
                agent_id, str(resolved["modelConfig"].get("provider") or "")
            ).create_chat_completion(
                messages=resolved["messages"],
                model=resolved["modelConfig"]["model"],
                temperature=resolved["modelConfig"].get("temperature"),
                **(
                    {"response_format": {"type": "json_object"}}
                    if agent_id == "agent1-boundary"
                    else {}
                ),
                **(
                    {"max_tokens": resolved["modelConfig"]["maxOutputTokens"]}
                    if resolved["modelConfig"].get("maxOutputTokens") is not None
                    else {}
                ),
            )
            output = self._response_text(response)
            validation = (
                self._validate_boundary_output(output)
                if agent_id == "agent1-boundary"
                else {"status": "not_applicable"}
            )
            usage = self._usage(response)
        record = {
            "schemaVersion": 1,
            "id": run_id,
            "agentId": agent_id,
            "promptVersionId": version["id"],
            "promptContentSnapshot": version["content"],
            "courseId": resolved["courseId"],
            "lectureId": resolved["lectureId"],
            "input": resolved["input"],
            "resolvedContext": resolved,
            "output": output,
            "validation": validation,
            "modelConfigSnapshot": resolved["modelConfig"],
            "model": resolved["modelConfig"]["model"],
            "latencyMs": round((time.perf_counter() - started) * 1000, 3),
            "usage": usage,
            "createdAt": _now(),
        }
        return self.store.save_test_run(record)

    async def _run_pre_agent(
        self,
        course_id: str,
        lecture_id: str,
        version: Mapping[str, Any],
        run_id: str,
        instruction_snapshot: Mapping[str, Any] | None,
    ) -> dict[str, Any]:
        source_root = self.catalog.lecture_root(course_id, lecture_id)
        sandbox = self.store.root / "test_workspaces" / run_id
        for relative in ("source", "normalized", "runtime/slide_images"):
            source = source_root / relative
            if source.exists():
                shutil.copytree(source, sandbox / relative, dirs_exist_ok=True)
        prompt_path = self.store.prompt_path("pre-agent", str(version["id"]))
        if self.pre_agent_runner is not None:
            return await self.pre_agent_runner(sandbox, prompt_path)
        model_config = self.store.get_agent("pre-agent", include_content=False)[
            "modelConfig"
        ]
        lecture = self.catalog.get_lecture(course_id, lecture_id)
        result = await PreAgentRun(
            sandbox,
            lecture_id,
            lecture["title"],
            QwenScaffoldModel(str(model_config["model"]), self._client("pre-agent", str(model_config.get("provider") or "")), prompt_path.read_text(encoding="utf-8"), sandbox),
            prompt_version=str(version["id"]),
        ).run()
        return {
            "output": f"Knowledge Scaffold committed: {result['run_id']}",
            "validation": {"status": "valid"},
            "usage": {},
        }

    def _client(self, agent_id: str, provider: str = "") -> ChatClient:
        if agent_id in self.clients:
            return self.clients[agent_id]
        cache_key = f"{agent_id}:{provider}"
        if cache_key in self.clients:
            return self.clients[cache_key]
        if provider == "alibaba_cloud_model_studio":
            client: ChatClient = QwenChatClient()
        else:
            client = DeepSeekClient(load_deepseek_config())
        self.clients[cache_key] = client
        return client

    @staticmethod
    def _response_text(response: Any) -> str:
        choices = getattr(response, "choices", None)
        content = getattr(getattr(choices[0], "message", None), "content", None) if choices else None
        if not isinstance(content, str) or not content.strip():
            raise AgentLabError("Model returned no text output")
        return content.strip()

    @staticmethod
    def _usage(response: Any) -> dict[str, int] | None:
        usage = getattr(response, "usage", None)
        if usage is None:
            return None
        return {
            "inputTokens": int(getattr(usage, "prompt_tokens", 0) or 0),
            "outputTokens": int(getattr(usage, "completion_tokens", 0) or 0),
            "totalTokens": int(getattr(usage, "total_tokens", 0) or 0),
        }

    @staticmethod
    def _validate_boundary_output(output: str) -> dict[str, str]:
        try:
            value = json.loads(output)
        except json.JSONDecodeError:
            return {"status": "invalid", "reason": "Output is not valid JSON"}
        if not isinstance(value, dict) or set(value) != {
            "decision",
            "cut_after_source_id",
            "boundary_type",
        }:
            return {"status": "invalid", "reason": "Output does not match the boundary schema"}
        decision = value["decision"]
        cut = value["cut_after_source_id"]
        boundary_type = value["boundary_type"]
        if decision == "wait" and cut is None and boundary_type is None:
            return {"status": "valid"}
        if (
            decision == "cut"
            and isinstance(cut, (str, int))
            and not isinstance(cut, bool)
            and boundary_type in BOUNDARY_TYPES
        ):
            return {"status": "valid"}
        return {
            "status": "invalid",
            "reason": "Decision, source ID, and boundary type are inconsistent",
        }
