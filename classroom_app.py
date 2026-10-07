"""FastAPI host for the classroom workspace and WhisperLiveKit realtime path."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import logging
import mimetypes
import re
from dataclasses import replace
from pathlib import Path

import uvicorn
from fastapi import FastAPI, HTTPException, Request, WebSocket
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, Response

from agent_lab import (
    AgentLabConflictError,
    AgentLabError,
    AgentLabService,
    AgentLabStore,
)
from agent1_v1 import Agent1V1Run, ModelSpec, provider_from_spec
from agent2_asr import Agent2ASRRun, provider_for_agent2
from agent2_retrieval import read_jsonl
from postclass_backends import selected_spec
from codex_scaffold import CodexUnavailableError
from agent2_enrichment import is_running as enrichment_running
from classroom_realtime import create_realtime_engine, run_realtime_classroom
from config import Settings, load_settings
from scoped_instructions import (
    InstructionConflictError,
    InstructionError,
    InstructionResolver,
    InstructionStore,
    instruction_context_blocks,
)
from session_runtime import (
    DuplicateRuntimeConnectionError,
    SessionRuntimeOwner,
    SessionRuntimeRegistry,
)
from transcript import FinalTranscriptSink
from workspace_catalog import (
    PreparationManager,
    WorkspaceCatalog,
    WorkspaceCatalogError,
)

PROJECT_ROOT = Path(__file__).resolve().parent
FRONTEND_DIST = PROJECT_ROOT / "frontend" / "dist"
logger = logging.getLogger(__name__)
SESSION_CONTEXT_PATTERN = re.compile(r"^[A-Za-z0-9._-]{1,128}$")


def _configure_logging(*, debug: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if debug else logging.INFO,
        format="%(levelname)s %(name)s: %(message)s",
    )
    for logger_name in ("websockets.client", "websockets.server"):
        logging.getLogger(logger_name).setLevel(logging.WARNING)


def _frontend_file(request_path: str) -> Path | None:
    relative = (
        "index.html"
        if request_path in {"", "/", "index.html"}
        else "workspace.html"
        if request_path in {"agent-lab", "instructions"}
        or request_path == "courses"
        or request_path.startswith("courses/")
        else request_path.lstrip("/")
    )
    root = FRONTEND_DIST
    path = (root / relative).resolve()
    try:
        path.relative_to(root.resolve())
    except ValueError:
        return None
    return path if path.is_file() else None


def _session_settings(websocket: WebSocket, settings: Settings) -> Settings:
    """Bind one realtime connection to its explicitly selected course context."""

    course_id = websocket.query_params.get("course_id")
    lecture_id = websocket.query_params.get("lecture_id")
    if course_id is None and lecture_id is None:
        return settings
    if not course_id or not lecture_id:
        raise ValueError("course_id and lecture_id must be provided together")
    if not SESSION_CONTEXT_PATTERN.fullmatch(course_id):
        raise ValueError("invalid course_id")
    if not SESSION_CONTEXT_PATTERN.fullmatch(lecture_id):
        raise ValueError("invalid lecture_id")
    return replace(settings, course_id=course_id, lecture_id=lecture_id)



def create_app(
    settings: Settings,
    *,
    final_transcript_sink: FinalTranscriptSink | None = None,
    workspace_catalog: WorkspaceCatalog | None = None,
    preparation_manager: PreparationManager | None = None,
    agent_lab_store: AgentLabStore | None = None,
    agent_lab_service: AgentLabService | None = None,
    instruction_store: InstructionStore | None = None,
    instruction_resolver: InstructionResolver | None = None,
    runtime_registry: SessionRuntimeRegistry | None = None,
) -> FastAPI:
    app = FastAPI(title="Classroom Assistant")
    catalog = workspace_catalog or WorkspaceCatalog(PROJECT_ROOT)
    prompt_store = agent_lab_store or AgentLabStore(
        catalog.project_root, source_root=PROJECT_ROOT
    )
    scoped_store = instruction_store or InstructionStore(catalog.project_root, catalog)
    resolver = instruction_resolver or InstructionResolver(scoped_store)
    preparations = preparation_manager or PreparationManager(
        catalog, prompt_store, resolver
    )
    lab = agent_lab_service or AgentLabService(
        prompt_store, catalog, instruction_resolver=resolver
    )
    runtimes = runtime_registry or SessionRuntimeRegistry()
    realtime_engine = create_realtime_engine(settings)
    agent1_tasks: dict[str, asyncio.Task[dict[str, object]]] = {}
    agent2_tasks: dict[tuple[str, str, str], asyncio.Task] = {}
    agent1_backends: dict[str, str] = {}
    agent2_backends: dict[tuple[str, str, str], str] = {}
    agent1_specs: dict[str, ModelSpec] = {}
    agent2_specs: dict[tuple[str, str, str], ModelSpec] = {}

    def agent2_output(course_id: str, lecture_id: str, session_id: str) -> Path:
        session = catalog.get_session(course_id, lecture_id, session_id)
        return (catalog.project_root / str(session["transcriptPath"])).parent / "agent2"

    def agent2_snapshot(course_id: str, lecture_id: str, session_id: str) -> dict:
        output = agent2_output(course_id, lecture_id, session_id)
        agent1_manifest = output.parent / "agent1/manifest.json"
        ready = agent1_manifest.is_file() and json.loads(agent1_manifest.read_text(encoding="utf-8")).get("status") == "completed"
        first_task = agent1_tasks.get(session_id)
        if first_task and not first_task.done():
            ready = False
        manifest = output / "manifest.json"
        value = json.loads(manifest.read_text(encoding="utf-8")) if manifest.is_file() else {"status": "not_started"}
        task = agent2_tasks.get((course_id, lecture_id, session_id))
        if task and not task.done():
            spec = agent2_specs[(course_id, lecture_id, session_id)]
            value = {**value, "status": "processing", "provider": spec.provider, "model": spec.model}
        elif task and not task.cancelled() and task.exception():
            error = task.exception()
            value = {**value, "status": "failed", "root_cause": {"error_type": type(error).__name__, "error_message": str(error)}}
        elif value["status"] == "processing":
            value = {**value, "status": "failed", "root_cause": {"error_type": "INTERRUPTED", "error_message": "Correction was interrupted. Retry to reuse completed stages."}}
        return {**value, "agent1_ready": ready}

    def launch_agent2(course_id: str, lecture_id: str, session_id: str, backend: str = "qwen") -> dict:
        if backend not in {"qwen", "codex"}:
            raise ValueError("Execution backend must be qwen or codex")
        key = (course_id, lecture_id, session_id)
        session = catalog.get_session(*key)
        if session["status"] != "completed":
            raise ValueError("ASR correction requires a completed ClassSession")
        task = agent2_tasks.get(key)
        if task and not task.done():
            if agent2_backends.get(key) != backend:
                raise HTTPException(status_code=409, detail="ASR correction is running. Wait before changing its execution backend.")
            return agent2_snapshot(*key)
        if not agent2_snapshot(*key)["agent1_ready"]:
            raise ValueError("Complete Agent1 before starting ASR correction")
        if enrichment_running(agent2_output(*key).parent / "agent2_enrichment"):
            raise HTTPException(status_code=409, detail="Wait for Agent2 enrichment before rerunning ASR correction.")
        spec = selected_spec("asr", backend, ModelSpec(provider=os.getenv("AGENT2_PROVIDER", "alibaba_cloud_model_studio"),
            model=os.getenv("AGENT2_MODEL", "qwen3.8-max"),
            context_window_tokens=int(os.getenv("AGENT2_CONTEXT_WINDOW_TOKENS") or os.getenv("AGENT1_SEGMENTER_CONTEXT_WINDOW_TOKENS") or "0"),
            max_output_tokens=int(os.getenv("AGENT2_MAX_OUTPUT_TOKENS", "8192"))))
        if not spec.max_output_tokens or spec.max_output_tokens <= 0:
            raise ValueError("AGENT2_MAX_OUTPUT_TOKENS must be positive")
        snapshot = resolver.resolve(agent_definition={"agentId": "agent2", "contextPolicy": [
            "Faculty Instructions", "User Instructions", "Course Instructions", "Lecture Instructions"]},
            course_id=course_id, lecture_id=lecture_id)
        # Exclude resolvedAt from cache identity; unchanged instructions reuse results.
        instructions = [{key: item[key] for key in ("scope", "versionId", "content")} for item in snapshot["instructions"]]
        run = Agent2ASRRun(final_path=catalog.project_root / str(session["transcriptPath"]),
            lecture_root=catalog.lecture_root(course_id, lecture_id), output_dir=agent2_output(*key),
            course_id=course_id, lecture_id=lecture_id, session_id=session_id,
            provider=provider_for_agent2(spec), spec=spec, instructions=instructions,
            max_concurrency=int(os.getenv("AGENT2_MAX_CONCURRENCY", "2")))
        run.prepare()
        agent2_backends[key] = backend
        agent2_specs[key] = spec
        agent2_tasks[key] = asyncio.create_task(run.run(), name=f"agent2-asr-{session_id}")
        return {"status": "processing", "agent1_ready": True, "block_count": len(run.blocks),
                "provider": spec.provider, "model": spec.model}

    from agent2_enrichment_api import register_enrichment_api
    register_enrichment_api(app, catalog, agent2_snapshot)

    def agent1_run_for(course_id: str, lecture_id: str, session_id: str, backend: str = "qwen") -> Agent1V1Run:
        session = catalog.get_session(course_id, lecture_id, session_id)
        if session["status"] != "completed":
            raise ValueError("Agent1 requires a completed ClassSession")
        try:
            context_limit = int(os.getenv("AGENT1_SEGMENTER_CONTEXT_WINDOW_TOKENS", "0"))
            concurrency = int(os.getenv("AGENT1_SUMMARIZER_MAX_CONCURRENCY", "4"))
            max_output = int(os.getenv("AGENT1_SEGMENTER_MAX_OUTPUT_TOKENS", "4096"))
        except ValueError as exc:
            raise ValueError("Agent1 context window, output limit, and concurrency must be integers") from exc
        if (backend == "qwen" and context_limit <= 0) or concurrency <= 0 or max_output <= 0:
            raise ValueError("Configure positive Agent1 context window, output limit, and concurrency")
        model_config = prompt_store.get_agent("agent1", include_content=False)["modelConfig"]
        segmenter_spec = selected_spec("agent1", backend, ModelSpec(
            provider=model_config["provider"],
            model=model_config["model"],
            context_window_tokens=context_limit,
            max_output_tokens=max_output,
        ))
        summarizer_spec = ModelSpec(
            provider=segmenter_spec.provider,
            model=segmenter_spec.model,
        )
        transcript_path = catalog.project_root / str(session["transcriptPath"])
        output_dir = transcript_path.parent / "agent1"
        lecture = catalog.get_lecture(course_id, lecture_id)
        course = catalog.get_course(course_id)
        context = "; ".join(filter(None, (str(course.get("title") or ""), str(lecture.get("title") or ""))))
        return Agent1V1Run(
            final_path=transcript_path,
            output_dir=output_dir,
            course_id=course_id,
            lecture_id=lecture_id,
            session_id=session_id,
            segmenter=provider_from_spec(segmenter_spec),
            summarizer=provider_from_spec(summarizer_spec),
            segmenter_spec=segmenter_spec,
            summarizer_spec=summarizer_spec,
            lecture_context=context or None,
            max_concurrency=concurrency,
            runtime_dir=catalog.lecture_root(course_id, lecture_id) / "runtime" / "agent1" / session_id,
            allow_new_run=True,
        )

    def launch_agent1(course_id: str, lecture_id: str, session_id: str, backend: str = "qwen") -> dict[str, object]:
        if backend not in {"qwen", "codex"}:
            raise ValueError("Execution backend must be qwen or codex")
        existing = agent1_tasks.get(session_id)
        if existing is not None and not existing.done():
            if agent1_backends.get(session_id) != backend:
                raise HTTPException(status_code=409, detail="Agent1 is running. Wait before changing its execution backend.")
            return {"status": "processing", "session_id": session_id, "execution_backend": backend}
        downstream = agent2_tasks.get((course_id, lecture_id, session_id))
        if (downstream and not downstream.done()) or enrichment_running(agent2_output(course_id, lecture_id, session_id).parent / "agent2_enrichment"):
            raise HTTPException(status_code=409, detail="Wait for ASR correction and Agent2 enrichment before rerunning Agent1.")
        run = agent1_run_for(course_id, lecture_id, session_id, backend)
        agent1_backends[session_id] = backend
        agent1_specs[session_id] = run.segmenter_spec
        task = asyncio.create_task(run.run(), name=f"agent1-v1-{session_id}")
        def log_unhandled_failure(finished: asyncio.Task[dict[str, object]]) -> None:
            if finished.cancelled():
                return
            error = finished.exception()
            if error is not None:
                logger.error("Agent1 post-class task failed", exc_info=(type(error), error, error.__traceback__))
        task.add_done_callback(log_unhandled_failure)
        agent1_tasks[session_id] = task
        return {"status": "processing", "session_id": session_id,
                "segmenter_provider": run.segmenter_spec.provider, "segmenter_model": run.segmenter_spec.model}

    def build_runtime_owner(
        session_settings: Settings, session: dict[str, object]
    ) -> SessionRuntimeOwner:
        session_id = str(session["id"])
        return SessionRuntimeOwner(
            catalog=catalog,
            course_id=session_settings.course_id,
            lecture_id=session_settings.lecture_id,
            session_id=session_id,
            agent1_pipeline=None,
        )

    async def runtime_for(
        course_id: str, lecture_id: str, session_id: str
    ) -> SessionRuntimeOwner:
        session = catalog.get_session(course_id, lecture_id, session_id)
        runtime_settings = replace(settings, course_id=course_id, lecture_id=lecture_id)
        return await runtimes.get_or_create(
            course_id,
            lecture_id,
            session_id,
            lambda: build_runtime_owner(runtime_settings, session),
        )

    @app.on_event("startup")
    async def recover_interrupted_sessions() -> None:
        """Mark sessions left live by a previous server process reconnectable."""

        for course in catalog.list_courses():
            for lecture in course["lectures"]:
                for session in catalog.list_sessions(course["id"], lecture["id"]):
                    if session["status"] == "live":
                        catalog.set_session_runtime_state(
                            course["id"],
                            lecture["id"],
                            session["id"],
                            "interrupted",
                        )

    def workspace_error(exc: WorkspaceCatalogError) -> HTTPException:
        missing = "does not exist" in str(exc) or "not available" in str(exc)
        return HTTPException(status_code=404 if missing else 400, detail=str(exc))

    def agent_lab_error(exc: AgentLabError) -> HTTPException:
        return HTTPException(
            status_code=409 if isinstance(exc, AgentLabConflictError) else 400,
            detail=str(exc),
        )

    def instruction_error(exc: InstructionError) -> HTTPException:
        missing = "does not exist" in str(exc)
        return HTTPException(
            status_code=(
                409
                if isinstance(exc, InstructionConflictError)
                else 404
                if missing
                else 400
            ),
            detail=str(exc),
        )

    @app.get("/health")
    async def health() -> JSONResponse:
        return JSONResponse({"ok": True})

    @app.get("/")
    async def workspace_home() -> RedirectResponse:
        return RedirectResponse("/courses", status_code=307)

    @app.get("/api/courses")
    async def list_courses() -> JSONResponse:
        try:
            return JSONResponse({"courses": catalog.list_courses()})
        except WorkspaceCatalogError as exc:
            raise workspace_error(exc) from exc

    @app.get("/api/agent-lab/agents")
    async def list_lab_agents() -> JSONResponse:
        return JSONResponse({"agents": prompt_store.list_agents()})

    @app.get("/api/agent-lab/agents/{agent_id}")
    async def get_lab_agent(agent_id: str) -> JSONResponse:
        try:
            return JSONResponse(prompt_store.get_agent(agent_id))
        except AgentLabError as exc:
            raise agent_lab_error(exc) from exc

    @app.put("/api/agent-lab/agents/{agent_id}/model-config")
    async def update_lab_agent_model(agent_id: str, request: Request) -> JSONResponse:
        try:
            body = await request.json()
            return JSONResponse(
                prompt_store.update_model_config(
                    agent_id, model=str(body.get("model", ""))
                )
            )
        except (AgentLabError, AttributeError) as exc:
            error = (
                exc
                if isinstance(exc, AgentLabError)
                else AgentLabError("Request body must be an object")
            )
            raise agent_lab_error(error) from exc

    @app.get("/api/agent-lab/agents/{agent_id}/versions")
    async def list_lab_prompt_versions(agent_id: str) -> JSONResponse:
        try:
            return JSONResponse({"versions": prompt_store.list_versions(agent_id)})
        except AgentLabError as exc:
            raise agent_lab_error(exc) from exc

    @app.post("/api/agent-lab/agents/{agent_id}/drafts", status_code=201)
    async def create_lab_draft(agent_id: str, request: Request) -> JSONResponse:
        try:
            body = await request.json()
            return JSONResponse(
                prompt_store.create_draft(
                    agent_id,
                    source_version_id=body.get("sourceVersionId"),
                    note=str(body.get("note", "")),
                ),
                status_code=201,
            )
        except (AgentLabError, AttributeError) as exc:
            error = (
                exc
                if isinstance(exc, AgentLabError)
                else AgentLabError("Request body must be an object")
            )
            raise agent_lab_error(error) from exc

    @app.put("/api/agent-lab/agents/{agent_id}/versions/{version_id}")
    async def save_lab_draft(
        agent_id: str, version_id: str, request: Request
    ) -> JSONResponse:
        try:
            body = await request.json()
            return JSONResponse(
                prompt_store.save_draft(
                    agent_id,
                    version_id,
                    content=str(body.get("content", "")),
                    expected_updated_at=str(body.get("expectedUpdatedAt", "")),
                    note=body.get("note"),
                )
            )
        except (AgentLabError, AttributeError) as exc:
            error = (
                exc
                if isinstance(exc, AgentLabError)
                else AgentLabError("Request body must be an object")
            )
            raise agent_lab_error(error) from exc

    @app.post("/api/agent-lab/agents/{agent_id}/versions/{version_id}/activate")
    async def activate_lab_version(agent_id: str, version_id: str) -> JSONResponse:
        try:
            return JSONResponse(prompt_store.activate(agent_id, version_id))
        except AgentLabError as exc:
            raise agent_lab_error(exc) from exc

    @app.post(
        "/api/agent-lab/agents/{agent_id}/versions/{version_id}/restore",
        status_code=201,
    )
    async def restore_lab_version(agent_id: str, version_id: str) -> JSONResponse:
        try:
            return JSONResponse(
                prompt_store.restore_as_draft(agent_id, version_id), status_code=201
            )
        except AgentLabError as exc:
            raise agent_lab_error(exc) from exc

    @app.get("/api/agent-lab/agents/{agent_id}/compare")
    async def compare_lab_versions(
        agent_id: str, base: str, target: str
    ) -> JSONResponse:
        try:
            return JSONResponse(prompt_store.compare(agent_id, base, target))
        except AgentLabError as exc:
            raise agent_lab_error(exc) from exc

    @app.post("/api/agent-lab/context/resolve")
    async def resolve_lab_context(request: Request) -> JSONResponse:
        try:
            return JSONResponse(lab.resolve_context(await request.json()))
        except (AgentLabError, WorkspaceCatalogError, AttributeError) as exc:
            if isinstance(exc, WorkspaceCatalogError):
                raise workspace_error(exc) from exc
            error = (
                exc
                if isinstance(exc, AgentLabError)
                else AgentLabError("Request body must be an object")
            )
            raise agent_lab_error(error) from exc

    @app.post("/api/agent-lab/tests", status_code=201)
    async def run_lab_test(request: Request) -> JSONResponse:
        try:
            return JSONResponse(
                await lab.run_test(await request.json()), status_code=201
            )
        except (AgentLabError, WorkspaceCatalogError, AttributeError) as exc:
            if isinstance(exc, WorkspaceCatalogError):
                raise workspace_error(exc) from exc
            error = (
                exc
                if isinstance(exc, AgentLabError)
                else AgentLabError("Request body must be an object")
            )
            raise agent_lab_error(error) from exc

    @app.get("/api/agent-lab/tests")
    async def list_lab_tests(agent_id: str | None = None) -> JSONResponse:
        return JSONResponse({"tests": prompt_store.list_test_runs(agent_id)})

    @app.get("/api/agent-lab/tests/{run_id}")
    async def get_lab_test(run_id: str) -> JSONResponse:
        try:
            return JSONResponse(prompt_store.get_test_run(run_id))
        except AgentLabError as exc:
            raise agent_lab_error(exc) from exc

    @app.get("/api/instructions/documents")
    async def list_instruction_documents() -> JSONResponse:
        try:
            return JSONResponse({"documents": scoped_store.list_documents()})
        except (InstructionError, WorkspaceCatalogError) as exc:
            if isinstance(exc, WorkspaceCatalogError):
                raise workspace_error(exc) from exc
            raise instruction_error(exc) from exc

    @app.get("/api/instructions/documents/{document_id}")
    async def get_instruction_document(document_id: str) -> JSONResponse:
        try:
            return JSONResponse(scoped_store.get_document(document_id))
        except InstructionError as exc:
            raise instruction_error(exc) from exc

    @app.get("/api/instructions/documents/{document_id}/versions")
    async def list_instruction_versions(document_id: str) -> JSONResponse:
        try:
            return JSONResponse({"versions": scoped_store.list_versions(document_id)})
        except InstructionError as exc:
            raise instruction_error(exc) from exc

    @app.post("/api/instructions/documents/{document_id}/drafts", status_code=201)
    async def create_instruction_draft(
        document_id: str, request: Request
    ) -> JSONResponse:
        try:
            body = await request.json()
            return JSONResponse(
                scoped_store.create_draft(
                    document_id,
                    source_version_id=body.get("sourceVersionId"),
                    note=str(body.get("note", "")),
                ),
                status_code=201,
            )
        except (InstructionError, AttributeError) as exc:
            error = (
                exc
                if isinstance(exc, InstructionError)
                else InstructionError("Request body must be an object")
            )
            raise instruction_error(error) from exc

    @app.put("/api/instructions/documents/{document_id}/versions/{version_id}")
    async def save_instruction_draft(
        document_id: str, version_id: str, request: Request
    ) -> JSONResponse:
        try:
            body = await request.json()
            return JSONResponse(
                scoped_store.save_draft(
                    document_id,
                    version_id,
                    content=str(body.get("content", "")),
                    expected_updated_at=str(body.get("expectedUpdatedAt", "")),
                    note=body.get("note"),
                )
            )
        except (InstructionError, AttributeError) as exc:
            error = (
                exc
                if isinstance(exc, InstructionError)
                else InstructionError("Request body must be an object")
            )
            raise instruction_error(error) from exc

    @app.post(
        "/api/instructions/documents/{document_id}/versions/{version_id}/activate"
    )
    async def activate_instruction_version(
        document_id: str, version_id: str
    ) -> JSONResponse:
        try:
            return JSONResponse(scoped_store.activate(document_id, version_id))
        except InstructionError as exc:
            raise instruction_error(exc) from exc

    @app.post(
        "/api/instructions/documents/{document_id}/versions/{version_id}/restore",
        status_code=201,
    )
    async def restore_instruction_version(
        document_id: str, version_id: str
    ) -> JSONResponse:
        try:
            return JSONResponse(
                scoped_store.restore_as_draft(document_id, version_id),
                status_code=201,
            )
        except InstructionError as exc:
            raise instruction_error(exc) from exc

    @app.get("/api/instructions/documents/{document_id}/compare")
    async def compare_instruction_versions(
        document_id: str, base: str, target: str
    ) -> JSONResponse:
        try:
            return JSONResponse(scoped_store.compare(document_id, base, target))
        except InstructionError as exc:
            raise instruction_error(exc) from exc

    def resolve_instruction_request(
        body: object, *, allow_version_overrides: bool = False
    ) -> dict[str, object]:
        if not isinstance(body, dict):
            raise InstructionError("Request body must be an object")
        agent_id = str(body.get("agentId", ""))
        course_id = str(body.get("courseId", ""))
        lecture_id = str(body.get("lectureId", ""))
        catalog.get_lecture(course_id, lecture_id)
        agent = prompt_store.get_agent(agent_id, include_content=False)
        raw_overrides = body.get("versionOverrides", {})
        if not isinstance(raw_overrides, dict):
            raise InstructionError("versionOverrides must be an object")
        version_overrides = (
            {str(key): str(value) for key, value in raw_overrides.items()}
            if allow_version_overrides
            else None
        )
        snapshot = resolver.resolve(
            agent_definition=agent,
            course_id=course_id,
            lecture_id=lecture_id,
            version_overrides=version_overrides,
        )
        return {
            **snapshot,
            "blocks": [
                block.to_dict() for block in instruction_context_blocks(snapshot)
            ],
        }

    @app.post("/api/instructions/resolve")
    async def resolve_instructions(request: Request) -> JSONResponse:
        try:
            return JSONResponse(resolve_instruction_request(await request.json()))
        except AgentLabError as exc:
            raise agent_lab_error(exc) from exc
        except WorkspaceCatalogError as exc:
            raise workspace_error(exc) from exc
        except (InstructionError, AttributeError) as exc:
            error = (
                exc
                if isinstance(exc, InstructionError)
                else InstructionError("Request body must be an object")
            )
            raise instruction_error(error) from exc

    @app.post("/api/instructions/preview-context")
    async def preview_instruction_context(request: Request) -> JSONResponse:
        try:
            return JSONResponse(
                resolve_instruction_request(
                    await request.json(), allow_version_overrides=True
                )
            )
        except AgentLabError as exc:
            raise agent_lab_error(exc) from exc
        except WorkspaceCatalogError as exc:
            raise workspace_error(exc) from exc
        except (InstructionError, AttributeError) as exc:
            error = (
                exc
                if isinstance(exc, InstructionError)
                else InstructionError("Request body must be an object")
            )
            raise instruction_error(error) from exc

    @app.post("/api/courses", status_code=201)
    async def create_course(request: Request) -> JSONResponse:
        try:
            body = await request.json()
            course = catalog.create_course(
                title=str(body.get("title", "")),
                code=body.get("code"),
                description=body.get("description"),
            )
            return JSONResponse(course, status_code=201)
        except (WorkspaceCatalogError, AttributeError) as exc:
            error = (
                exc
                if isinstance(exc, WorkspaceCatalogError)
                else WorkspaceCatalogError("Request body must be an object")
            )
            raise workspace_error(error) from exc

    @app.get("/api/courses/{course_id}")
    async def get_course(course_id: str) -> JSONResponse:
        try:
            return JSONResponse(catalog.get_course(course_id))
        except WorkspaceCatalogError as exc:
            raise workspace_error(exc) from exc

    @app.patch("/api/courses/{course_id}")
    async def update_course(course_id: str, request: Request) -> JSONResponse:
        try:
            body = await request.json()
            return JSONResponse(
                catalog.update_course(
                    course_id,
                    title=str(body.get("title", "")),
                    code=body.get("code"),
                    description=body.get("description"),
                )
            )
        except (WorkspaceCatalogError, AttributeError) as exc:
            error = (
                exc
                if isinstance(exc, WorkspaceCatalogError)
                else WorkspaceCatalogError("Request body must be an object")
            )
            raise workspace_error(error) from exc

    @app.delete("/api/courses/{course_id}")
    async def delete_course(course_id: str) -> JSONResponse:
        try:
            course = catalog.get_course(course_id)
            session_keys = [
                (lecture["id"], session["id"])
                for lecture in course["lectures"]
                for session in catalog.list_sessions(course_id, lecture["id"])
            ]
            catalog.delete_course(course_id)
            for lecture_id, session_id in session_keys:
                await runtimes.remove(course_id, lecture_id, session_id)
            return JSONResponse({"deleted": True, "id": course_id})
        except WorkspaceCatalogError as exc:
            raise workspace_error(exc) from exc

    @app.post("/api/courses/{course_id}/lectures", status_code=201)
    async def create_lecture(course_id: str, request: Request) -> JSONResponse:
        try:
            body = await request.json()
            lecture = catalog.create_lecture(
                course_id,
                number=str(body.get("number", "")),
                title=str(body.get("title", "")),
            )
            return JSONResponse(lecture, status_code=201)
        except (WorkspaceCatalogError, AttributeError) as exc:
            error = (
                exc
                if isinstance(exc, WorkspaceCatalogError)
                else WorkspaceCatalogError("Request body must be an object")
            )
            raise workspace_error(error) from exc

    @app.get("/api/courses/{course_id}/lectures/{lecture_id}")
    async def get_lecture(course_id: str, lecture_id: str) -> JSONResponse:
        try:
            return JSONResponse(catalog.get_lecture(course_id, lecture_id))
        except WorkspaceCatalogError as exc:
            raise workspace_error(exc) from exc

    @app.patch("/api/courses/{course_id}/lectures/{lecture_id}")
    async def update_lecture(
        course_id: str, lecture_id: str, request: Request
    ) -> JSONResponse:
        try:
            body = await request.json()
            return JSONResponse(
                catalog.update_lecture(
                    course_id,
                    lecture_id,
                    number=str(body.get("number", "")),
                    title=str(body.get("title", "")),
                )
            )
        except (WorkspaceCatalogError, AttributeError) as exc:
            error = (
                exc
                if isinstance(exc, WorkspaceCatalogError)
                else WorkspaceCatalogError("Request body must be an object")
            )
            raise workspace_error(error) from exc

    @app.delete("/api/courses/{course_id}/lectures/{lecture_id}")
    async def delete_lecture(course_id: str, lecture_id: str) -> JSONResponse:
        try:
            session_ids = [
                session["id"]
                for session in catalog.list_sessions(course_id, lecture_id)
            ]
            catalog.delete_lecture(course_id, lecture_id)
            for session_id in session_ids:
                await runtimes.remove(course_id, lecture_id, session_id)
            return JSONResponse({"deleted": True, "id": lecture_id})
        except WorkspaceCatalogError as exc:
            raise workspace_error(exc) from exc

    @app.post("/api/courses/{course_id}/lectures/{lecture_id}/material")
    async def upload_material(
        course_id: str, lecture_id: str, request: Request, filename: str
    ) -> JSONResponse:
        try:
            content = await request.body()
            return JSONResponse(
                await asyncio.to_thread(
                    catalog.upload_material,
                    course_id,
                    lecture_id,
                    filename=filename,
                    content=content,
                )
            )
        except WorkspaceCatalogError as exc:
            raise workspace_error(exc) from exc

    @app.post("/api/courses/{course_id}/lectures/{lecture_id}/prepare", status_code=202)
    async def prepare_lecture(course_id: str, lecture_id: str, backend: str = "qwen") -> JSONResponse:
        try:
            return JSONResponse(
                preparations.start(course_id, lecture_id, backend=backend), status_code=202
            )
        except WorkspaceCatalogError as exc:
            raise workspace_error(exc) from exc

    @app.get("/api/courses/{course_id}/lectures/{lecture_id}/sessions")
    async def list_lecture_sessions(course_id: str, lecture_id: str) -> JSONResponse:
        try:
            return JSONResponse(
                {"sessions": catalog.list_sessions(course_id, lecture_id)}
            )
        except WorkspaceCatalogError as exc:
            raise workspace_error(exc) from exc

    @app.get("/api/courses/{course_id}/lectures/{lecture_id}/sessions/current")
    async def current_lecture_session(course_id: str, lecture_id: str) -> JSONResponse:
        try:
            return JSONResponse(
                {"session": catalog.get_current_session(course_id, lecture_id)}
            )
        except WorkspaceCatalogError as exc:
            raise workspace_error(exc) from exc

    @app.get("/api/courses/{course_id}/lectures/{lecture_id}/sessions/{session_id}")
    async def get_lecture_session(
        course_id: str, lecture_id: str, session_id: str
    ) -> JSONResponse:
        try:
            return JSONResponse(catalog.get_session(course_id, lecture_id, session_id))
        except WorkspaceCatalogError as exc:
            raise workspace_error(exc) from exc

    @app.post(
        "/api/courses/{course_id}/lectures/{lecture_id}/sessions",
        status_code=201,
    )
    async def start_lecture_session(course_id: str, lecture_id: str) -> JSONResponse:
        try:
            preparation_snapshot = catalog.committed_preparation_snapshot(
                course_id, lecture_id
            )
            return JSONResponse(
                catalog.start_session(
                    course_id,
                    lecture_id,
                    preparation_snapshot=preparation_snapshot,
                ),
                status_code=201,
            )
        except WorkspaceCatalogError as exc:
            raise workspace_error(exc) from exc

    @app.post(
        "/api/courses/{course_id}/lectures/{lecture_id}/sessions/{session_id}/pause"
    )
    async def pause_lecture_session(
        course_id: str, lecture_id: str, session_id: str
    ) -> JSONResponse:
        try:
            runtime = await runtime_for(course_id, lecture_id, session_id)
            return JSONResponse(await runtime.pause())
        except (WorkspaceCatalogError, ValueError) as exc:
            raise workspace_error(exc) from exc

    @app.post(
        "/api/courses/{course_id}/lectures/{lecture_id}/sessions/{session_id}/resume"
    )
    async def resume_lecture_session(
        course_id: str, lecture_id: str, session_id: str
    ) -> JSONResponse:
        try:
            runtime = await runtime_for(course_id, lecture_id, session_id)
            return JSONResponse(await runtime.resume())
        except (WorkspaceCatalogError, ValueError) as exc:
            raise workspace_error(exc) from exc

    @app.post(
        "/api/courses/{course_id}/lectures/{lecture_id}/sessions/{session_id}/complete"
    )
    async def complete_lecture_session(
        course_id: str, lecture_id: str, session_id: str
    ) -> JSONResponse:
        try:
            runtime = await runtime_for(course_id, lecture_id, session_id)
            was_completed = catalog.get_session(course_id, lecture_id, session_id)["status"] == "completed"
            session = await runtime.stop()
            if not was_completed and os.getenv("AGENT1_POSTCLASS_AUTO", "false").lower() in {"1", "true", "yes"}:
                try:
                    launch_agent1(course_id, lecture_id, session_id)
                except Exception:
                    logger.exception("Could not schedule post-class Agent1 for %s", session_id)
            return JSONResponse(session)
        except (WorkspaceCatalogError, ValueError) as exc:
            raise workspace_error(exc) from exc

    @app.post("/api/courses/{course_id}/lectures/{lecture_id}/sessions/{session_id}/agent1/run", status_code=202)
    async def run_agent1(course_id: str, lecture_id: str, session_id: str, backend: str = "qwen") -> JSONResponse:
        try:
            return JSONResponse(launch_agent1(course_id, lecture_id, session_id, backend), status_code=202)
        except (WorkspaceCatalogError, ValueError, CodexUnavailableError) as exc:
            raise workspace_error(exc) from exc

    @app.get("/api/courses/{course_id}/lectures/{lecture_id}/sessions/{session_id}/agent1")
    async def agent1_status(course_id: str, lecture_id: str, session_id: str) -> JSONResponse:
        try:
            session = catalog.get_session(course_id, lecture_id, session_id)
            path = catalog.project_root / str(session["transcriptPath"])
            manifest = path.parent / "agent1" / "manifest.json"
            task = agent1_tasks.get(session_id)
            if task and not task.done():
                current = json.loads(manifest.read_text(encoding="utf-8")) if manifest.is_file() else {"block_count": 0}
                spec = agent1_specs[session_id]
                return JSONResponse({**current, "status": "processing", "segmenter_provider": spec.provider,
                                     "segmenter_model": spec.model})
            if task and task.done() and not task.cancelled():
                try:
                    outcome = task.result()
                except Exception as exc:
                    return JSONResponse({"status": "failed", "root_cause": {"error_type": type(exc).__name__, "error_message": str(exc)}})
                if outcome.get("status") == "failed":
                    return JSONResponse(outcome)
            if manifest.is_file():
                return JSONResponse(json.loads(manifest.read_text(encoding="utf-8")))
            return JSONResponse({"status": "processing" if task and not task.done() else "not_started", "block_count": 0})
        except WorkspaceCatalogError as exc:
            raise workspace_error(exc) from exc

    @app.post("/api/courses/{course_id}/lectures/{lecture_id}/sessions/{session_id}/agent2/run", status_code=202)
    async def run_agent2(course_id: str, lecture_id: str, session_id: str, backend: str = "qwen") -> JSONResponse:
        try:
            return JSONResponse(launch_agent2(course_id, lecture_id, session_id, backend), status_code=202)
        except (WorkspaceCatalogError, ValueError, OSError, CodexUnavailableError) as exc:
            raise workspace_error(exc) from exc

    @app.get("/api/courses/{course_id}/lectures/{lecture_id}/sessions/{session_id}/agent2")
    async def agent2_status(course_id: str, lecture_id: str, session_id: str) -> JSONResponse:
        try:
            return JSONResponse(agent2_snapshot(course_id, lecture_id, session_id))
        except (WorkspaceCatalogError, ValueError, OSError) as exc:
            raise workspace_error(exc) from exc

    @app.get("/api/courses/{course_id}/lectures/{lecture_id}/sessions/{session_id}/agent2/results")
    async def agent2_results(course_id: str, lecture_id: str, session_id: str) -> JSONResponse:
        try:
            status = agent2_snapshot(course_id, lecture_id, session_id)
            fingerprint = status.get("input_fingerprint", "")
            if not isinstance(fingerprint, str) or not re.fullmatch(r"[0-9a-f]{64}", fingerprint):
                return JSONResponse({"manifest": status, "blocks": []})
            path = agent2_output(course_id, lecture_id, session_id) / "runs" / fingerprint / "corrected_blocks.jsonl"
            return JSONResponse({"manifest": status, "blocks": read_jsonl(path) if path.is_file() else []})
        except (WorkspaceCatalogError, ValueError, OSError) as exc:
            raise workspace_error(exc) from exc

    @app.get("/api/courses/{course_id}/lectures/{lecture_id}/formulas")
    async def lecture_formulas(course_id: str, lecture_id: str) -> JSONResponse:
        from lecture_formulas import collect_lecture_formulas
        try:
            lecture = catalog.get_lecture(course_id, lecture_id)
            if lecture["preparationStatus"] != "ready":
                raise WorkspaceCatalogError("Prepare this lecture before opening the formula collection")
            scaffold = json.loads(catalog.artifact_path(course_id, lecture_id, "scaffold").read_text(encoding="utf-8"))
            result = await asyncio.to_thread(collect_lecture_formulas, catalog.lecture_root(course_id, lecture_id), scaffold)
            return JSONResponse(result)
        except (WorkspaceCatalogError, OSError, ValueError, TypeError) as exc:
            raise workspace_error(exc) from exc

    @app.get("/api/courses/{course_id}/lectures/{lecture_id}/files/{artifact}")
    async def lecture_file(course_id: str, lecture_id: str, artifact: str) -> Response:
        try:
            path = catalog.artifact_path(course_id, lecture_id, artifact)
        except WorkspaceCatalogError as exc:
            raise workspace_error(exc) from exc
        media_type = {
            "material": "application/pdf",
            "scaffold": "application/json",
            "markdown": "text/markdown; charset=utf-8",
            "hotwords": "application/json",
            "asr_context": "text/plain; charset=utf-8",
        }.get(artifact)
        return FileResponse(path, media_type=media_type)

    @app.websocket("/ws")
    async def classroom_websocket(websocket: WebSocket) -> None:
        await websocket.accept()
        runtime: SessionRuntimeOwner | None = None
        lease = None
        session_id = websocket.query_params.get("session_id")
        if (
            websocket.query_params.get("course_id") is None
            or websocket.query_params.get("lecture_id") is None
            or session_id is None
        ):
            await websocket.close(
                code=1008,
                reason="Production realtime requires course_id, lecture_id, and session_id",
            )
            return
        try:
            session_settings = _session_settings(websocket, settings)
        except ValueError as exc:
            await websocket.close(code=1008, reason=str(exc))
            return
        try:
            if not SESSION_CONTEXT_PATTERN.fullmatch(session_id):
                await websocket.close(code=1008, reason="invalid session_id")
                return
            session = catalog.get_session(
                session_settings.course_id,
                session_settings.lecture_id,
                session_id,
            )
            if session["status"] != "live":
                await websocket.close(code=1008, reason="session is not live")
                return
            if not isinstance(session.get("preparationSnapshot"), dict):
                await websocket.close(
                    code=1008, reason="session snapshot is incomplete"
                )
                return
            runtime = await runtime_for(
                session_settings.course_id,
                session_settings.lecture_id,
                session_id,
            )
            try:
                lease = await runtime.attach()
            except DuplicateRuntimeConnectionError as exc:
                await websocket.close(code=1008, reason=str(exc))
                return
            await run_realtime_classroom(
                websocket,
                session_settings,
                engine=realtime_engine,
                session_id=session_id,
                runtime_owner=runtime,
                connection_generation=lease.generation,
                hotword_terms=(
                    dict(session["hotwordVocabularySnapshot"])
                    if isinstance(session.get("hotwordVocabularySnapshot"), dict)
                    else tuple(session.get("hotwordsSnapshot", ()))
                ),
            )
        except Exception:
            logger.exception("Classroom pipeline failed")
        finally:
            if (
                runtime is not None
                and lease is not None
                and not lease.finished.is_set()
            ):
                await runtime.finish_connection(lease.generation, unexpected=True)

    @app.get("/{request_path:path}")
    async def static_file(request_path: str) -> Response:
        path = _frontend_file(request_path)
        if path is None:
            return Response("Not found", status_code=404, media_type="text/plain")
        content_type = mimetypes.guess_type(path.name)[0]
        return FileResponse(path, media_type=content_type)

    return app


async def run_server(*, host: str, port: int, settings: Settings | None = None) -> None:
    settings = settings or load_settings()
    config = uvicorn.Config(
        create_app(settings),
        host=host,
        port=port,
        log_level="debug" if settings.debug else "info",
    )
    server = uvicorn.Server(config)
    await server.serve()


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Local realtime Classroom Assistant")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", default=8765, type=int)
    parser.add_argument("--course-id")
    parser.add_argument("--lecture-id")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    settings = load_settings()
    _configure_logging(debug=settings.debug)
    settings = replace(
        settings,
        course_id=args.course_id or settings.course_id,
        lecture_id=args.lecture_id or settings.lecture_id,
    )
    try:
        asyncio.run(run_server(host=args.host, port=args.port, settings=settings))
    except KeyboardInterrupt:
        return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
