"""Parse one lecture document with MinerU into the active course directory."""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

from dotenv import load_dotenv

from mineru_normalizer import (
    MinerUNormalizationError,
    load_and_normalize_mineru_file,
    render_visual_slides,
    write_normalized_document_atomic,
)

PROJECT_ROOT = Path(__file__).resolve().parent
SUPPORTED_SUFFIXES = {".pdf", ".png", ".jpg", ".jpeg", ".docx", ".pptx", ".xlsx"}
LOCAL_PROXY_BYPASS = ("localhost", "127.0.0.1", "::1")


class CoursePptError(ValueError):
    """Raised when the course PPT preprocessing request is invalid."""


def prepare_canonical_lecture_workspace(
    *,
    lecture_root: Path,
    source_pdf: Path,
    lecture_id: str,
) -> dict[str, object]:
    """Stage raw MinerU evidence, normalize it, and render visual PDF pages."""

    canonical_content = lecture_root / "source" / "mineru" / "content_list_v2.json"
    candidates = sorted(
        path
        for path in lecture_root.rglob("*_content_list_v2.json")
        if path.resolve() != canonical_content.resolve()
    )
    if len(candidates) != 1:
        raise CoursePptError(
            "Expected exactly one raw MinerU content_list_v2 JSON, found "
            f"{len(candidates)}"
        )
    raw_content = candidates[0]
    raw_images = raw_content.parent / "images"
    canonical_source = lecture_root / "source" / "mineru"
    canonical_source.mkdir(parents=True, exist_ok=True)
    shutil.copy2(raw_content, canonical_content)
    if raw_images.is_dir():
        shutil.copytree(raw_images, canonical_source / "images", dirs_exist_ok=True)
    canonical_pdf = lecture_root / "source" / "original.pdf"
    canonical_pdf.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source_pdf, canonical_pdf)

    try:
        normalized = load_and_normalize_mineru_file(
            canonical_content,
            lecture_id=lecture_id,
        )
        render_validation = render_visual_slides(
            normalized,
            source_pdf=canonical_pdf,
            workspace_root=lecture_root,
        )
        write_normalized_document_atomic(
            normalized,
            lecture_root / "normalized" / "normalized_slides.json",
            images_dir=canonical_source / "images",
            workspace_root=lecture_root,
        )
    except MinerUNormalizationError as exc:
        raise CoursePptError(f"Canonical deterministic preprocessing failed: {exc}") from exc
    return {
        "normalized_path": "normalized/normalized_slides.json",
        "visual_slides": render_validation["stats"]["visual_slides"],
        "visual_fragments": render_validation["stats"]["visual_fragments"],
    }


def resolve_course_directory(project_root: Path, course_id: str) -> Path:
    """Resolve an existing course without allowing path traversal."""

    if (
        not course_id
        or Path(course_id).parts != (course_id,)
        or course_id in {".", ".."}
    ):
        raise CoursePptError(f"Invalid course ID: {course_id!r}")

    courses_root = (project_root / "courses").resolve()
    course_directory = (courses_root / course_id).resolve()
    try:
        course_directory.relative_to(courses_root)
    except ValueError as exc:
        raise CoursePptError(f"Invalid course ID: {course_id!r}") from exc

    if not (course_directory / "course.md").is_file():
        raise CoursePptError(
            f"Course {course_id!r} does not exist or has no course.md under {courses_root}"
        )
    return course_directory


def resolve_input_path(raw_path: str) -> Path:
    input_path = Path(raw_path).expanduser().resolve()
    if not input_path.is_file():
        raise CoursePptError(f"Input file does not exist: {input_path}")
    if input_path.suffix.lower() not in SUPPORTED_SUFFIXES:
        supported = ", ".join(sorted(SUPPORTED_SUFFIXES))
        raise CoursePptError(
            f"Unsupported input type {input_path.suffix!r}; supported: {supported}"
        )
    return input_path


def find_mineru(explicit_path: str | None, project_root: Path) -> Path:
    """Find MinerU from an argument, environment, PATH, or known local venvs."""

    configured = explicit_path or os.getenv("MINERU_BIN", "").strip()
    candidates: list[Path] = []
    if configured:
        candidates.append(Path(configured).expanduser())

    path_command = shutil.which("mineru")
    if path_command:
        candidates.append(Path(path_command))

    candidates.extend(
        [
            project_root / ".venv" / "bin" / "mineru",
            project_root.parent / "法律agent_test" / ".venv" / "bin" / "mineru",
        ]
    )

    for candidate in candidates:
        resolved = candidate.resolve()
        if resolved.is_file() and os.access(resolved, os.X_OK):
            return resolved

    raise CoursePptError(
        "MinerU executable was not found. Set MINERU_BIN in .env or pass --mineru-bin."
    )


