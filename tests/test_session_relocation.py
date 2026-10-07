import json
from pathlib import Path
from unittest.mock import patch

import pytest

from session_relocation import locations, read, relocate, write


@pytest.fixture
def capture(tmp_path):
    intent = {"courseId": "test-course", "sourceLectureId": "lecture-04",
              "targetLectureId": "lecture-03", "sessionId": "capture", "status": "waiting"}
    task = tmp_path / "runtime" / "session_relocations" / "capture.json"
    write(task, intent)
    source_meta, target_meta, source_data, target_data = locations(tmp_path, intent)
    source_data.mkdir(parents=True)
    session = {"id": "capture", "courseId": "test-course", "lectureId": "lecture-04",
               "status": "live", "runtimeState": "connected", "connectionGeneration": 2,
               "hotwordsSnapshot": ["Actual recording vocabulary"],
               "preparationSnapshot": {"runId": "original-preparation"},
               "transcriptPath": str(source_data.relative_to(tmp_path) / "realtime_final.json")}
    write(source_meta / "session.json", session)
    record = {"course_id": "test-course", "lecture_id": "lecture-04", "session_id": "capture",
              "connection_generation": 2, "revision": 2, "status": "final",
              "source": {"committed": "TCP sequence number. 原始内容", "unstable": "", "display": "TCP sequence number. 原始内容"}}
    earlier = {**record, "connection_generation": 1, "revision": 1, "status": "interrupted"}
    for name in ("realtime_latest.json", "realtime_final.json"):
        write(source_data / name, record)
    (source_data / "realtime_snapshots.jsonl").write_text("\n".join(json.dumps(x, ensure_ascii=False) for x in (earlier, record)) + "\n")
    (source_data / "audio.bin").write_bytes(b"original audio")
    return tmp_path, task, source_meta, target_meta, source_data, target_data, session, record


def test_live_capture_is_never_modified(capture):
    root, task, source_meta, target_meta, source_data, target_data, session, record = capture
    before = {str(p): p.read_bytes() for p in root.rglob("*") if p.is_file()}
    assert relocate(root, task) is False
    assert before == {str(p): p.read_bytes() for p in root.rglob("*") if p.is_file()}
    assert not target_meta.exists() and not target_data.exists()


def test_stale_previous_connection_is_not_migrated(capture):
    root, task, source_meta, target_meta, source_data, target_data, session, record = capture
    session.update(status="completed", runtimeState="stopped", connectionGeneration=3)
    write(source_meta / "session.json", session)
    assert relocate(root, task) is False
    assert source_data.exists() and not target_data.exists()


def test_paused_migration_keeps_pause_and_switches_future_vocabulary(capture):
    root, task, source_meta, target_meta, source_data, target_data, session, record = capture
    session.update(status="paused", runtimeState="paused", pauseIntervals=[{"startedAt": "pause-time", "endedAt": None}])
    write(source_meta / "session.json", session)
    assert relocate(root, task) is False
    intent = read(task)
    intent["allowPaused"] = True
    write(task, intent)
    target = {"runId": "transport-prep", "hotwordsSnapshot": ["Selective Repeat"],
              "hotwordVocabularySnapshot": {"Selective Repeat": 2}}
    with patch("workspace_catalog.WorkspaceCatalog.committed_preparation_snapshot", return_value=target), patch("workspace_catalog.WorkspaceCatalog.get_current_session", return_value=None):
        assert relocate(root, task) is True
    moved = read(target_meta / "session.json")
    assert (moved["status"], moved["runtimeState"]) == ("paused", "paused")
    assert moved["pauseIntervals"] == session["pauseIntervals"]
    assert moved["connectionGeneration"] == session["connectionGeneration"]
    assert moved["preparationSnapshot"] == target
    assert moved["hotwordVocabularySnapshot"] == target["hotwordVocabularySnapshot"]
    assert moved["recordingHotwordsSnapshot"] == session["hotwordsSnapshot"]
    assert moved["recordingPreparationSnapshot"] == session["preparationSnapshot"]


def test_paused_migration_refuses_occupied_target(capture):
    root, task, source_meta, target_meta, source_data, target_data, session, record = capture
    session.update(status="paused", runtimeState="paused")
    write(source_meta / "session.json", session)
    intent = read(task)
    intent["allowPaused"] = True
    write(task, intent)
    with patch("workspace_catalog.WorkspaceCatalog.committed_preparation_snapshot", return_value={}), patch("workspace_catalog.WorkspaceCatalog.get_current_session", return_value={"id": "other"}):
        with pytest.raises(ValueError, match="Target lecture already"):
            relocate(root, task)
    assert source_meta.exists() and source_data.exists()
    assert not target_meta.exists() and not target_data.exists()


def test_migration_preserves_all_content_and_provenance(capture):
    root, task, source_meta, target_meta, source_data, target_data, session, record = capture
    session.update(status="completed", runtimeState="stopped")
    write(source_meta / "session.json", session)
    original_history = (source_data / "realtime_snapshots.jsonl").read_bytes()
    with patch("workspace_catalog.WorkspaceCatalog.committed_preparation_snapshot", return_value={"runId": "target-preparation"}):
        assert relocate(root, task) is True
    moved = read(target_meta / "session.json")
    assert moved["lectureId"] == "lecture-03"
    assert moved["transcriptPath"] == str(target_data.relative_to(root) / "realtime_final.json")
    assert moved["preparationSnapshot"] == session["preparationSnapshot"]
    assert moved["hotwordsSnapshot"] == session["hotwordsSnapshot"]
    restored = read(target_data / "realtime_final.json")
    restored["lecture_id"] = "lecture-04"
    assert restored == record
    assert (target_data / "audio.bin").read_bytes() == b"original audio"
    audit = task.parent / "capture-original"
    assert (audit / "data" / "realtime_snapshots.jsonl").read_bytes() == original_history
    assert read(audit / "metadata" / "session.json") == session
    assert read(task)["verification"]["snapshotCount"] == 2
    assert not source_meta.exists() and not source_data.exists()
    assert relocate(root, task) is True


def test_publish_failure_restores_source(capture):
    root, task, source_meta, target_meta, source_data, target_data, session, record = capture
    session.update(status="completed", runtimeState="stopped")
    write(source_meta / "session.json", session)
    import os
    rename = os.rename

    def fail_source_data(source, target):
        if Path(source) == source_data:
            raise OSError("simulated publish failure")
        return rename(source, target)

    with patch("workspace_catalog.WorkspaceCatalog.committed_preparation_snapshot", return_value={}), patch("session_relocation.os.rename", side_effect=fail_source_data):
        with pytest.raises(OSError, match="simulated publish failure"):
            relocate(root, task)
    assert read(source_meta / "session.json") == session
    assert read(source_data / "realtime_final.json") == record
    assert not target_meta.exists() and not target_data.exists()
