"""Manual-only endpoints, registered beside the existing Agent1/ASR tasks."""
from __future__ import annotations

import asyncio
import json
import os

from fastapi import HTTPException
from fastapi.responses import JSONResponse

from agent1_v1 import ModelSpec
from agent2_asr import provider_for_agent2  # Reuse transport configuration, never the correction pipeline.
from agent2_enrichment import EnrichmentRun, RunLease, is_running, read_results
from agent2_enrichment_adapter import ASRAdapter
from agent2_retrieval import Agent2Error
from postclass_backends import selected_spec
from codex_scaffold import CodexUnavailableError


def register_enrichment_api(app, catalog, asr_status):
    tasks = {}
    prefix = "/api/courses/{course_id}/lectures/{lecture_id}/sessions/{session_id}/agent2/enrichment"

    def paths(key):
        session = catalog.get_session(*key)
        final = catalog.project_root / str(session["transcriptPath"])
        return session, final, final.parent / "agent2_enrichment"

    def adapter(key, final):
        return ASRAdapter(final_path=final, lecture_root=catalog.lecture_root(*key[:2]),
                          course_id=key[0], lecture_id=key[1], session_id=key[2])

    def snapshot(key):
        session, final, output = paths(key)
        upstream = asr_status(*key)
        manifest = output / "manifest.json"
        value = json.loads(manifest.read_text(encoding="utf-8")) if manifest.exists() else {"status": "not_started"}
        ready, compatible, prerequisite_error = False, False, None
        if session["status"] == "completed" and upstream.get("status") == "completed" and upstream.get("agent1_ready", True):
            try:
                current = adapter(key, final)
                ready = True
                compatible = all(value.get(k) == v for k, v in current.version.items())
            except (ValueError, OSError, KeyError, TypeError) as exc:
                prerequisite_error = str(exc)
        running = is_running(output)
        if running:
            value = {**value, "status": "processing"}
        elif value.get("status") == "processing":
            task = tasks.get(key)
            failure = task.exception() if task and task.done() and not task.cancelled() else None
            value = {**value, "status": "failed", "root_cause": {
                "error_type": type(failure).__name__ if failure else "INTERRUPTED",
                "error_message": str(failure) if failure else "Enrichment was interrupted. Retry to reuse completed stages."}}
        return {**value, "asr_status": upstream.get("status", "not_started"),
                "asr_failed_block_ids": upstream.get("failed_block_ids", []), "asr_ready": ready,
                "asr_root_cause": upstream.get("root_cause"),
                "compatible": compatible, "stale": bool(manifest.exists() and not compatible),
                "prerequisite_error": prerequisite_error}

    def translate_error(exc):
        code = 409 if isinstance(exc, Agent2Error) else 400
        return HTTPException(status_code=code, detail=str(exc))

    @app.get(prefix)
    async def status(course_id: str, lecture_id: str, session_id: str):
        try:
            return snapshot((course_id, lecture_id, session_id))
        except (ValueError, OSError, KeyError, TypeError) as exc:
            raise translate_error(exc) from exc

    @app.get(prefix + "/results")
    async def results(course_id: str, lecture_id: str, session_id: str):
        try:
            key = (course_id, lecture_id, session_id)
            value = snapshot(key)
            empty = {name: [] for name in ("fragments", "relations", "shared_content", "pending", "requirements", "node_enrichment")}
            return {"manifest": value, **(read_results(paths(key)[2], value) if value["compatible"] else empty)}
        except (ValueError, OSError, KeyError, TypeError) as exc:
            raise translate_error(exc) from exc

    @app.post(prefix + "/run", status_code=202)
    async def launch(course_id: str, lecture_id: str, session_id: str, backend: str = "qwen"):
        key = (course_id, lecture_id, session_id)
        lease = None
        try:
            if backend not in {"qwen", "codex"}:
                raise ValueError("Execution backend must be qwen or codex")
            value = snapshot(key)
            if is_running(paths(key)[2]):
                expected = "codex" if backend == "codex" else "alibaba_cloud_model_studio"
                if value.get("provider") != expected:
                    raise Agent2Error("BACKEND_CHANGE_WHILE_RUNNING", "Enrichment is running. Wait before changing its execution backend.")
                return JSONResponse(value, status_code=202)
            if not value["asr_ready"]:
                raise Agent2Error("UPSTREAM_NOT_READY", value["prerequisite_error"] or
                                  f"ASR correction is {value['asr_status']}; complete upstream first")
            _, final, output = paths(key)
            upstream = adapter(key, final)
            spec = selected_spec("enrichment", backend, ModelSpec(os.getenv("AGENT2_ENRICHMENT_PROVIDER", os.getenv("AGENT2_PROVIDER", "alibaba_cloud_model_studio")),
                os.getenv("AGENT2_ENRICHMENT_MODEL", os.getenv("AGENT2_MODEL", "qwen3.8-max")),
                int(os.getenv("AGENT2_ENRICHMENT_CONTEXT_WINDOW_TOKENS") or os.getenv("AGENT2_CONTEXT_WINDOW_TOKENS") or os.getenv("AGENT1_SEGMENTER_CONTEXT_WINDOW_TOKENS") or "0"),
                int(os.getenv("AGENT2_ENRICHMENT_MAX_OUTPUT_TOKENS", "8192"))))
            # Validate and compare the complete run binding BEFORE constructing a real provider.
            run = EnrichmentRun(upstream, output, None, spec)
            if value.get("status") == "completed" and value.get("input_fingerprint") == run.fingerprint:
                return JSONResponse(value, status_code=202)
            lease = RunLease(output)
            run.provider = provider_for_agent2(spec)
            run.save()
            owned_lease = lease
            task = asyncio.create_task(run.run(owned_lease), name=f"agent2-enrichment-{session_id}")
            # Also release if cancellation occurs before the coroutine's first step.
            def finish(finished):
                owned_lease.close()
                if not finished.cancelled():
                    finished.exception()  # Observe unexpected persistence errors as well.
            task.add_done_callback(finish)
            tasks[key] = task
            lease = None  # Task owns the lease, including cancellation/error cleanup.
            return JSONResponse(snapshot(key), status_code=202)
        except Agent2Error as exc:
            if exc.error_type == "ALREADY_RUNNING":
                return JSONResponse(snapshot(key), status_code=202)
            raise translate_error(exc) from exc
        except (ValueError, OSError, KeyError, TypeError, CodexUnavailableError) as exc:
            raise translate_error(exc) from exc
        finally:
            if lease:
                lease.close()

    @app.on_event("shutdown")
    async def stop_tasks():
        running = [t for t in tasks.values() if not t.done()]
        for task in running:
            task.cancel()
        if running:
            await asyncio.gather(*running, return_exceptions=True)
