"""Relocate a closed capture without touching its live recording connection.

Run with a durable intent JSON path. Originals are kept in the intent's audit
directory; only lecture attribution and metadata paths change in the new copy.
"""
from __future__ import annotations

import argparse
import copy
import fcntl
import hashlib
import json
import os
from pathlib import Path
import shutil
import time
from datetime import UTC, datetime


def read(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def write(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + ".tmp")
    with temp.open("w", encoding="utf-8") as handle:
        json.dump(value, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temp, path)


def now() -> str:
    return datetime.now(UTC).isoformat()


def locations(root: Path, intent: dict) -> tuple[Path, Path, Path, Path]:
    course, source, target, sid = (intent[k] for k in (
        "courseId", "sourceLectureId", "targetLectureId", "sessionId"))
    for part in (course, source, target, sid):
        if not isinstance(part, str) or not part or Path(part).name != part or part in {".", ".."}:
            raise ValueError("Invalid relocation identity")
    if source == target:
        raise ValueError("Source and target are identical")
    lectures = root / "courses" / course / "lectures"
    data = root / "data" / course
    return (lectures / source / "sessions" / sid,
            lectures / target / "sessions" / sid,
            data / source / sid, data / target / sid)


def ready(session: dict, data: Path, *, allow_paused: bool = False) -> bool:
    closed = (session.get("status"), session.get("runtimeState")) == ("completed", "stopped")
    paused = allow_paused and (session.get("status"), session.get("runtimeState")) == ("paused", "paused")
    if not (closed or paused):
        return False
    if not all((data / name).is_file() for name in ("realtime_latest.json", "realtime_final.json")):
        return False
    latest, final = (read(data / name) for name in ("realtime_latest.json", "realtime_final.json"))
    return (latest == final and final.get("status") in {"final", "interrupted", "error"}
            and final.get("connection_generation") == session.get("connectionGeneration"))


def rewrite_record(record: dict, intent: dict) -> dict:
    expected = {"course_id": intent["courseId"], "lecture_id": intent["sourceLectureId"],
                "session_id": intent["sessionId"]}
    if any(record.get(k) != v for k, v in expected.items()):
        raise ValueError("Transcript identity mismatch")
    return {**record, "lecture_id": intent["targetLectureId"]}


def transcript_copy(source: Path, target: Path, intent: dict) -> dict:
    report = {}
    for name in ("realtime_latest.json", "realtime_final.json"):
        original = read(source / name)
        write(target / name, rewrite_record(original, intent))
        restored = read(target / name)
        restored["lecture_id"] = intent["sourceLectureId"]
        if restored != original:
            raise ValueError("Transcript payload changed")
    digest = hashlib.sha256()
    count = 0
    last = None
    with (source / "realtime_snapshots.jsonl").open("rb") as incoming, (target / "realtime_snapshots.jsonl").open("wb") as outgoing:
        for line in incoming:
            digest.update(line)
            original = json.loads(line)
            last = rewrite_record(original, intent)
            outgoing.write((json.dumps(last, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8"))
            count += 1
        outgoing.flush()
        os.fsync(outgoing.fileno())
    if last != read(target / "realtime_final.json"):
        raise ValueError("Append log does not end with the final snapshot")
    # Verify every field of every history record, including English source text.
    with (source / "realtime_snapshots.jsonl").open("rb") as incoming, (target / "realtime_snapshots.jsonl").open("rb") as outgoing:
        for line in incoming:
            converted = json.loads(next(outgoing))
            converted["lecture_id"] = intent["sourceLectureId"]
            if converted != json.loads(line):
                raise ValueError("History payload changed")
        if outgoing.read(1):
            raise ValueError("Extra history records")
    report.update(snapshotCount=count, originalSnapshotsSha256=digest.hexdigest())
    return report


def relocate(root: Path, intent_path: Path) -> bool:
    intent = read(intent_path)
    if intent.get("status") == "completed":
        return True
    source_meta, target_meta, source_data, target_data = locations(root, intent)
    session = read(source_meta / "session.json")
    allow_paused = intent.get("allowPaused") is True
    if not ready(session, source_data, allow_paused=allow_paused):
        return False
    if session.get("lectureId") != intent["sourceLectureId"] or session.get("id") != intent["sessionId"] or session.get("courseId") != intent["courseId"]:
        raise ValueError("Session identity mismatch")
    if target_meta.exists() or target_data.exists():
        raise ValueError("Destination already exists; refusing to overwrite")
    if any((source_data / name).exists() for name in ("agent1", "agent2", "lecture_dense_transcript.jsonl")):
        raise ValueError("Post-class outputs already exist; review them before relocating")
    counts = session.get("agent1JobStatus", {})
    if any(counts.get(k, 0) for k in ("pending", "running", "written")):
        raise ValueError("Agent1 work exists; relocation needs review")
    from workspace_catalog import WorkspaceCatalog
    catalog = WorkspaceCatalog(root)
    target_preparation = catalog.committed_preparation_snapshot(intent["courseId"], intent["targetLectureId"])
    if session["status"] == "paused" and catalog.get_current_session(intent["courseId"], intent["targetLectureId"]) is not None:
        raise ValueError("Target lecture already has a live or paused session")
    audit = intent_path.parent / (intent["sessionId"] + "-original")
    if audit.exists():
        raise ValueError("Audit directory already exists; inspect previous attempt")
    stage = intent_path.parent / (intent["sessionId"] + "-staging")
    if stage.exists():
        shutil.rmtree(stage)
    required = sum(p.stat().st_size for p in source_data.rglob("*") if p.is_file())
    if shutil.disk_usage(root).free < required * 2 + 100_000_000:
        raise ValueError("Not enough free disk space for a verified copy")
    intent.update(status="copying", updatedAt=now())
    write(intent_path, intent)
    stage.mkdir()
    try:
        shutil.copytree(source_data, stage / "data", ignore=shutil.ignore_patterns("realtime_*.json", "realtime_snapshots.jsonl"))
        report = transcript_copy(source_data, stage / "data", intent)
        shutil.copytree(source_meta, stage / "metadata")
        moved = copy.deepcopy(session)
        moved["lectureId"] = intent["targetLectureId"]
        old_prefix = source_data.relative_to(root).as_posix() + "/"
        new_prefix = target_data.relative_to(root).as_posix() + "/"
        for key, value in moved.items():
            if isinstance(value, str) and value.startswith(old_prefix):
                moved[key] = new_prefix + value[len(old_prefix):]
        # Frozen hotwords/preparation describe what ASR actually used, and stay
        # unchanged. Target context is recorded separately for subsequent work.
        moved["recordingLectureId"] = intent["sourceLectureId"]
        moved["targetPreparationSnapshot"] = target_preparation
        if session["status"] == "paused":
            # A resumed socket reads these canonical fields. Preserve the old
            # generation's actual ASR configuration separately before switching.
            moved["recordingPreparationSnapshot"] = copy.deepcopy(session.get("preparationSnapshot"))
            moved["recordingHotwordsSnapshot"] = copy.deepcopy(session.get("hotwordsSnapshot", []))
            moved["recordingHotwordVocabularySnapshot"] = copy.deepcopy(session.get("hotwordVocabularySnapshot", {}))
            moved["preparationSnapshot"] = target_preparation
            moved["preparationRunId"] = target_preparation["runId"]
            moved["hotwordsSnapshot"] = list(target_preparation.get("hotwordsSnapshot", []))
            moved["hotwordVocabularySnapshot"] = dict(target_preparation.get("hotwordVocabularySnapshot", {}))
        moved["relocation"] = {"fromLectureId": intent["sourceLectureId"],
                               "toLectureId": intent["targetLectureId"], "movedAt": now(),
                               "originalDataPath": str((audit / "data").relative_to(root))}
        catalog._validate_session(moved, intent["courseId"], intent["targetLectureId"])
        write(stage / "metadata" / "session.json", moved)
        # Source must still be exactly the stopped capture we checked.
        if read(source_meta / "session.json") != session or not ready(session, source_data, allow_paused=allow_paused):
            raise ValueError("Session changed during relocation")
        audit.mkdir()
        target_data.parent.mkdir(parents=True, exist_ok=True)
        target_meta.parent.mkdir(parents=True, exist_ok=True)
        try:
            os.rename(stage / "data", target_data)
            os.rename(stage / "metadata", target_meta)
            os.rename(source_meta, audit / "metadata")
            os.rename(source_data, audit / "data")
        except BaseException:
            if (audit / "metadata").exists():
                os.rename(audit / "metadata", source_meta)
            if (audit / "data").exists():
                os.rename(audit / "data", source_data)
            for path in (target_meta, target_data):
                if path.exists():
                    shutil.rmtree(path)
            audit.rmdir()
            raise
        intent.update(status="completed", completedAt=now(), verification=report,
                      destinationSessionPath=str((target_meta / "session.json").relative_to(root)))
        write(intent_path, intent)
        return True
    finally:
        if stage.exists():
            shutil.rmtree(stage)


def watch(root: Path, intent_path: Path) -> None:
    with intent_path.with_suffix(".lock").open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        intent = read(intent_path)
        intent.update(watcherPid=os.getpid(), watcherStartedAt=now())
        write(intent_path, intent)
        print("Waiting for the recording to end normally", flush=True)
        while True:
            try:
                if relocate(root, intent_path):
                    print("Relocation completed and verified", flush=True)
                    return
            except Exception as exc:
                intent = read(intent_path)
                intent.update(status="needs_attention", error=str(exc), updatedAt=now())
                write(intent_path, intent)
                raise
            time.sleep(2)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("intent", type=Path)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parent)
    args = parser.parse_args()
    watch(args.root.resolve(), args.intent.resolve())
