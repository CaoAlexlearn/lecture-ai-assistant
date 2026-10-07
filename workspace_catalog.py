"""Filesystem-backed Course/Lecture workspace catalog and preparation lifecycle."""

from __future__ import annotations

import asyncio
import fcntl
import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
import threading
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from knowledge_scaffold import hotword_bias_weight, is_safe_asr_hotword, normalize_asr_hotword
from mineru_normalizer import (
    load_and_normalize_mineru_file,
    render_visual_slides,
    write_normalized_document_atomic,
)
from preagent_scaffold import PreAgentError, PreAgentRun, QwenScaffoldModel, resolve_committed_package
from process_course_ppt import add_local_proxy_bypass, build_mineru_command, find_mineru
from scoped_instructions import InstructionResolver, instruction_context_blocks

PREPARATION_STATUSES = {
    "created",
    "material_uploaded",
    "preparing",
    "ready",
    "outdated",
    "failed",
}
SESSION_STATUSES = {"created", "live", "paused", "completed", "aborted"}
PREPARED_FILES = {
    "scaffold": "prepared/lecture_scaffold.json",
    "markdown": "prepared/lecture_scaffold.md",
    "hotwords": "prepared/hotwords.json",
    "asr_context": "prepared/asr_context.txt",
}
SAFE_ID = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")
SESSION_ID = re.compile(r"^[A-Za-z0-9._-]{1,128}$")
MAX_MATERIAL_BYTES = 200 * 1024 * 1024
PPTX_CONVERSION_TIMEOUT_SECONDS = 120.0


class WorkspaceCatalogError(ValueError):
    pass


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _slug(value: str) -> str:
    result = re.sub(r"[^a-z0-9]+", "-", value.strip().lower()).strip("-")
    return result[:64] or "untitled"


def _write_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    with temporary.open("xb") as handle:
        handle.write(
            (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
        )
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)
    directory = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


