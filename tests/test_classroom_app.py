from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from fastapi.testclient import TestClient

from classroom_app import _frontend_file, _session_settings, create_app
from config import Settings
from workspace_catalog import WorkspaceCatalog


def settings(output_path: Path) -> Settings:
    return Settings(
        api_key="test-key",
        workspace_id=None,
        send_workspace_header=False,
        source_language="en",
        target_language="zh",
        debug=False,
        raw_event_debug=False,
        agent1_enabled=False,
        agent1_output_path=output_path,
    )


class ClassroomAppTests(unittest.TestCase):
    def test_preparation_api_forwards_selected_backend(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "courses").mkdir()
            (root / "courses/index.json").write_text('{"schema_version":1,"course_ids":[]}')
            manager = mock.Mock()
            manager.start.return_value = {"preparationStatus": "preparing"}
            client = TestClient(create_app(settings(root / "data"), workspace_catalog=WorkspaceCatalog(root), preparation_manager=manager))
            response = client.post("/api/courses/course/lectures/lecture/prepare?backend=codex")
            self.assertEqual(response.status_code, 202)
            manager.start.assert_called_once_with("course", "lecture", backend="codex")
            client.post("/api/courses/course/lectures/lecture/prepare")
            manager.start.assert_called_with("course", "lecture", backend="qwen")

    def test_workspace_api_creates_course_lecture_and_uploads_material(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "courses").mkdir()
            (root / "courses" / "index.json").write_text(
                json.dumps({"schema_version": 1, "course_ids": []}),
                encoding="utf-8",
            )
            client = TestClient(
                create_app(
                    settings(root / "data"),
                    workspace_catalog=WorkspaceCatalog(root),
                )
            )

            course = client.post(
                "/api/courses", json={"title": "Distributed Systems"}
            )
            lecture = client.post(
                f"/api/courses/{course.json()['id']}/lectures",
                json={"number": "01", "title": "Introduction"},
            )
            uploaded = client.post(
                f"/api/courses/{course.json()['id']}/lectures/{lecture.json()['id']}/material?filename=intro.pdf",
                content=b"%PDF-test",
                headers={"Content-Type": "application/pdf"},
            )
            material = client.get(
                f"/api/courses/{course.json()['id']}/lectures/{lecture.json()['id']}/files/material"
            )

        self.assertEqual(course.status_code, 201)
        self.assertEqual(lecture.status_code, 201)
        self.assertEqual(lecture.json()["preparationStatus"], "created")
        self.assertEqual(uploaded.status_code, 200)
        self.assertEqual(uploaded.json()["preparationStatus"], "material_uploaded")
        self.assertEqual(material.content, b"%PDF-test")

    def test_workspace_api_updates_and_deletes_courses_and_lectures(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "courses").mkdir()
            (root / "courses" / "index.json").write_text(
                json.dumps({"schema_version": 1, "course_ids": []}),
                encoding="utf-8",
            )
            client = TestClient(
                create_app(
                    settings(root / "data"),
                    workspace_catalog=WorkspaceCatalog(root),
                )
            )
            course = client.post("/api/courses", json={"title": "Wrong"}).json()
            lecture = client.post(
                f"/api/courses/{course['id']}/lectures",
                json={"number": "99", "title": "Wrong"},
            ).json()

            updated_course = client.patch(
                f"/api/courses/{course['id']}",
                json={
                    "title": "Correct Course",
                    "code": "COR101",
                    "description": "Correct description",
                },
            )
            updated_lecture = client.patch(
                f"/api/courses/{course['id']}/lectures/{lecture['id']}",
                json={"number": "01", "title": "Introduction"},
            )
            deleted_lecture = client.delete(
                f"/api/courses/{course['id']}/lectures/{lecture['id']}"
            )
            deleted_course = client.delete(f"/api/courses/{course['id']}")

        self.assertEqual(updated_course.status_code, 200)
        self.assertEqual(updated_course.json()["id"], course["id"])
        self.assertEqual(updated_course.json()["title"], "Correct Course")
        self.assertEqual(updated_lecture.status_code, 200)
        self.assertEqual(updated_lecture.json()["id"], lecture["id"])
        self.assertEqual(updated_lecture.json()["number"], "01")
        self.assertEqual(deleted_lecture.json(), {"deleted": True, "id": lecture["id"]})
        self.assertEqual(deleted_course.json(), {"deleted": True, "id": course["id"]})

    def test_selected_course_context_is_bound_to_realtime_session(self) -> None:
        current = settings(Path("data"))
        websocket = mock.Mock(
            query_params={
                "course_id": "open-system",
                "lecture_id": "0_intro_networks_1",
            }
        )

        selected = _session_settings(websocket, current)

        self.assertEqual(selected.course_id, "open-system")
        self.assertEqual(selected.lecture_id, "0_intro_networks_1")
        self.assertEqual(current.course_id, "default-course")

    def test_session_api_preserves_ready_preparation_and_reuses_paused_id(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            catalog = WorkspaceCatalog(root)
            (root / "courses").mkdir()
            (root / "courses" / "index.json").write_text(
                json.dumps({"schema_version": 1, "course_ids": []}), encoding="utf-8"
            )
            course = catalog.create_course(title="Networks")
            lecture = catalog.create_lecture(course["id"], number="01", title="Intro")
            catalog.upload_material(course["id"], lecture["id"], filename="intro.pdf", content=b"%PDF")
            prepared = catalog.lecture_root(course["id"], lecture["id"]) / "prepared"
            prepared.mkdir()
            (prepared / "lecture_scaffold.json").write_text("# Intro", encoding="utf-8")
            (prepared / "lecture_scaffold.md").write_text("# Slides", encoding="utf-8")
            (prepared / "asr_context.txt").write_text("Lecture context", encoding="utf-8")
            (prepared / "hotwords.json").write_text("[]", encoding="utf-8")
            catalog.mark_prepared(course["id"], lecture["id"])
            catalog.committed_preparation_snapshot = mock.Mock(
                return_value={
                    "runId": "prep-run-1",
                    "manifestSha256": "a" * 64,
                    "inputFingerprint": {"materialSha256": "b" * 64},
                    "artifactHashes": {},
                    "hotwordsSnapshot": [],
                    "hotwordVocabularySnapshot": {},
                    "capturedAt": "2026-09-14T00:00:00+00:00",
                }
            )
            client = TestClient(create_app(settings(root / "data"), workspace_catalog=catalog))
            base = f"/api/courses/{course['id']}/lectures/{lecture['id']}/sessions"

            before_start = client.get(f"{base}/current")
            started = client.post(base)
            session_id = started.json()["id"]
            paused = client.post(f"{base}/{session_id}/pause")
            resumed = client.post(f"{base}/{session_id}/resume")
            stopped = client.post(f"{base}/{session_id}/complete")
            listed = client.get(base)
            lecture_response = client.get(
                f"/api/courses/{course['id']}/lectures/{lecture['id']}"
            )

        self.assertIsNone(before_start.json()["session"])
        self.assertEqual(started.status_code, 201)
        self.assertEqual(started.json()["agent1Mode"], "detached")
        self.assertIsNone(started.json()["agent1PromptVersionId"])
        self.assertEqual(
            started.json()["realtimeModels"],
            {
                "asr": "qwen-audio-3.1-asr-flash-streaming",
                "translation": "qwen3.7-flash",
                "semanticPunctuation": True,
            },
        )
        self.assertTrue(started.json()["transcriptPath"].endswith("realtime_final.json"))
        self.assertTrue(
            started.json()["realtimeSnapshotsPath"].endswith(
                "realtime_snapshots.jsonl"
            )
        )
        self.assertEqual(paused.json()["status"], "paused")
        self.assertEqual(resumed.json()["id"], session_id)
        self.assertEqual(resumed.json()["status"], "live")
        self.assertEqual(stopped.json()["status"], "completed")
        self.assertEqual(len(listed.json()["sessions"]), 1)
        self.assertEqual(lecture_response.json()["preparationStatus"], "ready")
        self.assertIsNone(lecture_response.json()["activeSession"])

    def test_realtime_session_context_requires_both_ids(self) -> None:
        websocket = mock.Mock(query_params={"course_id": "open-system"})

        with self.assertRaisesRegex(ValueError, "must be provided together"):
            _session_settings(websocket, settings(Path("data")))

    def test_realtime_session_context_rejects_unsafe_ids(self) -> None:
        websocket = mock.Mock(
            query_params={"course_id": "../outside", "lecture_id": "lecture"}
        )

        with self.assertRaisesRegex(ValueError, "invalid course_id"):
            _session_settings(websocket, settings(Path("data")))

    def test_missing_session_context_preserves_server_defaults(self) -> None:
        current = settings(Path("data"))
        websocket = mock.Mock(query_params={})

        self.assertIs(_session_settings(websocket, current), current)

    def test_health_and_frontend_are_served_by_fastapi(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            # App construction recovers preparation state. Keep that operation
            # inside this test's workspace, never the user's running workspace.
            root = Path(directory)
            (root / "courses").mkdir()
            (root / "courses/index.json").write_text('{"schema_version":1,"course_ids":[]}', encoding="utf-8")
            client = TestClient(create_app(settings(root), workspace_catalog=WorkspaceCatalog(root)))
            health = client.get("/health")
            frontend = client.get("/")
            workspace = client.get(
                "/courses/open-system/lectures/networks-introduction/live"
            )
            agent_lab = client.get("/agent-lab")
            instructions = client.get("/instructions")

        self.assertEqual(health.status_code, 200)
        self.assertEqual(health.json(), {"ok": True})
        self.assertEqual(frontend.status_code, 200)
        self.assertIn("text/html", frontend.headers["content-type"])
        self.assertEqual(workspace.status_code, 200)
        self.assertIn("text/html", workspace.headers["content-type"])
        self.assertEqual(agent_lab.status_code, 200)
        self.assertIn("text/html", agent_lab.headers["content-type"])
        self.assertEqual(instructions.status_code, 200)
        self.assertIn("text/html", instructions.headers["content-type"])

    def test_static_path_cannot_escape_frontend_root(self) -> None:
        self.assertIsNone(_frontend_file("../../.env"))

if __name__ == "__main__":
    unittest.main()
