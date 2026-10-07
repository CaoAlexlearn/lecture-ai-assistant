from __future__ import annotations

import json
import fcntl
from pathlib import Path

import pytest

from workspace_catalog import (
    PreparationManager,
    WorkspaceCatalog,
    WorkspaceCatalogError,
)


def write_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")


def test_recovery_preserves_live_worker_and_recovers_released_lock(catalog):
    course = catalog.create_course(title="HTML and CSS")
    lecture = catalog.create_lecture(course["id"], number="02", title="HTML and CSS")
    catalog.upload_material(course["id"], lecture["id"], filename="slides.pdf", content=b"%PDF-test")
    catalog.mark_preparing(course["id"], lecture["id"], execution_backend="codex")
    root = catalog.lecture_root(course["id"], lecture["id"])
    state_path = root / "runtime/preagent_state.json"
    write_json(state_path, {"status": "running", "current_stage": "enriching_nodes",
                           "error": None, "stages": {"enriching_nodes": {"status": "running", "error": None}}})
    original = state_path.read_bytes()
    with state_path.with_name("preagent.lock").open("a+b") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        PreparationManager(catalog)
        assert catalog.get_lecture(course["id"], lecture["id"])["preparationStatus"] == "preparing"
        assert state_path.read_bytes() == original
    # Merely having a lock file does not make a dead worker live.
    PreparationManager(catalog)
    assert catalog.get_lecture(course["id"], lecture["id"])["preparationStatus"] == "failed"
    assert json.loads(state_path.read_text())["status"] == "failed"


def ready_lecture(catalog: WorkspaceCatalog) -> tuple[dict, dict]:
    course = catalog.create_course(title="Networks")
    lecture = catalog.create_lecture(course["id"], number="01", title="Intro")
    catalog.upload_material(
        course["id"], lecture["id"], filename="intro.pdf", content=b"%PDF-test"
    )
    root = catalog.lecture_root(course["id"], lecture["id"])
    prepared = root / "prepared"
    prepared.mkdir()
    (prepared / "lecture_scaffold.json").write_text("# Intro", encoding="utf-8")
    (prepared / "lecture_scaffold.md").write_text("# Slides", encoding="utf-8")
    (prepared / "asr_context.txt").write_text("Lecture context", encoding="utf-8")
    (prepared / "hotwords.json").write_text("[]", encoding="utf-8")
    catalog.mark_prepared(course["id"], lecture["id"])
    return course, catalog.get_lecture(course["id"], lecture["id"])


@pytest.fixture
def catalog(tmp_path: Path) -> WorkspaceCatalog:
    write_json(
        tmp_path / "courses" / "index.json",
        {"schema_version": 1, "course_ids": []},
    )
    return WorkspaceCatalog(tmp_path)


def test_course_and_lecture_registry_preserves_stable_order(
    catalog: WorkspaceCatalog,
) -> None:
    first = catalog.create_course(title="Open Systems")
    second = catalog.create_course(title="Engineering Economics")
    lecture_2 = catalog.create_lecture(
        first["id"], number="02", title="Application Layer"
    )
    lecture_1 = catalog.create_lecture(
        first["id"], number="01", title="Networks Introduction"
    )

    courses = catalog.list_courses()

    assert [item["id"] for item in courses] == [first["id"], second["id"]]
    assert [item["id"] for item in courses[0]["lectures"]] == [
        lecture_2["id"],
        lecture_1["id"],
    ]


def test_course_and_lecture_display_fields_update_without_changing_ids(
    catalog: WorkspaceCatalog,
) -> None:
    course = catalog.create_course(
        title="Wrong Course", code="OLD", description="Wrong description"
    )
    lecture = catalog.create_lecture(course["id"], number="99", title="Wrong")
    lecture_root = catalog.lecture_root(course["id"], lecture["id"])

    updated_course = catalog.update_course(
        course["id"],
        title="Correct Course",
        code="NEW",
        description="Correct description",
    )
    updated_lecture = catalog.update_lecture(
        course["id"], lecture["id"], number="01", title="Introduction"
    )

    assert updated_course["id"] == course["id"]
    assert updated_course["title"] == "Correct Course"
    assert updated_course["code"] == "NEW"
    assert updated_course["description"] == "Correct description"
    assert updated_lecture["id"] == lecture["id"]
    assert updated_lecture["workspace_dir"] == lecture["workspace_dir"]
    assert updated_lecture["number"] == "01"
    assert updated_lecture["title"] == "Introduction"
    assert catalog.lecture_root(course["id"], lecture["id"]) == lecture_root