def add_local_proxy_bypass(environment: dict[str, str]) -> dict[str, str]:
    """Keep external proxy settings while forcing MinerU localhost traffic direct."""

    result = environment.copy()
    existing: list[str] = []
    for key in ("NO_PROXY", "no_proxy"):
        existing.extend(item.strip() for item in result.get(key, "").split(","))

    values: list[str] = []
    for value in (*existing, *LOCAL_PROXY_BYPASS):
        if value and value not in values:
            values.append(value)

    bypass = ",".join(values)
    result["NO_PROXY"] = bypass
    result["no_proxy"] = bypass
    return result


def build_mineru_command(
    mineru_bin: Path,
    input_path: Path,
    output_root: Path,
    *,
    backend: str,
    effort: str,
    image_analysis: bool,
    method: str,
) -> list[str]:
    command = [
        str(mineru_bin),
        "-p",
        str(input_path),
        "-o",
        str(output_root),
        "-b",
        backend,
    ]
    if backend.startswith("hybrid-"):
        command.extend(["--effort", effort])
    if backend.startswith(("hybrid-", "vlm-")):
        command.extend(["--image-analysis", str(image_analysis).lower()])
    command.extend(["-m", method])
    return command


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Run MinerU for one lecture document and store the result under "
            "courses/<course_id>/lectures/."
        )
    )
    parser.add_argument("input", help="Path to a PDF, image, DOCX, PPTX, or XLSX file")
    parser.add_argument(
        "--course-id",
        help="Course directory name; defaults to COURSE_ID from .env",
    )
    parser.add_argument(
        "--mineru-bin",
        help="MinerU executable; defaults to MINERU_BIN, PATH, or a known local venv",
    )
    parser.add_argument(
        "--backend",
        choices=(
            "pipeline",
            "vlm-engine",
            "hybrid-engine",
            "vlm-http-client",
            "hybrid-http-client",
        ),
        default="hybrid-engine",
    )
    parser.add_argument("--effort", choices=("medium", "high"), default="medium")
    parser.add_argument(
        "--image-analysis",
        action=argparse.BooleanOptionalAction,
        default=False,
        help="Analyze image/chart semantics (default: disabled)",
    )
    parser.add_argument("--method", choices=("auto", "txt", "ocr"), default="auto")
    parser.add_argument(
        "--overwrite",
        action="store_true",
        help="Allow MinerU to write into an existing lecture result directory",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    load_dotenv(PROJECT_ROOT / ".env")
    args = parse_args(argv)

    try:
        course_id = (args.course_id or os.getenv("COURSE_ID", "default-course")).strip()
        course_directory = resolve_course_directory(PROJECT_ROOT, course_id)
        input_path = resolve_input_path(args.input)
        mineru_bin = find_mineru(args.mineru_bin, PROJECT_ROOT)
    except CoursePptError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2

    output_root = course_directory / "lectures"
    expected_lecture_directory = output_root / input_path.stem
    if expected_lecture_directory.exists() and not args.overwrite:
        print(
            f"Error: result directory already exists: {expected_lecture_directory}\n"
            "Use --overwrite to allow a rerun.",
            file=sys.stderr,
        )
        return 2

    output_root.mkdir(parents=True, exist_ok=True)
    command = build_mineru_command(
        mineru_bin,
        input_path,
        output_root,
        backend=args.backend,
        effort=args.effort,
        image_analysis=args.image_analysis,
        method=args.method,
    )

    print(f"Course: {course_id}")
    print(f"Input: {input_path}")
    print(f"Output root: {output_root}")
    print(
        f"MinerU: backend={args.backend}, effort={args.effort}, "
        f"image_analysis={str(args.image_analysis).lower()}, method={args.method}"
    )

    started = time.perf_counter()
    try:
        completed = subprocess.run(
            command,
            cwd=PROJECT_ROOT,
            env=add_local_proxy_bypass(dict(os.environ)),
            check=False,
        )
    except KeyboardInterrupt:
        print("\nInterrupted.", file=sys.stderr)
        return 130

    elapsed = time.perf_counter() - started
    if completed.returncode != 0:
        print(
            f"MinerU failed with exit code {completed.returncode} after {elapsed:.1f}s.",
            file=sys.stderr,
        )
        return completed.returncode

    if input_path.suffix.lower() == ".pdf":
        try:
            preprocessing = prepare_canonical_lecture_workspace(
                lecture_root=expected_lecture_directory,
                source_pdf=input_path,
                lecture_id=input_path.stem,
            )
        except CoursePptError as exc:
            print(f"Error: {exc}", file=sys.stderr)
            return 2
        print(
            "Preprocessed: "
            f"{preprocessing['visual_slides']} visual slides / "
            f"{preprocessing['visual_fragments']} visual fragments"
        )

    print(f"Completed in {elapsed:.1f}s")
    print(f"Result: {expected_lecture_directory}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
