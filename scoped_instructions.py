"""Filesystem-backed, human-maintained scoped instruction documents."""

from __future__ import annotations

import difflib
import json
import os
import threading
import uuid
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from agent_context import ContextBlock

INSTRUCTION_SCOPES = ("faculty", "user", "course", "lecture")
INSTRUCTION_POLICY_LABELS = {
    "faculty": "Faculty Instructions",
    "user": "User Instructions",
    "course": "Course Instructions",
    "lecture": "Lecture Instructions",
}
INSTRUCTION_PRECEDENCE = (
    "agent_prompt",
    "lecture",
    "course",
    "user",
    "faculty",
)


class InstructionError(ValueError):
    pass


class InstructionConflictError(InstructionError):
    pass


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
    normalized = value.rstrip()
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    temporary.write_text(normalized + ("\n" if normalized else ""), encoding="utf-8")
    os.replace(temporary, path)


class InstructionStore:
    """Own lightweight version history for the four deterministic scopes."""

    def __init__(self, project_root: Path, catalog: Any) -> None:
        self.project_root = project_root.resolve()
        self.catalog = catalog
        self._lock = threading.RLock()
        self._ensure_document(self._global_descriptor("faculty"))
        self._ensure_document(self._global_descriptor("user"))

    def list_documents(self) -> list[dict[str, Any]]:
        documents = [
            self._public_document(self._ensure_document(self._global_descriptor(scope)))
            for scope in ("faculty", "user")
        ]
        for course in self.catalog.list_courses():
            course_descriptor = self._course_descriptor(course)
            documents.append(
                self._public_document(self._ensure_document(course_descriptor))
            )
            for lecture in course["lectures"]:
                documents.append(
                    self._public_document(
                        self._ensure_document(
                            self._lecture_descriptor(course, lecture)
                        )
                    )
                )
        return documents

    def get_document(
        self, document_id: str, *, include_content: bool = True
    ) -> dict[str, Any]:
        with self._lock:
            descriptor = self._descriptor_for_id(document_id)
            document = self._ensure_document(descriptor)
            versions = [
                self._version_value(descriptor, item, include_content=include_content)
                for item in reversed(document["versions"])
            ]
            return {**self._public_document(document), "versions": versions}

    def list_versions(self, document_id: str) -> list[dict[str, Any]]:
        return self.get_document(document_id)["versions"]

    def get_version(self, document_id: str, version_id: str) -> dict[str, Any]:
        with self._lock:
            descriptor = self._descriptor_for_id(document_id)
            document = self._ensure_document(descriptor)
            metadata = next(
                (item for item in document["versions"] if item["id"] == version_id),
                None,
            )
            if metadata is None:
                raise InstructionError("Instruction version does not exist")
            return self._version_value(descriptor, metadata, include_content=True)

    def get_active_version(self, document_id: str) -> dict[str, Any] | None:
        document = self.get_document(document_id, include_content=False)
        version_id = document.get("activeVersionId")
        return self.get_version(document_id, version_id) if version_id else None

    def create_draft(
        self,
        document_id: str,
        *,
        source_version_id: str | None = None,
        note: str = "",
    ) -> dict[str, Any]:
        with self._lock:
            descriptor = self._descriptor_for_id(document_id)
            document = self._ensure_document(descriptor)
            existing = next(
                (item for item in document["versions"] if item["status"] == "draft"),
                None,
            )
            if existing is not None:
                return self._version_value(descriptor, existing, include_content=True)
            source_id = source_version_id or document.get("activeVersionId")
            content = (
                self.get_version(document_id, source_id)["content"]
                if source_id
                else ""
            )
            number = int(document["nextVersion"])
            version_id = f"v{number}"
            now = _now()
            metadata = {
                "id": version_id,
                "documentId": document_id,
                "version": number,
                "status": "draft",
                "createdAt": now,
                "updatedAt": now,
                "note": note.strip()[:500],
            }
            _write_text(self._version_path(descriptor, version_id), content)
            document["versions"].append(metadata)
            document["nextVersion"] = number + 1
            _write_json(descriptor["metadataPath"], document)
            return self._version_value(descriptor, metadata, include_content=True)

    def save_draft(
        self,
        document_id: str,
        version_id: str,
        *,
        content: str,
        expected_updated_at: str,
        note: str | None = None,
    ) -> dict[str, Any]:
        with self._lock:
            descriptor, document, metadata = self._mutable_version(
                document_id, version_id
            )
            if metadata["status"] != "draft":
                raise InstructionError("Only a draft can be edited")
            if metadata["updatedAt"] != expected_updated_at:
                raise InstructionConflictError(
                    "Instruction draft was updated in another window"
                )
            metadata["updatedAt"] = _now()
            if note is not None:
                metadata["note"] = note.strip()[:500]
            _write_text(self._version_path(descriptor, version_id), content)
            _write_json(descriptor["metadataPath"], document)
            return self._version_value(descriptor, metadata, include_content=True)

    def activate(self, document_id: str, version_id: str) -> dict[str, Any]:
        with self._lock:
            descriptor, document, selected = self._mutable_version(
                document_id, version_id
            )
            if selected["status"] != "draft":
                raise InstructionError("Only a draft can be activated")
            now = _now()
            for item in document["versions"]:
                if item["status"] == "active":
                    item["status"] = "archived"
                    item["updatedAt"] = now
            selected["status"] = "active"
            selected["updatedAt"] = now
            document["activeVersionId"] = version_id
            _write_json(descriptor["metadataPath"], document)
            return self.get_document(document_id)

    def restore_as_draft(
        self, document_id: str, version_id: str
    ) -> dict[str, Any]:
        return self.create_draft(
            document_id,
            source_version_id=version_id,
            note=f"Restored from {version_id}.",
        )

    def compare(
        self, document_id: str, base_id: str, target_id: str
    ) -> dict[str, Any]:
        base = self.get_version(document_id, base_id)
        target = self.get_version(document_id, target_id)
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
            "added": sum(
                line.startswith("+") and not line.startswith("+++") for line in lines
            ),
            "removed": sum(
                line.startswith("-") and not line.startswith("---") for line in lines
            ),
        }

    def document_id_for_scope(
        self, scope: str, *, course_id: str, lecture_id: str
    ) -> str:
        if scope == "faculty" or scope == "user":
            return scope
        if scope == "course":
            return f"course--{course_id}"
        if scope == "lecture":
            return f"lecture--{course_id}--{lecture_id}"
        raise InstructionError("Instruction scope is invalid")

    def _mutable_version(
        self, document_id: str, version_id: str
    ) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
        descriptor = self._descriptor_for_id(document_id)
        document = self._ensure_document(descriptor)
        metadata = next(
            (item for item in document["versions"] if item["id"] == version_id),
            None,
        )
        if metadata is None:
            raise InstructionError("Instruction version does not exist")
        return descriptor, document, metadata

    def _ensure_document(self, descriptor: Mapping[str, Any]) -> dict[str, Any]:
        path = Path(descriptor["metadataPath"])
        if path.is_file():
            return self._read_json(path)
        with self._lock:
            if path.is_file():
                return self._read_json(path)
            now = _now()
            versions: list[dict[str, Any]] = []
            active: str | None = None
            legacy_path = descriptor.get("legacyPath")
            if isinstance(legacy_path, Path) and legacy_path.is_file():
                content = legacy_path.read_text(encoding="utf-8").strip()
                if content:
                    active = "v1"
                    _write_text(self._version_path(descriptor, active), content)
                    versions.append(
                        {
                            "id": active,
                            "documentId": descriptor["id"],
                            "version": 1,
                            "status": "active",
                            "createdAt": now,
                            "updatedAt": now,
                            "note": f"Migrated from {legacy_path.name}.",
                        }
                    )
            document = {
                "schemaVersion": 1,
                "id": descriptor["id"],
                "scope": descriptor["scope"],
                "scopeId": descriptor["scopeId"],
                "courseId": descriptor.get("courseId"),
                "lectureId": descriptor.get("lectureId"),
                "title": descriptor["title"],
                "subtitle": descriptor["subtitle"],
                "appliesTo": descriptor["appliesTo"],
                "activeVersionId": active,
                "nextVersion": 2 if active else 1,
                "versions": versions,
            }
            _write_json(path, document)
            return document

    def _descriptor_for_id(self, document_id: str) -> dict[str, Any]:
        if document_id in {"faculty", "user"}:
            return self._global_descriptor(document_id)
        for course in self.catalog.list_courses():
            descriptor = self._course_descriptor(course)
            if descriptor["id"] == document_id:
                return descriptor
            for lecture in course["lectures"]:
                descriptor = self._lecture_descriptor(course, lecture)
                if descriptor["id"] == document_id:
                    return descriptor
        raise InstructionError("Instruction document does not exist")

    def _global_descriptor(self, scope: str) -> dict[str, Any]:
        title = INSTRUCTION_POLICY_LABELS[scope]
        return {
            "id": scope,
            "scope": scope,
            "scopeId": "global" if scope == "faculty" else "current-user",
            "title": title,
            "subtitle": "Faculty / Department" if scope == "faculty" else "Current local user",
            "appliesTo": "All courses",
            "metadataPath": self.project_root / "instructions" / scope / "instruction.json",
            "versionsPath": self.project_root / "instructions" / scope / "versions",
            "legacyPath": self.project_root / "instructions" / f"{scope}.md",
        }

    def _course_descriptor(self, course: Mapping[str, Any]) -> dict[str, Any]:
        root = self.project_root / "courses" / str(course["id"])
        return {
            "id": f"course--{course['id']}",
            "scope": "course",
            "scopeId": course["id"],
            "courseId": course["id"],
            "title": "Course Instructions",
            "subtitle": course["title"],
            "appliesTo": "All lectures in this course",
            "metadataPath": root / "instructions" / "instruction.json",
            "versionsPath": root / "instructions" / "versions",
            "legacyPath": root / "course.md",
        }

    def _lecture_descriptor(
        self, course: Mapping[str, Any], lecture: Mapping[str, Any]
    ) -> dict[str, Any]:
        root = self.catalog.lecture_root(str(course["id"]), str(lecture["id"]))
        return {
            "id": f"lecture--{course['id']}--{lecture['id']}",
            "scope": "lecture",
            "scopeId": lecture["id"],
            "courseId": course["id"],
            "lectureId": lecture["id"],
            "title": "Lecture Instructions",
            "subtitle": f"{course['title']} / Lecture {lecture['number']} · {lecture['title']}",
            "appliesTo": "This lecture only",
            "metadataPath": root / "instructions" / "instruction.json",
            "versionsPath": root / "instructions" / "versions",
            "legacyPath": None,
        }

    @staticmethod
    def _public_document(document: Mapping[str, Any]) -> dict[str, Any]:
        return dict(document)

    @staticmethod
    def _version_path(descriptor: Mapping[str, Any], version_id: str) -> Path:
        if not version_id.startswith("v") or not version_id[1:].isdigit():
            raise InstructionError("Instruction version ID is invalid")
        return Path(descriptor["versionsPath"]) / f"{version_id}.md"

    def _version_value(
        self,
        descriptor: Mapping[str, Any],
        metadata: Mapping[str, Any],
        *,
        include_content: bool,
    ) -> dict[str, Any]:
        value = dict(metadata)
        if include_content:
            value["content"] = self._version_path(
                descriptor, str(metadata["id"])
            ).read_text(encoding="utf-8").rstrip()
        return value

    @staticmethod
    def _read_json(path: Path) -> dict[str, Any]:
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise InstructionError("Instruction metadata is unavailable") from exc
        if not isinstance(value, dict):
            raise InstructionError("Instruction metadata is invalid")
        return value