def test_delete_lecture_removes_registry_workspace_and_runtime_data(
    catalog: WorkspaceCatalog,
) -> None:
    course = catalog.create_course(title="Networks")
    lecture = catalog.create_lecture(course["id"], number="01", title="Intro")
    lecture_root = catalog.lecture_root(course["id"], lecture["id"])
    runtime_root = catalog.project_root / "data" / course["id"] / lecture["id"]
    runtime_root.mkdir(parents=True)
    (runtime_root / "sentinel").write_text("runtime", encoding="utf-8")

    catalog.delete_lecture(course["id"], lecture["id"])

    assert catalog.get_course(course["id"])["lectures"] == []
    assert not lecture_root.exists()
    assert not runtime_root.exists()


def test_delete_course_cascades_only_when_no_work_is_active(
    catalog: WorkspaceCatalog,
) -> None:
    course = catalog.create_course(title="Networks")
    lecture = catalog.create_lecture(course["id"], number="01", title="Intro")
    catalog.upload_material(
        course["id"], lecture["id"], filename="intro.pdf", content=b"%PDF-test"
    )
    catalog.mark_preparing(course["id"], lecture["id"])

    with pytest.raises(WorkspaceCatalogError, match="preparation is running"):
        catalog.delete_course(course["id"])

    catalog.mark_preparation_failed(course["id"], lecture["id"], "stopped")
    runtime_root = catalog.project_root / "data" / course["id"]
    runtime_root.mkdir(parents=True)
    catalog.delete_course(course["id"])

    assert catalog.list_courses() == []
    assert not (catalog.courses_root / course["id"]).exists()
    assert not runtime_root.exists()


def test_delete_lecture_rejects_live_or_paused_session(
    catalog: WorkspaceCatalog,
) -> None:
    course, lecture = ready_lecture(catalog)
    session = catalog.start_session(course["id"], lecture["id"])

    with pytest.raises(WorkspaceCatalogError, match="live or paused"):
        catalog.delete_lecture(course["id"], lecture["id"])

    catalog.pause_session(course["id"], lecture["id"], session["id"])
    with pytest.raises(WorkspaceCatalogError, match="live or paused"):
        catalog.delete_lecture(course["id"], lecture["id"])


def test_lecture_preparation_status_is_saved_by_one_catalog_authority(
    catalog: WorkspaceCatalog,
) -> None:
    course = catalog.create_course(title="Networks")
    lecture = catalog.create_lecture(course["id"], number="01", title="Intro")
    assert lecture["preparationStatus"] == "created"

    uploaded = catalog.upload_material(
        course["id"], lecture["id"], filename="intro.pdf", content=b"%PDF-test"
    )
    assert uploaded["preparationStatus"] == "material_uploaded"

    preparing = catalog.mark_preparing(course["id"], lecture["id"])
    assert preparing["preparationStatus"] == "preparing"
    catalog.mark_preparation_failed(course["id"], lecture["id"], "provider failed")
    failed = catalog.get_lecture(course["id"], lecture["id"])
    assert failed["preparationStatus"] == "failed"
    assert failed["preparation"]["error"] == "provider failed"


def test_ready_requires_the_complete_prepared_output_set(
    catalog: WorkspaceCatalog,
) -> None:
    course = catalog.create_course(title="Networks")
    lecture = catalog.create_lecture(course["id"], number="01", title="Intro")
    catalog.upload_material(
        course["id"], lecture["id"], filename="intro.pdf", content=b"%PDF-test"
    )
    catalog.mark_preparing(course["id"], lecture["id"])
    root = catalog.lecture_root(course["id"], lecture["id"])
    prepared = root / "prepared"
    prepared.mkdir()
    (prepared / "lecture_scaffold.json").write_text("# Intro", encoding="utf-8")
    (prepared / "lecture_scaffold.md").write_text("# Slides", encoding="utf-8")
    (prepared / "asr_context.txt").write_text("Lecture context", encoding="utf-8")
    (prepared / "hotwords.json").write_text("[]", encoding="utf-8")

    catalog.mark_prepared(course["id"], lecture["id"])
    ready = catalog.get_lecture(course["id"], lecture["id"])

    assert ready["preparationStatus"] == "ready"
    assert ready["outputs"] == {"scaffold": True, "markdown": True, "hotwords": True, "asr_context": True}
    assert ready["urls"]["scaffold"].endswith("/files/scaffold")

    (prepared / "hotwords.json").unlink()
    assert catalog.get_lecture(course["id"], lecture["id"])["preparationStatus"] == "material_uploaded"