class WorkspaceCatalog:
    def __init__(self, project_root: Path) -> None:
        self.project_root = project_root.resolve()
        self.courses_root = self.project_root / "courses"
        self._lock = threading.RLock()

    def list_courses(self) -> list[dict[str, Any]]:
        with self._lock:
            index = self._read_json(self.courses_root / "index.json")
            return [self.get_course(course_id) for course_id in index["course_ids"]]

    def get_course(self, course_id: str) -> dict[str, Any]:
        with self._lock:
            course = self._read_json(self._course_path(course_id) / "course.json")
            lectures = [
                self.get_lecture(course_id, lecture_id)
                for lecture_id in course.get("lecture_ids", [])
            ]
            return {**course, "lectures": lectures}

    def get_lecture(self, course_id: str, lecture_id: str) -> dict[str, Any]:
        with self._lock:
            course = self._read_json(self._course_path(course_id) / "course.json")
            if lecture_id not in course.get("lecture_ids", []):
                raise WorkspaceCatalogError("Lecture does not exist")
            metadata_path = self._lecture_metadata_path(course_id, lecture_id)
            lecture = self._read_json(metadata_path)
            lecture = self._migrate_legacy_lecture(course_id, lecture)
            lecture = self._resolved_lecture(course_id, lecture)
            return lecture

    def create_course(
        self, *, title: str, code: str | None = None, description: str | None = None
    ) -> dict[str, Any]:
        title = title.strip()
        if not title:
            raise WorkspaceCatalogError("Course name is required")
        with self._lock:
            index_path = self.courses_root / "index.json"
            index = self._read_json(index_path)
            base = _slug(title)
            course_id = self._unique_id(base, set(index["course_ids"]))
            course = {
                "schema_version": 1,
                "id": course_id,
                "title": title,
                "code": (code or "").strip() or None,
                "description": (description or "").strip() or None,
                "created_at": _now(),
                "lecture_ids": [],
            }
            path = self._course_path(course_id)
            path.mkdir(parents=True, exist_ok=False)
            _write_json(path / "course.json", course)
            _write_json(index_path, {**index, "course_ids": [*index["course_ids"], course_id]})
            return {**course, "lectures": []}

    def update_course(
        self,
        course_id: str,
        *,
        title: str,
        code: str | None = None,
        description: str | None = None,
    ) -> dict[str, Any]:
        title = title.strip()
        if not title:
            raise WorkspaceCatalogError("Course name is required")
        with self._lock:
            path = self._course_path(course_id) / "course.json"
            course = self._read_json(path)
            course["title"] = title
            course["code"] = (code or "").strip() or None
            course["description"] = (description or "").strip() or None
            _write_json(path, course)
            return self.get_course(course_id)

    def delete_course(self, course_id: str) -> None:
        with self._lock:
            index_path = self.courses_root / "index.json"
            index = self._read_json(index_path)
            if course_id not in index.get("course_ids", []):
                raise WorkspaceCatalogError("Course does not exist")
            course = self.get_course(course_id)
            for lecture in course["lectures"]:
                self._assert_lecture_deletable(course_id, lecture)
            index["course_ids"] = [
                item for item in index["course_ids"] if item != course_id
            ]
            _write_json(index_path, index)
            shutil.rmtree(self._course_path(course_id), ignore_errors=True)
            shutil.rmtree(self.project_root / "data" / course_id, ignore_errors=True)

    def create_lecture(
        self, course_id: str, *, number: str, title: str
    ) -> dict[str, Any]:
        number, title = number.strip(), title.strip()
        if not number or not title:
            raise WorkspaceCatalogError("Lecture number and title are required")
        with self._lock:
            course_path = self._course_path(course_id)
            course = self._read_json(course_path / "course.json")
            lecture_id = self._unique_id(
                f"lecture-{_slug(number)}-{_slug(title)}",
                set(course.get("lecture_ids", [])),
            )
            workspace_dir = lecture_id
            lecture_root = course_path / "lectures" / workspace_dir
            lecture_root.mkdir(parents=True, exist_ok=False)
            lecture = {
                "schema_version": 1,
                "id": lecture_id,
                "course_id": course_id,
                "workspace_dir": workspace_dir,
                "number": number,
                "title": title,
                "preparationStatus": "created",
                "created_at": _now(),
                "prepared_at": None,
                "material": None,
                "preparation": {"error": None},
            }
            _write_json(lecture_root / "lecture.json", lecture)
            course["lecture_ids"] = [*course.get("lecture_ids", []), lecture_id]
            _write_json(course_path / "course.json", course)
            return self._resolved_lecture(course_id, lecture)

    def update_lecture(
        self, course_id: str, lecture_id: str, *, number: str, title: str
    ) -> dict[str, Any]:
        number, title = number.strip(), title.strip()
        if not number or not title:
            raise WorkspaceCatalogError("Lecture number and title are required")
        with self._lock:
            lecture = self.get_lecture(course_id, lecture_id)
            lecture["number"] = number
            lecture["title"] = title
            self._save_lecture(course_id, lecture)
            return self.get_lecture(course_id, lecture_id)

    def delete_lecture(self, course_id: str, lecture_id: str) -> None:
        with self._lock:
            course_path = self._course_path(course_id)
            course = self._read_json(course_path / "course.json")
            if lecture_id not in course.get("lecture_ids", []):
                raise WorkspaceCatalogError("Lecture does not exist")
            lecture = self.get_lecture(course_id, lecture_id)
            self._assert_lecture_deletable(course_id, lecture)
            lecture_root = self.lecture_root(course_id, lecture_id)
            course["lecture_ids"] = [
                item for item in course["lecture_ids"] if item != lecture_id
            ]
            _write_json(course_path / "course.json", course)
            shutil.rmtree(lecture_root, ignore_errors=True)
            shutil.rmtree(
                self.project_root / "data" / course_id / lecture_id,
                ignore_errors=True,
            )

    def upload_material(
        self, course_id: str, lecture_id: str, *, filename: str, content: bytes
    ) -> dict[str, Any]:
        suffix = Path(filename).suffix.lower()
        if suffix not in {".pdf", ".pptx"}:
            raise WorkspaceCatalogError("Only PDF and PPTX course materials are supported")
        if not content or len(content) > MAX_MATERIAL_BYTES:
            raise WorkspaceCatalogError("Course material must be between 1 byte and 200 MB")
        with self._lock:
            lecture = self.get_lecture(course_id, lecture_id)
            if lecture["preparationStatus"] == "preparing":
                raise WorkspaceCatalogError("Material cannot be replaced in the current state")
            root = self.lecture_root(course_id, lecture_id)
        source = root / "source"
        source.mkdir(parents=True, exist_ok=True)
        staging = source / f".material-{uuid.uuid4().hex}"
        staging.mkdir()
        try:
            stored = staging / f"uploaded{suffix}"
            stored.write_bytes(content)
            pdf_path = (
                self._convert_pptx(stored, staging) if suffix == ".pptx" else stored
            )
            staged_pdf = staging / "original.pdf"
            if pdf_path.resolve() != staged_pdf.resolve():
                shutil.copy2(pdf_path, staged_pdf)
            with self._lock:
                lecture = self.get_lecture(course_id, lecture_id)
                if lecture["preparationStatus"] == "preparing":
                    raise WorkspaceCatalogError(
                        "Material cannot be published while preparation is running"
                    )
                os.replace(stored, source / f"uploaded{suffix}")
                os.replace(staged_pdf, source / "original.pdf")
                lecture["material"] = {
                    "filename": Path(filename).name,
                    "content_type": "application/pdf" if suffix == ".pdf" else "application/vnd.openxmlformats-officedocument.presentationml.presentation",
                    "size_bytes": len(content),
                    "sha256": _sha256_bytes(content),
                    "uploaded_at": _now(),
                    "pdf_path": "source/original.pdf",
                }
                lecture["preparationStatus"] = (
                    "outdated" if lecture.get("prepared_at") else "material_uploaded"
                )
                lecture["preparation"] = {
                    **lecture.get("preparation", {}),
                    "error": None,
                }
                self._save_lecture(course_id, lecture)
                return self._resolved_lecture(course_id, lecture)
        finally:
            shutil.rmtree(staging, ignore_errors=True)

    def mark_preparing(
        self,
        course_id: str,
        lecture_id: str,
        *,
        prompt_version_id: str | None = None,
        instruction_snapshot: dict[str, Any] | None = None,
        execution_backend: str = "qwen",
    ) -> dict[str, Any]:
        with self._lock:
            lecture = self.get_lecture(course_id, lecture_id)
            if lecture["preparationStatus"] not in {
                "material_uploaded",
                "ready",
                "outdated",
                "failed",
            }:
                raise WorkspaceCatalogError("Lecture is not ready for preparation")
            lecture["preparationStatus"] = "preparing"
            lecture["preparation"] = {
                "started_at": _now(),
                "error": None,
                "preAgentPromptVersionId": prompt_version_id,
                "instructionSnapshot": instruction_snapshot,
                "executionBackend": execution_backend,
            }
            self._save_lecture(course_id, lecture)
            return self._resolved_lecture(course_id, lecture)

    def mark_prepared(self, course_id: str, lecture_id: str) -> None:
        with self._lock:
            lecture = self.get_lecture(course_id, lecture_id)
            lecture["preparationStatus"] = "ready"
            lecture["prepared_at"] = _now()
            lecture["preparation"] = {
                **lecture.get("preparation", {}),
                "error": None,
            }
            try:
                lecture["preparation"]["committed"] = (
                    self.committed_preparation_snapshot(course_id, lecture_id)
                )
            except WorkspaceCatalogError:
                # Migrated workspaces and direct catalog tests may predate the
                # persistent pipeline. Production Session creation is strict.
                pass
            self._save_lecture(course_id, lecture)

    def committed_preparation_snapshot(
        self, course_id: str, lecture_id: str
    ) -> dict[str, Any]:
        """Resolve and copy the current authoritative preparation generation."""

        root = self.lecture_root(course_id, lecture_id)
        try:
            package = resolve_committed_package(root)
        except ValueError as exc:
            raise WorkspaceCatalogError(
                f"Lecture preparation is not a valid committed package: {exc}"
            ) from exc
        manifest = self._read_json(root / package["manifest_path"])
        artifacts = {
            str(item["path"]): str(item["sha256"])
            for item in manifest["artifacts"]
            if isinstance(item, dict)
        }
        hotwords = self._read_json(root / PREPARED_FILES["hotwords"])
        terms: list[str] = []
        vocabulary: dict[str, int] = {}
        canonical_terms: dict[str, str] = {}
        for item in hotwords.get("hotwords", []):
            if not isinstance(item, dict):
                continue
            term = normalize_asr_hotword(str(item.get("term", "")))
            if not is_safe_asr_hotword(term):
                continue
            normalized = term.casefold()
            weight = hotword_bias_weight(
                term, str(item.get("priority", "medium"))
            )
            canonical = canonical_terms.get(normalized)
            if canonical is None:
                canonical_terms[normalized] = term
                terms.append(term)
                vocabulary[term] = weight
            else:
                # Duplicate spellings are one Qwen term; keep the strongest
                # PreAgent priority while preserving the first display form.
                vocabulary[canonical] = max(vocabulary[canonical], weight)
        return {
            "runId": package["run_id"],
            "manifestPath": package["manifest_path"],
            "manifestSha256": package["manifest_sha256"],
            "inputFingerprint": manifest["input_fingerprint"],
            "artifactHashes": artifacts,
            "hotwordsSnapshot": terms,
            "hotwordVocabularySnapshot": vocabulary,
            "capturedAt": _now(),
        }

    def mark_preparation_failed(
        self, course_id: str, lecture_id: str, message: str
    ) -> None:
        with self._lock:
            lecture = self.get_lecture(course_id, lecture_id)
            lecture["preparationStatus"] = "failed"
            lecture["preparation"] = {
                **lecture.get("preparation", {}),
                "error": message[:500],
            }
            self._save_lecture(course_id, lecture)

    def list_sessions(self, course_id: str, lecture_id: str) -> list[dict[str, Any]]:
        with self._lock:
            sessions_root = self.lecture_root(course_id, lecture_id) / "sessions"
            sessions: list[dict[str, Any]] = []
            if sessions_root.is_dir():
                for path in sessions_root.glob("*/session.json"):
                    session = self._read_json(path)
                    self._validate_session(session, course_id, lecture_id)
                    sessions.append(session)
            return sorted(
                sessions,
                key=lambda item: (str(item.get("createdAt", "")), str(item["id"])),
            )

    def get_session(
        self, course_id: str, lecture_id: str, session_id: str
    ) -> dict[str, Any]:
        with self._lock:
            path = self._session_path(course_id, lecture_id, session_id)
            session = self._read_json(path)
            self._validate_session(session, course_id, lecture_id)
            return session

    def get_current_session(
        self, course_id: str, lecture_id: str
    ) -> dict[str, Any] | None:
        sessions = self.list_sessions(course_id, lecture_id)
        return next(
            (
                item
                for item in reversed(sessions)
                if item["status"] in {"live", "paused"}
            ),
            None,
        )

    def start_session(
        self,
        course_id: str,
        lecture_id: str,
        *,
        agent1_prompt_version_id: str | None = None,
        agent1_model_config_snapshot: dict[str, Any] | None = None,
        agent1_boundary_prompt_version_id: str | None = None,
        agent1_boundary_model_config_snapshot: dict[str, Any] | None = None,
        instruction_snapshot: dict[str, Any] | None = None,
        preparation_snapshot: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        with self._lock:
            lecture = self.get_lecture(course_id, lecture_id)
            if lecture["preparationStatus"] != "ready":
                raise WorkspaceCatalogError("Only a prepared lecture can start a class")
            current = self.get_current_session(course_id, lecture_id)
            if current is not None:
                if current["status"] == "paused":
                    raise WorkspaceCatalogError("Resume the paused session before starting a new one")
                raise WorkspaceCatalogError("A class session is already live")
            session_id = uuid.uuid4().hex
            now = _now()
            runtime = Path("data") / course_id / lecture_id / session_id
            session = {
                "schema_version": 2,
                "id": session_id,
                "courseId": course_id,
                "lectureId": lecture_id,
                "status": "live",
                "runtimeState": "disconnected",
                "connectionGeneration": 0,
                "createdAt": now,
                "startedAt": now,
                "endedAt": None,
                "pausedAt": None,
                "pauseIntervals": [],
                "activeCaptureSeconds": 0.0,
                "lastResumedAt": now,
                "transcriptPath": str(runtime / "realtime_final.json"),
                "translationPath": str(runtime / "realtime_final.json"),
                "realtimeSnapshotsPath": str(runtime / "realtime_snapshots.jsonl"),
                "realtimeLatestPath": str(runtime / "realtime_latest.json"),
                "realtimeProtocolVersion": 1,
                "realtimeModels": {
                    "asr": "qwen-audio-3.1-asr-flash-streaming",
                    "translation": "qwen3.7-flash",
                    "semanticPunctuation": True,
                },
                "agent1Mode": "detached",
                "agent1NotesPath": str(runtime / "lecture_dense_transcript.jsonl"),
                "agent1PromptVersionId": agent1_prompt_version_id,
                "agent1ModelConfigSnapshot": agent1_model_config_snapshot,
                "agent1BoundaryPromptVersionId": agent1_boundary_prompt_version_id,
                "agent1BoundaryModelConfigSnapshot": (
                    agent1_boundary_model_config_snapshot
                ),
                "instructionSnapshot": instruction_snapshot,
                "preparationSnapshot": preparation_snapshot,
                "preparationRunId": (
                    preparation_snapshot.get("runId")
                    if isinstance(preparation_snapshot, dict)
                    else None
                ),
                "hotwordsSnapshot": (
                    list(preparation_snapshot.get("hotwordsSnapshot", []))
                    if isinstance(preparation_snapshot, dict)
                    else []
                ),
                "hotwordVocabularySnapshot": (
                    dict(preparation_snapshot.get("hotwordVocabularySnapshot", {}))
                    if isinstance(preparation_snapshot, dict)
                    else {}
                ),
                "agent1JobStatus": {
                    "pending": 0,
                    "running": 0,
                    "written": 0,
                    "failed": 0,
                },
                "agent1SourceStatus": {
                    "sourceCursor": 0,
                    "rawSourceHead": 0,
                    "bufferedSourceCount": 0,
                    "durableJobCount": 0,
                    "pendingJobs": 0,
                    "runningJobs": 0,
                    "failedJobs": 0,
                },
                "notesDrained": False,
            }
            _write_json(self._session_path(course_id, lecture_id, session_id), session)
            return session

    def pause_session(
        self,
        course_id: str,
        lecture_id: str,
        session_id: str,
        *,
        job_status: dict[str, int] | None = None,
        source_status: dict[str, int] | None = None,
    ) -> dict[str, Any]:
        with self._lock:
            session = self.get_session(course_id, lecture_id, session_id)
            if session["status"] == "paused":
                return session
            if session["status"] != "live":
                raise WorkspaceCatalogError(
                    "Session cannot be paused from its current state"
                )
            now = _now()
            self._accrue_active_duration(session, now)
            session["status"] = "paused"
            session["runtimeState"] = "paused"
            session["pausedAt"] = now
            session.setdefault("pauseIntervals", []).append(
                {"startedAt": now, "endedAt": None}
            )
            session["lastResumedAt"] = None
            if job_status is not None:
                session["agent1JobStatus"] = job_status
            if source_status is not None:
                session["agent1SourceStatus"] = source_status
            _write_json(self._session_path(course_id, lecture_id, session_id), session)
            return session

    def resume_session(
        self, course_id: str, lecture_id: str, session_id: str
    ) -> dict[str, Any]:
        with self._lock:
            session = self.get_session(course_id, lecture_id, session_id)
            if session["status"] != "paused":
                raise WorkspaceCatalogError(
                    "Session cannot be resumed from its current state"
                )
            now = _now()
            intervals = session.setdefault("pauseIntervals", [])
            if intervals and intervals[-1].get("endedAt") is None:
                intervals[-1]["endedAt"] = now
            session["status"] = "live"
            session["runtimeState"] = "disconnected"
            session["pausedAt"] = None
            session["lastResumedAt"] = now
            _write_json(self._session_path(course_id, lecture_id, session_id), session)
            return session

    def complete_session(
        self,
        course_id: str,
        lecture_id: str,
        session_id: str,
        *,
        job_status: dict[str, int] | None = None,
        notes_drained: bool | None = None,
        source_status: dict[str, int] | None = None,
    ) -> dict[str, Any]:
        with self._lock:
            session = self.get_session(course_id, lecture_id, session_id)
            if session["status"] == "completed":
                return session
            if session["status"] not in {"live", "paused"}:
                raise WorkspaceCatalogError(
                    "Session cannot be completed from its current state"
                )
            now = _now()
            if session["status"] == "live":
                self._accrue_active_duration(session, now)
            else:
                intervals = session.get("pauseIntervals", [])
                if intervals and intervals[-1].get("endedAt") is None:
                    intervals[-1]["endedAt"] = now
            session["status"] = "completed"
            session["runtimeState"] = "stopped"
            session["endedAt"] = now
            session["lastResumedAt"] = None
            if job_status is not None:
                session["agent1JobStatus"] = job_status
            if notes_drained is not None:
                session["notesDrained"] = notes_drained
            if source_status is not None:
                session["agent1SourceStatus"] = source_status
            _write_json(self._session_path(course_id, lecture_id, session_id), session)
            return session

    def begin_session_connection(
        self, course_id: str, lecture_id: str, session_id: str
    ) -> dict[str, Any]:
        with self._lock:
            session = self.get_session(course_id, lecture_id, session_id)
            if session["status"] != "live":
                raise WorkspaceCatalogError("Session is not live")
            session["connectionGeneration"] = int(
                session.get("connectionGeneration", 0)
            ) + 1
            session["runtimeState"] = "connected"
            session["connectedAt"] = _now()
            _write_json(self._session_path(course_id, lecture_id, session_id), session)
            return session

    def set_session_runtime_state(
        self,
        course_id: str,
        lecture_id: str,
        session_id: str,
        state: str,
        *,
        expected_generation: int | None = None,
        **values: Any,
    ) -> dict[str, Any]:
        with self._lock:
            session = self.get_session(course_id, lecture_id, session_id)
            if expected_generation is not None and int(
                session.get("connectionGeneration", 0)
            ) != expected_generation:
                return session
            session["runtimeState"] = state
            session.update(values)
            _write_json(self._session_path(course_id, lecture_id, session_id), session)
            return session

    @staticmethod
    def _accrue_active_duration(session: dict[str, Any], ended_at: str) -> None:
        resumed = session.get("lastResumedAt") or session.get("startedAt")
        if not isinstance(resumed, str):
            return
        try:
            elapsed = max(
                0.0,
                (
                    datetime.fromisoformat(ended_at)
                    - datetime.fromisoformat(resumed)
                ).total_seconds(),
            )
        except ValueError:
            return
        session["activeCaptureSeconds"] = round(
            float(session.get("activeCaptureSeconds", 0.0)) + elapsed, 3
        )

    def abort_session_if_live(
        self, course_id: str, lecture_id: str, session_id: str
    ) -> dict[str, Any]:
        with self._lock:
            session = self.get_session(course_id, lecture_id, session_id)
            if session["status"] != "live":
                return session
            return self._transition_session(
                course_id, lecture_id, session_id, allowed={"live"}, target="aborted"
            )

    def bind_agent1_prompt_version(
        self,
        course_id: str,
        lecture_id: str,
        session_id: str,
        prompt_version_id: str,
    ) -> dict[str, Any]:
        with self._lock:
            session = self.get_session(course_id, lecture_id, session_id)
            existing = session.get("agent1PromptVersionId")
            if existing is not None and existing != prompt_version_id:
                raise WorkspaceCatalogError(
                    "A running session cannot change its Agent1 prompt version"
                )
            session["agent1PromptVersionId"] = prompt_version_id
            _write_json(self._session_path(course_id, lecture_id, session_id), session)
            return session

    def bind_session_instruction_snapshot(
        self,
        course_id: str,
        lecture_id: str,
        session_id: str,
        instruction_snapshot: dict[str, Any],
    ) -> dict[str, Any]:
        with self._lock:
            session = self.get_session(course_id, lecture_id, session_id)
            existing = session.get("instructionSnapshot")
            if existing is not None and existing != instruction_snapshot:
                raise WorkspaceCatalogError(
                    "A running session cannot change its instruction snapshot"
                )
            session["instructionSnapshot"] = instruction_snapshot
            _write_json(self._session_path(course_id, lecture_id, session_id), session)
            return session

    def bind_agent1_boundary_snapshot(
        self,
        course_id: str,
        lecture_id: str,
        session_id: str,
        *,
        prompt_version_id: str,
        model_config: dict[str, Any],
    ) -> dict[str, Any]:
        """Backfill and then freeze Boundary Detector settings for a legacy session."""

        with self._lock:
            session = self.get_session(course_id, lecture_id, session_id)
            existing_prompt = session.get("agent1BoundaryPromptVersionId")
            existing_model = session.get("agent1BoundaryModelConfigSnapshot")
            if existing_prompt is not None and existing_prompt != prompt_version_id:
                raise WorkspaceCatalogError(
                    "A running session cannot change its Boundary Detector prompt version"
                )
            if existing_model is not None and existing_model != model_config:
                raise WorkspaceCatalogError(
                    "A running session cannot change its Boundary Detector model snapshot"
                )
            session["agent1BoundaryPromptVersionId"] = prompt_version_id
            session["agent1BoundaryModelConfigSnapshot"] = dict(model_config)
            _write_json(self._session_path(course_id, lecture_id, session_id), session)
            return session

    def recover_transient_states(self) -> None:
        """Resolve process-owned states left behind by an interrupted server."""

        with self._lock:
            for course in self.list_courses():
                for lecture in course["lectures"]:
                    if lecture["preparationStatus"] == "preparing":
                        state_path = self.lecture_root(course["id"], lecture["id"]) / "runtime/preagent_state.json"
                        lock_path = state_path.with_name("preagent.lock")
                        if lock_path.is_file():
                            # Another manager may inspect the same workspace while
                            # its original worker is still running. A held worker
                            # lock is evidence of activity, not a stale state.
                            with lock_path.open("r+b") as lock:
                                try:
                                    fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                                except BlockingIOError:
                                    continue
                                else:
                                    fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
                        if state_path.is_file():
                            try:
                                state = self._read_json(state_path)
                                if state.get("status") == "running":
                                    state["status"] = "failed"
                                    state["error"] = "Preparation was interrupted. Retry when ready."
                                    current = state.get("current_stage")
                                    if current in state.get("stages", {}):
                                        state["stages"][current]["status"] = "failed"
                                        state["stages"][current]["error"] = state["error"]
                                    _write_json(state_path, state)
                            except WorkspaceCatalogError:
                                pass
                        lecture["preparationStatus"] = "failed"
                        lecture["preparation"] = {
                            **lecture.get("preparation", {}),
                            "error": "Preparation was interrupted. Retry when ready.",
                        }
                        self._save_lecture(course["id"], lecture)

    def _assert_lecture_deletable(
        self, course_id: str, lecture: dict[str, Any]
    ) -> None:
        if lecture["preparationStatus"] == "preparing":
            raise WorkspaceCatalogError(
                "A lecture cannot be deleted while preparation is running"
            )
        if any(
            session["status"] in {"live", "paused"}
            for session in self.list_sessions(course_id, lecture["id"])
        ):
            raise WorkspaceCatalogError(
                "A lecture with a live or paused class session cannot be deleted"
            )

    def lecture_root(self, course_id: str, lecture_id: str) -> Path:
        course = self._read_json(self._course_path(course_id) / "course.json")
        if lecture_id not in course.get("lecture_ids", []):
            raise WorkspaceCatalogError("Lecture does not exist")
        lecture_ids = {
            item: self._read_json(self._lecture_metadata_path(course_id, item))
            for item in course["lecture_ids"]
        }
        lecture = lecture_ids[lecture_id]
        workspace_dir = lecture.get("workspace_dir", lecture_id)
        if not isinstance(workspace_dir, str) or Path(workspace_dir).parts != (workspace_dir,):
            raise WorkspaceCatalogError("Lecture workspace is invalid")
        return self._course_path(course_id) / "lectures" / workspace_dir

    def artifact_path(self, course_id: str, lecture_id: str, artifact: str) -> Path:
        relative = {"material": "source/original.pdf", **PREPARED_FILES}.get(artifact)
        if relative is None:
            raise WorkspaceCatalogError("Unknown lecture artifact")
        path = self.lecture_root(course_id, lecture_id) / relative
        if not path.is_file():
            raise WorkspaceCatalogError("Lecture artifact is not available")
        return path

    def _resolved_lecture(self, course_id: str, lecture: dict[str, Any]) -> dict[str, Any]:
        status = lecture.get("preparationStatus")
        if status not in PREPARATION_STATUSES:
            raise WorkspaceCatalogError("Lecture preparation status is invalid")
        root = self._course_path(course_id) / "lectures" / lecture["workspace_dir"]
        outputs = {name: (root / relative).is_file() for name, relative in PREPARED_FILES.items()}
        stage = "parsing_slides" if status == "preparing" else None
        state_path = root / "runtime/preagent_state.json"
        if status in {"preparing", "failed", "ready"} and state_path.is_file():
            try:
                run_state = self._read_json(state_path)
                if status != "preparing" or run_state.get("status") == "running":
                    stage = run_state.get("current_stage")
            except WorkspaceCatalogError:
                pass
        if status == "ready" and not all(outputs.values()):
            status = "material_uploaded" if lecture.get("material") else "created"
        sessions = self.list_sessions(course_id, lecture["id"])
        active_session = next(
            (item for item in reversed(sessions) if item["status"] in {"live", "paused"}),
            None,
        )
        last_session = sessions[-1] if sessions else None
        base = f"/api/courses/{course_id}/lectures/{lecture['id']}/files"
        return {
            **lecture,
            "preparationStatus": status,
            "activeSession": active_session,
            "lastSession": last_session,
            "sessionCount": len(sessions),
            "outputs": outputs,
            "preparationStage": stage,
            "urls": {
                "material": f"{base}/material" if lecture.get("material") else None,
                "scaffold": f"{base}/scaffold" if outputs["scaffold"] else None,
                "markdown": f"{base}/markdown" if outputs["markdown"] else None,
                "hotwords": f"{base}/hotwords" if outputs["hotwords"] else None,
                "asr_context": f"{base}/asr_context" if outputs["asr_context"] else None,
            },
        }

    def _lecture_metadata_path(self, course_id: str, lecture_id: str) -> Path:
        course = self._read_json(self._course_path(course_id) / "course.json")
        if lecture_id not in course.get("lecture_ids", []):
            raise WorkspaceCatalogError("Lecture does not exist")
        for candidate in (self._course_path(course_id) / "lectures").glob("*/lecture.json"):
            value = self._read_json(candidate)
            if value.get("id") == lecture_id:
                return candidate
        raise WorkspaceCatalogError("Lecture metadata is missing")

    def _save_lecture(self, course_id: str, lecture: dict[str, Any]) -> None:
        clean = {
            key: value
            for key, value in lecture.items()
            if key
            not in {"outputs", "urls", "activeSession", "lastSession", "sessionCount"}
        }
        _write_json(self._lecture_metadata_path(course_id, lecture["id"]), clean)

    def _transition_session(
        self,
        course_id: str,
        lecture_id: str,
        session_id: str,
        *,
        allowed: set[str],
        target: str,
    ) -> dict[str, Any]:
        with self._lock:
            session = self.get_session(course_id, lecture_id, session_id)
            if session["status"] not in allowed:
                raise WorkspaceCatalogError(
                    f"Session cannot transition from {session['status']} to {target}"
                )
            session["status"] = target
            if target == "live" and not session.get("startedAt"):
                session["startedAt"] = _now()
            if target in {"completed", "aborted"}:
                session["endedAt"] = _now()
            _write_json(self._session_path(course_id, lecture_id, session_id), session)
            return session

    def _session_path(self, course_id: str, lecture_id: str, session_id: str) -> Path:
        if not SESSION_ID.fullmatch(session_id):
            raise WorkspaceCatalogError("Session ID is invalid")
        return self.lecture_root(course_id, lecture_id) / "sessions" / session_id / "session.json"

    @staticmethod
    def _validate_session(
        session: dict[str, Any], course_id: str, lecture_id: str
    ) -> None:
        if (
            session.get("courseId") != course_id
            or session.get("lectureId") != lecture_id
            or session.get("status") not in SESSION_STATUSES
            or not isinstance(session.get("id"), str)
        ):
            raise WorkspaceCatalogError("Class session metadata is invalid")

    def _migrate_legacy_lecture(
        self, course_id: str, lecture: dict[str, Any]
    ) -> dict[str, Any]:
        if "preparationStatus" in lecture:
            return lecture
        legacy_status = lecture.get("status")
        if legacy_status not in {
            "created",
            "material_uploaded",
            "preparing",
            "ready",
            "live",
            "completed",
        }:
            raise WorkspaceCatalogError("Lecture status is invalid")
        lecture = dict(lecture)
        lecture.pop("status", None)
        lecture["schema_version"] = max(int(lecture.get("schema_version", 1)), 2)
        lecture["preparationStatus"] = (
            "ready" if legacy_status in {"live", "completed"} else legacy_status
        )
        if legacy_status in {"live", "completed"}:
            self._create_legacy_session(course_id, lecture, legacy_status)
        self._save_lecture(course_id, lecture)
        return lecture

    def _create_legacy_session(
        self, course_id: str, lecture: dict[str, Any], legacy_status: str
    ) -> None:
        runtime_root = self.project_root / "data" / course_id / lecture["id"]
        runtime_dirs = (
            sorted(
                (item for item in runtime_root.iterdir() if item.is_dir()),
                key=lambda item: item.stat().st_mtime,
            )
            if runtime_root.is_dir()
            else []
        )
        runtime = runtime_dirs[-1] if runtime_dirs else runtime_root / "legacy-session"
        session_id = runtime.name if SESSION_ID.fullmatch(runtime.name) else "legacy-session"
        path = (
            self._course_path(course_id)
            / "lectures"
            / lecture["workspace_dir"]
            / "sessions"
            / session_id
            / "session.json"
        )
        if path.is_file():
            return
        created = lecture.get("created_at") or _now()
        relative_runtime = Path("data") / course_id / lecture["id"] / session_id
        _write_json(
            path,
            {
                "schema_version": 1,
                "id": session_id,
                "courseId": course_id,
                "lectureId": lecture["id"],
                "status": legacy_status,
                "createdAt": created,
                "startedAt": created,
                "endedAt": _now() if legacy_status == "completed" else None,
                "transcriptPath": str(relative_runtime / "raw_sentences.jsonl"),
                "translationPath": str(relative_runtime / "raw_sentences.jsonl"),
                "agent1NotesPath": str(relative_runtime / "lecture_dense_transcript.jsonl"),
                "legacy": True,
            },
        )

    def _course_path(self, course_id: str) -> Path:
        if not SAFE_ID.fullmatch(course_id):
            raise WorkspaceCatalogError("Course ID is invalid")
        path = (self.courses_root / course_id).resolve()
        try:
            path.relative_to(self.courses_root)
        except ValueError as exc:
            raise WorkspaceCatalogError("Course ID is invalid") from exc
        return path

    @staticmethod
    def _read_json(path: Path) -> dict[str, Any]:
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise WorkspaceCatalogError(f"Workspace metadata is unavailable: {path.name}") from exc
        if not isinstance(value, dict):
            raise WorkspaceCatalogError("Workspace metadata must be an object")
        return value

    @staticmethod
    def _unique_id(base: str, existing: set[str]) -> str:
        candidate, suffix = base, 2
        while candidate in existing:
            candidate = f"{base[:58]}-{suffix}"
            suffix += 1
        return candidate

    def _convert_pptx(self, source: Path, output_dir: Path) -> Path:
        executable = shutil.which("soffice") or shutil.which("libreoffice")
        if not executable:
            raise WorkspaceCatalogError("PPTX conversion requires LibreOffice")
        try:
            completed = subprocess.run(
                [executable, "--headless", "--convert-to", "pdf", "--outdir", str(output_dir), str(source)],
                capture_output=True,
                text=True,
                check=False,
                timeout=PPTX_CONVERSION_TIMEOUT_SECONDS,
            )
        except subprocess.TimeoutExpired as exc:
            raise WorkspaceCatalogError(
                f"PPTX conversion timed out after {PPTX_CONVERSION_TIMEOUT_SECONDS:g} seconds"
            ) from exc
        converted = output_dir / f"{source.stem}.pdf"
        if completed.returncode != 0 or not converted.is_file():
            raise WorkspaceCatalogError("PPTX could not be converted to PDF")
        return converted


class PreparationManager:
    def __init__(
        self,
        catalog: WorkspaceCatalog,
        prompt_store: Any | None = None,
        instruction_resolver: InstructionResolver | None = None,
    ) -> None:
        self.catalog = catalog
        self.prompt_store = prompt_store
        self.instruction_resolver = instruction_resolver
        self._tasks: dict[tuple[str, str], asyncio.Task[None]] = {}
        self.catalog.recover_transient_states()

    def start(self, course_id: str, lecture_id: str, *, backend: str = "qwen") -> dict[str, Any]:
        key = (course_id, lecture_id)
        if (task := self._tasks.get(key)) and not task.done():
            raise WorkspaceCatalogError("Preparation is already running")
        if backend not in {"qwen", "codex"}:
            raise WorkspaceCatalogError("Preparation backend must be qwen or codex")
        if backend == "codex":
            from codex_scaffold import CodexUnavailableError, require_codex_login
            try:
                require_codex_login()
            except CodexUnavailableError as exc:
                raise WorkspaceCatalogError(str(exc)) from exc
        prompt_version_id = (
            self.prompt_store.get_active_version("pre-agent")["id"]
            if self.prompt_store is not None
            else None
        )
        instruction_snapshot = (
            self.instruction_resolver.resolve(
                agent_definition=self.prompt_store.get_agent(
                    "pre-agent", include_content=False
                ),
                course_id=course_id,
                lecture_id=lecture_id,
            )
            if self.instruction_resolver is not None
            and self.prompt_store is not None
            else None
        )
        lecture = self.catalog.mark_preparing(
            course_id,
            lecture_id,
            prompt_version_id=prompt_version_id,
            instruction_snapshot=instruction_snapshot,
            execution_backend=backend,
        )
        task = asyncio.create_task(
            self._run(
                course_id, lecture_id, prompt_version_id, instruction_snapshot
            )
        )
        self._tasks[key] = task
        task.add_done_callback(lambda _task: self._tasks.pop(key, None))
        return lecture

    async def _run(
        self,
        course_id: str,
        lecture_id: str,
        prompt_version_id: str | None,
        instruction_snapshot: dict[str, Any] | None,
    ) -> None:
        try:
            await asyncio.to_thread(
                self._prepare,
                course_id,
                lecture_id,
                prompt_version_id,
                instruction_snapshot,
            )
            self.catalog.mark_prepared(course_id, lecture_id)
        except Exception as exc:  # noqa: BLE001 - persisted as a bounded UI error
            self.catalog.mark_preparation_failed(course_id, lecture_id, str(exc))

    def _prepare(
        self,
        course_id: str,
        lecture_id: str,
        prompt_version_id: str | None,
        instruction_snapshot: dict[str, Any] | None,
    ) -> None:
        root = self.catalog.lecture_root(course_id, lecture_id)
        model_name = "qwen3.8-max"
        base_prompt = ""
        if self.prompt_store is not None and prompt_version_id is not None:
            model_config = self.prompt_store.get_agent("pre-agent", include_content=False)["modelConfig"]
            model_name = str(model_config["model"])
            base_prompt = self.prompt_store.prompt_path("pre-agent", prompt_version_id).read_text(encoding="utf-8")
            if instruction_snapshot:
                base_prompt += "\n\n" + "\n\n".join(block.content for block in instruction_context_blocks(instruction_snapshot))
        lecture = self.catalog.get_lecture(course_id, lecture_id)
        if lecture.get("preparation", {}).get("executionBackend", "qwen") == "codex":
            from codex_scaffold import CodexScaffoldModel
            model = CodexScaffoldModel(base_prompt=base_prompt, workspace_root=root)
        else:
            model = QwenScaffoldModel(model_name, base_prompt=base_prompt, workspace_root=root)
        runner = PreAgentRun(
            root, lecture_id, lecture["title"],
            model,
            prompt_version=prompt_version_id or "knowledge-scaffold-v1",
        )
        runner.begin_stage("parsing_slides", "source/original.pdf")
        runner.state["stages"]["parsing_slides"]["attempts"] = 1
        runner.save()
        try:
            self._parse_material(root, course_id, lecture_id)
            asyncio.run(runner.run())
        except Exception as exc:
            if runner.state["status"] != "failed":
                runner.state["status"] = "failed"
                runner.state["error"] = str(exc)[:500]
                step = runner.state["stages"]["parsing_slides"]
                step["status"] = "failed"
                step["error"] = f"{type(exc).__name__}: {exc}"[:500]
                runner.save()
                raise PreAgentError("parsing_slides", "source/original.pdf", str(exc), 1) from exc
            raise

    def _parse_material(self, root: Path, course_id: str, lecture_id: str) -> None:
        source_pdf = self.catalog.artifact_path(course_id, lecture_id, "material")
        mineru = find_mineru(None, self.catalog.project_root)
        with tempfile.TemporaryDirectory(prefix="classroom-preparation-") as directory:
            output = Path(directory)
            command = build_mineru_command(
                mineru, source_pdf, output,
                backend="hybrid-engine", effort="medium",
                image_analysis=False, method="auto",
            )
            completed = subprocess.run(
                command,
                cwd=self.catalog.project_root,
                env=add_local_proxy_bypass(dict(os.environ)),
                capture_output=True, text=True, check=False,
            )
            if completed.returncode != 0:
                detail = (completed.stderr or completed.stdout or "").strip()[-300:]
                raise RuntimeError(f"MinerU failed with exit code {completed.returncode}: {detail}")
            candidates = sorted(output.rglob("*_content_list_v2.json"))
            if len(candidates) != 1:
                raise RuntimeError("MinerU did not produce one canonical content list")
            raw_content = candidates[0]
            canonical = root / "source" / "mineru"
            canonical.mkdir(parents=True, exist_ok=True)
            shutil.copy2(raw_content, canonical / "content_list_v2.json")
            images = raw_content.parent / "images"
            if images.is_dir():
                shutil.copytree(images, canonical / "images", dirs_exist_ok=True)

        normalized = load_and_normalize_mineru_file(
            root / "source" / "mineru" / "content_list_v2.json",
            lecture_id=lecture_id,
        )
        render_visual_slides(normalized, source_pdf=source_pdf, workspace_root=root)
        write_normalized_document_atomic(
            normalized,
            root / "normalized" / "normalized_slides.json",
            images_dir=root / "source" / "mineru" / "images",
            workspace_root=root,
        )