class InstructionResolver:
    """Resolve active scope versions from AgentDefinition.contextPolicy."""

    def __init__(self, store: InstructionStore) -> None:
        self.store = store

    def resolve(
        self,
        *,
        agent_definition: Mapping[str, Any],
        course_id: str,
        lecture_id: str,
        version_overrides: Mapping[str, str] | None = None,
    ) -> dict[str, Any]:
        policy = set(agent_definition.get("contextPolicy", ()))
        resolved: list[dict[str, Any]] = []
        for scope in INSTRUCTION_SCOPES:
            if INSTRUCTION_POLICY_LABELS[scope] not in policy:
                continue
            document_id = self.store.document_id_for_scope(
                scope, course_id=course_id, lecture_id=lecture_id
            )
            document = self.store.get_document(document_id, include_content=False)
            override_id = (version_overrides or {}).get(document_id)
            selected = (
                self.store.get_version(document_id, override_id)
                if override_id is not None
                else self.store.get_active_version(document_id)
            )
            if selected is None or not selected["content"].strip():
                continue
            resolved.append(
                {
                    "scope": scope,
                    "scopeId": document["scopeId"],
                    "documentId": document_id,
                    "versionId": selected["id"],
                    "versionStatus": selected["status"],
                    "content": selected["content"],
                    "source": f"{scope} scope",
                    "title": document["title"],
                    "subtitle": document["subtitle"],
                }
            )
        return {
            "schemaVersion": 1,
            "agentId": agent_definition["agentId"],
            "courseId": course_id,
            "lectureId": lecture_id,
            "resolvedAt": _now(),
            "loadOrder": [item["scope"] for item in resolved],
            "precedence": list(INSTRUCTION_PRECEDENCE),
            "instructions": resolved,
        }


def instruction_context_blocks(
    snapshot: Mapping[str, Any] | None,
) -> tuple[ContextBlock, ...]:
    if not snapshot:
        return ()
    instructions = snapshot.get("instructions", ())
    if not isinstance(instructions, Sequence):
        raise InstructionError("Instruction snapshot is invalid")
    blocks = []
    for item in instructions:
        if not isinstance(item, Mapping) or not str(item.get("content", "")).strip():
            continue
        scope = str(item["scope"])
        blocks.append(
            ContextBlock(
                key=f"{scope}_instructions",
                label=INSTRUCTION_POLICY_LABELS[scope],
                role="system",
                content=str(item["content"]),
                scope=scope,
                version_id=str(item["versionId"]),
                source=str(item["source"]),
            )
        )
    return tuple(blocks)