def test_replace_material_marks_existing_preparation_outdated(
    catalog: WorkspaceCatalog,
) -> None:
    course = catalog.create_course(title="Networks")
    lecture = catalog.create_lecture(course["id"], number="01", title="Intro")
    catalog.upload_material(
        course["id"], lecture["id"], filename="intro.pdf", content=b"%PDF-one"
    )
    metadata = catalog._read_json(
        catalog.lecture_root(course["id"], lecture["id"]) / "lecture.json"
    )
    metadata["prepared_at"] = "2026-09-12T00:00:00+00:00"
    catalog._save_lecture(course["id"], metadata)

    replaced = catalog.upload_material(
        course["id"], lecture["id"], filename="intro-v2.pdf", content=b"%PDF-two"
    )

    assert replaced["preparationStatus"] == "outdated"


def test_upload_rejects_unrelated_file_types(catalog: WorkspaceCatalog) -> None:
    course = catalog.create_course(title="Networks")
    lecture = catalog.create_lecture(course["id"], number="01", title="Intro")
    with pytest.raises(WorkspaceCatalogError, match="Only PDF and PPTX"):
        catalog.upload_material(
            course["id"], lecture["id"], filename="notes.txt", content=b"notes"
        )


def test_process_owned_status_is_recovered_after_restart(
    catalog: WorkspaceCatalog,
) -> None:
    course = catalog.create_course(title="Networks")
    lecture = catalog.create_lecture(course["id"], number="01", title="Intro")
    catalog.upload_material(
        course["id"], lecture["id"], filename="intro.pdf", content=b"%PDF-test"
    )
    catalog.mark_preparing(course["id"], lecture["id"])

    PreparationManager(catalog)

    recovered = catalog.get_lecture(course["id"], lecture["id"])
    assert recovered["preparationStatus"] == "failed"
    assert "interrupted" in recovered["preparation"]["error"]


def test_preparation_and_session_lifecycles_are_independent(
    catalog: WorkspaceCatalog,
) -> None:
    course, lecture = ready_lecture(catalog)

    session = catalog.start_session(course["id"], lecture["id"])
    during = catalog.get_lecture(course["id"], lecture["id"])
    completed = catalog.complete_session(course["id"], lecture["id"], session["id"])
    after = catalog.get_lecture(course["id"], lecture["id"])

    assert session["status"] == "live"
    assert during["preparationStatus"] == "ready"
    assert during["activeSession"]["id"] == session["id"]
    assert completed["status"] == "completed"
    assert completed["endedAt"]
    assert after["preparationStatus"] == "ready"
    assert after["activeSession"] is None


def test_pause_resume_reuses_session_and_lecture_supports_multiple_sessions(
    catalog: WorkspaceCatalog,
) -> None:
    course, lecture = ready_lecture(catalog)
    first = catalog.start_session(course["id"], lecture["id"])

    paused = catalog.pause_session(course["id"], lecture["id"], first["id"])
    resumed = catalog.resume_session(course["id"], lecture["id"], first["id"])
    catalog.complete_session(course["id"], lecture["id"], first["id"])
    second = catalog.start_session(course["id"], lecture["id"])

    assert paused["id"] == resumed["id"] == first["id"]
    assert paused["status"] == "paused"
    assert resumed["status"] == "live"
    assert second["id"] != first["id"]
    assert len(catalog.list_sessions(course["id"], lecture["id"])) == 2


@pytest.mark.parametrize("legacy_status", ["live", "completed"])
def test_legacy_runtime_status_migrates_to_ready_with_one_session(
    catalog: WorkspaceCatalog, legacy_status: str
) -> None:
    course, lecture = ready_lecture(catalog)
    path = catalog.lecture_root(course["id"], lecture["id"]) / "lecture.json"
    metadata = catalog._read_json(path)
    metadata.pop("preparationStatus")
    metadata["status"] = legacy_status
    write_json(path, metadata)

    migrated = catalog.get_lecture(course["id"], lecture["id"])
    again = catalog.get_lecture(course["id"], lecture["id"])

    assert migrated["preparationStatus"] == "ready"
    assert migrated["lastSession"]["status"] == legacy_status
    assert migrated["lastSession"]["legacy"] is True
    assert len(catalog.list_sessions(course["id"], lecture["id"])) == 1
    assert again["lastSession"]["id"] == migrated["lastSession"]["id"]


def test_paused_session_blocks_accidental_new_session(catalog: WorkspaceCatalog) -> None:
    course, lecture = ready_lecture(catalog)
    session = catalog.start_session(course["id"], lecture["id"])
    catalog.pause_session(course["id"], lecture["id"], session["id"])

    with pytest.raises(WorkspaceCatalogError, match="Resume the paused session"):
        catalog.start_session(course["id"], lecture["id"])
