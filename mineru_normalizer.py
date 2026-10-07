"""Deterministically normalize MinerU content_list_v2 JSON for PPT preparation."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import sys
import tempfile
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

import pymupdf

SCHEMA_VERSION = "1.0"
TEXT_CONTENT_KEYS = {
    "title": "title_content",
    "paragraph": "paragraph_content",
    "page_header": "page_header_content",
    "page_footer": "page_footer_content",
    "page_number": "page_number_content",
}
EXCLUDED_SEMANTIC_TYPES = {"page_footer", "page_number"}
IMAGE_TYPES = {"image"}
CHART_TYPES = {"chart"}
TABLE_TYPES = {"table"}
EQUATION_TYPES = {"equation", "formula", "interline_equation"}
SUPPORTED_SOURCE_TYPES = (
    set(TEXT_CONTENT_KEYS) | IMAGE_TYPES | CHART_TYPES | TABLE_TYPES | EQUATION_TYPES
)
VISUAL_SOURCE_TYPES = frozenset(IMAGE_TYPES | CHART_TYPES)
SLIDE_IMAGES_DIR = "runtime/slide_images"
DEFAULT_SLIDE_RENDER_DPI = 144
BLOCK_ID_PATTERN = re.compile(r"^s\d{3,}_b\d{3,}$")


class MinerUNormalizationError(ValueError):
    """Raised when the source cannot be safely normalized."""


def _is_sequence(value: object) -> bool:
    return isinstance(value, Sequence) and not isinstance(
        value, (str, bytes, bytearray)
    )


def _extract_text_fragments(value: object) -> str:
    """Join existing text fragments in source order without rewriting them."""

    fragments: list[str] = []

    def visit(item: object) -> None:
        if isinstance(item, str):
            if item:
                fragments.append(item)
            return
        if isinstance(item, Mapping):
            content = item.get("content")
            if isinstance(content, str):
                if content:
                    fragments.append(content)
            elif content is not None:
                visit(content)
            return
        if _is_sequence(item):
            for child in item:
                visit(child)

    visit(value)
    return "".join(fragments)


def _extract_fallback_text(value: object) -> str:
    """Best-effort text extraction for unknown or evolving MinerU content."""

    fragments: list[str] = []

    def visit(item: object) -> None:
        if isinstance(item, str):
            if item:
                fragments.append(item)
            return
        if _is_sequence(item):
            for child in item:
                visit(child)
            return
        if not isinstance(item, Mapping):
            return

        candidate_keys = [
            key
            for key in item
            if key in {"text", "content", "items", "children", "fragments"}
            or key.endswith("_content")
        ]
        for key in candidate_keys:
            visit(item[key])

    visit(value)
    return "".join(fragments)


def _extract_named_text(content: object, key: str) -> str:
    if not isinstance(content, Mapping):
        return _extract_text_fragments(content)
    return _extract_text_fragments(content.get(key, []))


def _extract_image_path(content: object) -> object | None:
    if not isinstance(content, Mapping):
        return None
    image_source = content.get("image_source")
    if isinstance(image_source, Mapping):
        return image_source.get("path")
    if image_source is not None:
        return image_source
    return content.get("image_path") or content.get("img_path")


def _copy_bbox(source_block: Mapping[str, Any], normalized: dict[str, Any]) -> None:
    if "bbox" in source_block:
        normalized["bbox"] = source_block["bbox"]


def _add_optional_text_field(
    normalized: dict[str, Any], content: object, source_key: str, output_key: str
) -> None:
    if not isinstance(content, Mapping):
        return
    text = _extract_text_fragments(content.get(source_key, []))
    if text:
        normalized[output_key] = text


def _normalize_block(
    source_block: object, *, slide_number: int, source_index: int
) -> dict[str, Any]:
    block_id = f"s{slide_number:03d}_b{source_index + 1:03d}"
    if not isinstance(source_block, Mapping):
        return {
            "block_id": block_id,
            "source_index": source_index,
            "source_type": "unknown",
            "content_type": "unknown",
            "raw_content": source_block,
            "include_in_semantic_context": True,
        }

    source_type_value = source_block.get("type")
    source_type = (
        source_type_value
        if isinstance(source_type_value, str) and source_type_value
        else "unknown"
    )
    content = source_block.get("content")
    normalized: dict[str, Any] = {
        "block_id": block_id,
        "source_index": source_index,
        "source_type": source_type,
        "content_type": "unknown",
        "include_in_semantic_context": source_type not in EXCLUDED_SEMANTIC_TYPES,
    }
    _copy_bbox(source_block, normalized)

    if source_type in TEXT_CONTENT_KEYS:
        normalized["content_type"] = "text"
        normalized["text"] = _extract_named_text(
            content, TEXT_CONTENT_KEYS[source_type]
        )
        return normalized

    if source_type in IMAGE_TYPES:
        normalized["content_type"] = "image"
        image_path = _extract_image_path(content)
        if image_path is not None:
            normalized["image_path"] = image_path
        _add_optional_text_field(normalized, content, "image_caption", "image_caption")
        _add_optional_text_field(
            normalized, content, "image_footnote", "image_footnote"
        )
        if isinstance(content, Mapping):
            existing_content = content.get("content")
            if isinstance(existing_content, str) and existing_content:
                normalized["text"] = existing_content
        return normalized

    if source_type in CHART_TYPES:
        normalized["content_type"] = "chart"
        image_path = _extract_image_path(content)
        if image_path is not None:
            normalized["image_path"] = image_path
        text = _extract_fallback_text(content)
        if text:
            normalized["text"] = text
        normalized["raw_content"] = content
        return normalized

    if source_type in TABLE_TYPES:
        normalized["content_type"] = "table"
        if isinstance(content, Mapping):
            table = content.get("html")
            if table is None:
                table = content.get("table_body")
            if table is not None:
                normalized["table"] = table
            image_path = _extract_image_path(content)
            if image_path is not None:
                normalized["image_path"] = image_path
        _add_optional_text_field(normalized, content, "table_caption", "table_caption")
        _add_optional_text_field(
            normalized, content, "table_footnote", "table_footnote"
        )
        return normalized

    if source_type in EQUATION_TYPES:
        normalized["content_type"] = "equation"
        if isinstance(content, Mapping):
            for key in ("latex", "equation", "formula"):
                if key in content:
                    normalized["latex"] = content[key]
                    break
            text = _extract_fallback_text(content)
        else:
            text = _extract_text_fragments(content)
        if text:
            normalized["text"] = text
        if "latex" not in normalized and "text" not in normalized:
            normalized["raw_content"] = content
        return normalized

    text = _extract_fallback_text(content)
    if text:
        normalized["text"] = text
    normalized["raw_content"] = content
    return normalized


def normalize_mineru_document(
    source_document: object, *, lecture_id: str, source_file: str
) -> dict[str, Any]:
    """Convert a MinerU v2 outer-page array to the canonical slide schema."""

    if not isinstance(lecture_id, str) or not lecture_id.strip():
        raise MinerUNormalizationError("lecture_id must be a non-empty string")
    if not isinstance(source_file, str) or not source_file:
        raise MinerUNormalizationError("source_file must be a non-empty string")
    if not _is_sequence(source_document):
        raise MinerUNormalizationError(
            "MinerU content_list_v2 top level must be an array of slide arrays"
        )

    slides: list[dict[str, Any]] = []
    for slide_index, source_slide in enumerate(source_document):
        slide_number = slide_index + 1
        if not _is_sequence(source_slide):
            raise MinerUNormalizationError(
                f"MinerU slide at outer index {slide_index} must be an array"
            )
        blocks = [
            _normalize_block(
                source_block,
                slide_number=slide_number,
                source_index=source_index,
            )
            for source_index, source_block in enumerate(source_slide)
        ]
        visual_blocks = [
            block
            for block in blocks
            if block.get("source_type") in VISUAL_SOURCE_TYPES
        ]
        if visual_blocks:
            visual_types = list(
                dict.fromkeys(block["source_type"] for block in visual_blocks)
            )
            vision: dict[str, Any] = {
                "required": True,
                "visual_block_ids": [block["block_id"] for block in visual_blocks],
                "visual_types": visual_types,
                # MinerU v2's outer list is the canonical, one-based PDF-page map.
                "pdf_page_number": slide_index + 1,
                "slide_image_path": (
                    f"{SLIDE_IMAGES_DIR}/slide_{slide_number:03d}.png"
                ),
            }
        else:
            vision = {"required": False}
        slides.append(
            {"slide_number": slide_number, "blocks": blocks, "vision": vision}
        )

    return {
        "schema_version": SCHEMA_VERSION,
        "lecture_id": lecture_id,
        "source": {
            "parser": "mineru",
            "source_file": source_file,
            "slide_count": len(slides),
        },
        "slides": slides,
    }


def _issue(
    code: str,
    message: str,
    *,
    slide_number: int | None = None,
    block_id: str | None = None,
) -> dict[str, Any]:
    issue: dict[str, Any] = {"code": code, "message": message}
    if slide_number is not None:
        issue["slide_number"] = slide_number
    if block_id is not None:
        issue["block_id"] = block_id
    return issue


def _valid_bbox(bbox: object) -> bool:
    if not _is_sequence(bbox) or len(bbox) != 4:
        return False
    if any(
        not isinstance(value, (int, float))
        or isinstance(value, bool)
        or not math.isfinite(value)
        for value in bbox
    ):
        return False
    x1, y1, x2, y2 = bbox
    return x2 >= x1 and y2 >= y1


def _resolve_image_path(images_dir: Path, image_path: str) -> Path:
    relative = Path(image_path)
    if relative.parts and relative.parts[0] == images_dir.name:
        relative = Path(*relative.parts[1:])
    return images_dir / relative


def validate_normalized_document(
    document: object,
    *,
    images_dir: Path | str | None = None,
    workspace_root: Path | str | None = None,
) -> dict[str, Any]:
    """Validate canonical structure, returning fatal errors and local warnings."""

    errors: list[dict[str, Any]] = []
    warnings: list[dict[str, Any]] = []
    stats = {
        "slides": 0,
        "blocks": 0,
        "included_blocks": 0,
        "excluded_blocks": 0,
        "images": 0,
        "tables": 0,
        "visual_slides": 0,
        "visual_fragments": 0,
    }

    try:
        json.dumps(document, ensure_ascii=False, allow_nan=False)
    except (TypeError, ValueError) as exc:
        errors.append(
            _issue("NOT_JSON_SERIALIZABLE", f"Document is not JSON serializable: {exc}")
        )

    if not isinstance(document, Mapping):
        errors.append(
            _issue("INVALID_TOP_LEVEL", "Normalized document must be an object")
        )
        return {
            "valid": False,
            "errors": errors,
            "warnings": warnings,
            "stats": stats,
        }

    if document.get("schema_version") != SCHEMA_VERSION:
        errors.append(
            _issue(
                "INVALID_SCHEMA_VERSION",
                f"schema_version must be {SCHEMA_VERSION!r}",
            )
        )
    if not isinstance(document.get("lecture_id"), str) or not document.get(
        "lecture_id"
    ):
        errors.append(_issue("INVALID_LECTURE_ID", "lecture_id must be non-empty"))

    source = document.get("source")
    slides = document.get("slides")
    if not isinstance(source, Mapping):
        errors.append(_issue("INVALID_SOURCE", "source must be an object"))
        source = {}
    else:
        if source.get("parser") != "mineru":
            errors.append(
                _issue("INVALID_SOURCE_PARSER", "source.parser must be 'mineru'")
            )
        if not isinstance(source.get("source_file"), str) or not source.get(
            "source_file"
        ):
            errors.append(
                _issue("INVALID_SOURCE_FILE", "source.source_file must be non-empty")
            )

    if not isinstance(slides, list):
        errors.append(_issue("INVALID_SLIDES", "slides must be a list"))
        return {
            "valid": False,
            "errors": errors,
            "warnings": warnings,
            "stats": stats,
        }

    stats["slides"] = len(slides)
    if source.get("slide_count") != len(slides):
        errors.append(
            _issue(
                "SLIDE_COUNT_MISMATCH",
                "source.slide_count must equal the number of slides",
            )
        )

    resolved_images_dir = (
        Path(images_dir).expanduser().resolve() if images_dir else None
    )
    resolved_workspace_root = (
        Path(workspace_root).expanduser().resolve() if workspace_root else None
    )
    seen_block_ids: set[str] = set()
    for slide_index, slide in enumerate(slides):
        expected_slide_number = slide_index + 1
        if not isinstance(slide, Mapping):
            errors.append(
                _issue(
                    "INVALID_SLIDE",
                    "Each slide must be an object",
                    slide_number=expected_slide_number,
                )
            )
            continue
        slide_number = slide.get("slide_number")
        if slide_number != expected_slide_number:
            errors.append(
                _issue(
                    "INVALID_SLIDE_NUMBER",
                    f"Expected slide_number {expected_slide_number}, got {slide_number!r}",
                    slide_number=expected_slide_number,
                )
            )
        blocks = slide.get("blocks")
        if not isinstance(blocks, list):
            errors.append(
                _issue(
                    "INVALID_BLOCKS",
                    "slide.blocks must be a list",
                    slide_number=expected_slide_number,
                )
            )
            continue

        vision = slide.get("vision")
        if not isinstance(vision, Mapping) or not isinstance(
            vision.get("required"), bool
        ):
            errors.append(
                _issue(
                    "INVALID_VISION_METADATA",
                    "Every slide requires boolean vision.required metadata",
                    slide_number=expected_slide_number,
                )
            )
            vision = {"required": False}
        expected_visual_blocks = [
            block
            for block in blocks
            if isinstance(block, Mapping)
            and block.get("source_type") in VISUAL_SOURCE_TYPES
        ]
        required = bool(expected_visual_blocks)
        if vision.get("required") is not required:
            errors.append(
                _issue(
                    "VISION_REQUIRED_MISMATCH",
                    "vision.required differs from deterministic block types",
                    slide_number=expected_slide_number,
                )
            )
        if required:
            stats["visual_slides"] += 1
            stats["visual_fragments"] += len(expected_visual_blocks)
            expected_ids = [block.get("block_id") for block in expected_visual_blocks]
            expected_types = list(
                dict.fromkeys(block.get("source_type") for block in expected_visual_blocks)
            )
            expected_path = f"{SLIDE_IMAGES_DIR}/slide_{expected_slide_number:03d}.png"
            if vision.get("visual_block_ids") != expected_ids:
                errors.append(
                    _issue(
                        "VISUAL_BLOCK_IDS_MISMATCH",
                        "visual_block_ids differ from visual blocks in source order",
                        slide_number=expected_slide_number,
                    )
                )
            if vision.get("visual_types") != expected_types:
                errors.append(
                    _issue(
                        "VISUAL_TYPES_MISMATCH",
                        "visual_types differ from visual blocks in source order",
                        slide_number=expected_slide_number,
                    )
                )
            if vision.get("pdf_page_number") != expected_slide_number:
                errors.append(
                    _issue(
                        "PDF_PAGE_MAPPING_MISMATCH",
                        "pdf_page_number must match the canonical MinerU outer index",
                        slide_number=expected_slide_number,
                    )
                )
            if vision.get("slide_image_path") != expected_path:
                errors.append(
                    _issue(
                        "SLIDE_IMAGE_PATH_MISMATCH",
                        f"slide_image_path must be {expected_path!r}",
                        slide_number=expected_slide_number,
                    )
                )
            digest = vision.get("slide_image_sha256")
            if digest is not None and (
                not isinstance(digest, str)
                or re.fullmatch(r"[0-9a-f]{64}", digest) is None
            ):
                errors.append(
                    _issue(
                        "INVALID_SLIDE_IMAGE_HASH",
                        "slide_image_sha256 must be a lowercase SHA-256 digest",
                        slide_number=expected_slide_number,
                    )
                )
            if resolved_workspace_root is not None and isinstance(digest, str):
                rendered = resolved_workspace_root / expected_path
                if not rendered.is_file():
                    errors.append(
                        _issue(
                            "SLIDE_IMAGE_MISSING",
                            f"Rendered slide image does not exist: {expected_path}",
                            slide_number=expected_slide_number,
                        )
                    )
                elif hashlib.sha256(rendered.read_bytes()).hexdigest() != digest:
                    errors.append(
                        _issue(
                            "SLIDE_IMAGE_HASH_MISMATCH",
                            f"Rendered slide image hash differs: {expected_path}",
                            slide_number=expected_slide_number,
                        )
                    )
        elif set(vision) != {"required"}:
            errors.append(
                _issue(
                    "UNEXPECTED_NO_VISUAL_METADATA",
                    "A non-visual slide may contain only vision.required=false",
                    slide_number=expected_slide_number,
                )
            )

        for block_position, block in enumerate(blocks):
            stats["blocks"] += 1
            if not isinstance(block, Mapping):
                errors.append(
                    _issue(
                        "INVALID_BLOCK",
                        "Each block must be an object",
                        slide_number=expected_slide_number,
                    )
                )
                continue

            block_id_value = block.get("block_id")
            block_id = block_id_value if isinstance(block_id_value, str) else None
            expected_block_id = (
                f"s{expected_slide_number:03d}_b{block_position + 1:03d}"
            )
            if block_id is None or not BLOCK_ID_PATTERN.fullmatch(block_id):
                errors.append(
                    _issue(
                        "INVALID_BLOCK_ID",
                        f"block_id must match {BLOCK_ID_PATTERN.pattern}",
                        slide_number=expected_slide_number,
                        block_id=block_id,
                    )
                )
            elif block_id != expected_block_id:
                errors.append(
                    _issue(
                        "BLOCK_ID_POSITION_MISMATCH",
                        f"Expected block_id {expected_block_id}, got {block_id}",
                        slide_number=expected_slide_number,
                        block_id=block_id,
                    )
                )
            if block_id in seen_block_ids:
                errors.append(
                    _issue(
                        "DUPLICATE_BLOCK_ID",
                        f"Duplicate block_id: {block_id}",
                        slide_number=expected_slide_number,
                        block_id=block_id,
                    )
                )
            elif block_id is not None:
                seen_block_ids.add(block_id)

            if block.get("source_index") != block_position:
                errors.append(
                    _issue(
                        "SOURCE_INDEX_MISMATCH",
                        f"Expected source_index {block_position}, got {block.get('source_index')!r}",
                        slide_number=expected_slide_number,
                        block_id=block_id,
                    )
                )
            source_type = block.get("source_type")
            if not isinstance(source_type, str) or not source_type:
                errors.append(
                    _issue(
                        "MISSING_SOURCE_TYPE",
                        "source_type must be a non-empty string",
                        slide_number=expected_slide_number,
                        block_id=block_id,
                    )
                )
            elif source_type not in SUPPORTED_SOURCE_TYPES:
                warnings.append(
                    _issue(
                        "UNSUPPORTED_SOURCE_TYPE",
                        f"Preserved unsupported MinerU source type: {source_type}",
                        slide_number=expected_slide_number,
                        block_id=block_id,
                    )
                )

            if not isinstance(block.get("content_type"), str) or not block.get(
                "content_type"
            ):
                errors.append(
                    _issue(
                        "MISSING_CONTENT_TYPE",
                        "content_type must be a non-empty string",
                        slide_number=expected_slide_number,
                        block_id=block_id,
                    )
                )

            included = block.get("include_in_semantic_context")
            if not isinstance(included, bool):
                errors.append(
                    _issue(
                        "INVALID_SEMANTIC_FLAG",
                        "include_in_semantic_context must be boolean",
                        slide_number=expected_slide_number,
                        block_id=block_id,
                    )
                )
            elif included:
                stats["included_blocks"] += 1
            else:
                stats["excluded_blocks"] += 1

            if "bbox" not in block:
                warnings.append(
                    _issue(
                        "BBOX_MISSING",
                        "Source block has no bbox",
                        slide_number=expected_slide_number,
                        block_id=block_id,
                    )
                )
            elif not _valid_bbox(block.get("bbox")):
                warnings.append(
                    _issue(
                        "BBOX_INVALID",
                        "bbox must contain four finite numbers with x2 >= x1 and y2 >= y1",
                        slide_number=expected_slide_number,
                        block_id=block_id,
                    )
                )

            content_type = block.get("content_type")
            if content_type == "image":
                stats["images"] += 1
            elif content_type == "table":
                stats["tables"] += 1

            if "image_path" in block or content_type == "image":
                image_path = block.get("image_path")
                if not isinstance(image_path, str) or not image_path.strip():
                    warnings.append(
                        _issue(
                            "IMAGE_PATH_INVALID",
                            "Image block has no valid image_path",
                            slide_number=expected_slide_number,
                            block_id=block_id,
                        )
                    )
                elif resolved_images_dir is not None:
                    resolved_image = _resolve_image_path(
                        resolved_images_dir, image_path
                    )
                    if not resolved_image.is_file():
                        warnings.append(
                            _issue(
                                "IMAGE_NOT_FOUND",
                                f"Referenced image does not exist: {resolved_image}",
                                slide_number=expected_slide_number,
                                block_id=block_id,
                            )
                        )

    return {
        "valid": not errors,
        "errors": errors,
        "warnings": warnings,
        "stats": stats,
    }


PageRenderer = Callable[[Path, int, Path, int], None]


def _pdf_page_count(pdf_path: Path) -> int:
    try:
        with pymupdf.open(str(pdf_path)) as document:
            page_count = document.page_count
    except (OSError, RuntimeError, ValueError) as exc:
        raise MinerUNormalizationError(f"Unable to inspect source PDF: {exc}") from exc
    if page_count <= 0:
        raise MinerUNormalizationError("Source PDF contains no pages")
    return page_count


def _render_pdf_page(
    pdf_path: Path, page_number: int, output_path: Path, dpi: int
) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(
        dir=output_path.parent, prefix=".slide-render-"
    ) as temporary_directory:
        rendered = Path(temporary_directory) / "page.png"
        try:
            with pymupdf.open(str(pdf_path)) as document:
                page = document.load_page(page_number - 1)
                pixmap = page.get_pixmap(dpi=dpi, alpha=False)
                pixmap.save(str(rendered))
        except (OSError, RuntimeError, ValueError) as exc:
            raise MinerUNormalizationError(
                f"Unable to render PDF page {page_number}: {exc}"
            ) from exc
        if not rendered.is_file() or not rendered.read_bytes().startswith(b"\x89PNG"):
            raise MinerUNormalizationError(
                f"Renderer did not produce a valid PNG for page {page_number}"
            )
        rendered.replace(output_path)


def render_visual_slides(
    document: dict[str, Any],
    *,
    source_pdf: Path | str,
    workspace_root: Path | str,
    dpi: int = DEFAULT_SLIDE_RENDER_DPI,
    page_count: int | None = None,
    renderer: PageRenderer | None = None,
) -> dict[str, Any]:
    """Render exactly one full PDF page for every deterministic visual slide."""

    if dpi <= 0:
        raise MinerUNormalizationError("Render DPI must be positive")
    pdf_path = Path(source_pdf).expanduser().resolve()
    root = Path(workspace_root).expanduser().resolve()
    if not pdf_path.is_file():
        raise MinerUNormalizationError(f"Source PDF does not exist: {pdf_path}")
    slides = document.get("slides")
    if not isinstance(slides, list):
        raise MinerUNormalizationError("Normalized document lacks slides")
    actual_page_count = page_count if page_count is not None else _pdf_page_count(pdf_path)
    if actual_page_count != len(slides):
        raise MinerUNormalizationError(
            "PDF page count differs from canonical MinerU slide count: "
            f"{actual_page_count} != {len(slides)}"
        )

    render_page = renderer or _render_pdf_page
    for slide in slides:
        vision = slide.get("vision") if isinstance(slide, Mapping) else None
        if not isinstance(vision, dict) or vision.get("required") is not True:
            continue
        page_number = vision.get("pdf_page_number")
        slide_number = slide.get("slide_number")
        if page_number != slide_number or not isinstance(page_number, int):
            raise MinerUNormalizationError(
                f"Invalid page/slide mapping for slide {slide_number!r}"
            )
        relative_path = vision.get("slide_image_path")
        if not isinstance(relative_path, str):
            raise MinerUNormalizationError(
                f"Visual slide {slide_number} lacks slide_image_path"
            )
        target = (root / relative_path).resolve()
        try:
            target.relative_to(root)
        except ValueError as exc:
            raise MinerUNormalizationError(
                f"Unsafe slide image path: {relative_path!r}"
            ) from exc
        target.parent.mkdir(parents=True, exist_ok=True)
        render_page(pdf_path, page_number, target, dpi)
        data = target.read_bytes()
        if not data.startswith(b"\x89PNG"):
            raise MinerUNormalizationError(
                f"Rendered slide is not a PNG: {relative_path}"
            )
        vision["slide_image_sha256"] = hashlib.sha256(data).hexdigest()

    validation = validate_normalized_document(document, workspace_root=root)
    if not validation["valid"]:
        codes = ", ".join(issue["code"] for issue in validation["errors"])
        raise MinerUNormalizationError(
            f"Rendered normalized document failed validation: {codes}"
        )
    return validation


def load_and_normalize_mineru_file(
    input_path: Path | str, *, lecture_id: str
) -> dict[str, Any]:
    path = Path(input_path).expanduser().resolve()
    try:
        with path.open(encoding="utf-8") as source_handle:
            source_document = json.load(source_handle)
    except FileNotFoundError as exc:
        raise MinerUNormalizationError(f"Input file does not exist: {path}") from exc
    except json.JSONDecodeError as exc:
        raise MinerUNormalizationError(
            f"Input is not valid JSON at line {exc.lineno}, column {exc.colno}: {exc.msg}"
        ) from exc

    return normalize_mineru_document(
        source_document,
        lecture_id=lecture_id,
        source_file=path.name,
    )


def write_normalized_document_atomic(
    document: object,
    output_path: Path | str,
    *,
    images_dir: Path | str | None = None,
    workspace_root: Path | str | None = None,
) -> dict[str, Any]:
    """Validate and atomically replace the normalized output file."""

    validation = validate_normalized_document(
        document,
        images_dir=images_dir,
        workspace_root=workspace_root,
    )
    if not validation["valid"]:
        codes = ", ".join(issue["code"] for issue in validation["errors"])
        raise MinerUNormalizationError(
            f"Normalized document failed validation; output not written: {codes}"
        )

    output = Path(output_path).expanduser().resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=output.parent,
            prefix=f".{output.name}.",
            suffix=".tmp",
            delete=False,
        ) as temporary_handle:
            temporary_path = Path(temporary_handle.name)
            json.dump(
                document,
                temporary_handle,
                ensure_ascii=False,
                indent=2,
                allow_nan=False,
            )
            temporary_handle.write("\n")
            temporary_handle.flush()
            os.fsync(temporary_handle.fileno())
        temporary_path.replace(output)
    finally:
        if temporary_path is not None and temporary_path.exists():
            temporary_path.unlink()
    return validation


def _default_lecture_id(input_path: Path) -> str:
    suffix = "_content_list_v2"
    return input_path.stem.removesuffix(suffix) or input_path.stem


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Normalize MinerU content_list_v2 JSON without semantic inference."
    )
    parser.add_argument("--input", required=True, help="MinerU content_list_v2 JSON")
    parser.add_argument(
        "--output",
        help="Output path; defaults to normalized_slides.json beside the input",
    )
    parser.add_argument(
        "--source-pdf",
        help="Original PDF; when set, render only visual slides before writing",
    )
    parser.add_argument(
        "--workspace-root",
        help="Lecture workspace root used by runtime/slide_images paths",
    )
    parser.add_argument(
        "--lecture-id",
        help="Stable lecture ID; defaults to the input filename prefix",
    )
    parser.add_argument(
        "--images-dir",
        help="Optional images directory used only to validate referenced files",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    input_path = Path(args.input).expanduser().resolve()
    output_path = (
        Path(args.output).expanduser().resolve()
        if args.output
        else input_path.parent / "normalized_slides.json"
    )
    lecture_id = args.lecture_id or _default_lecture_id(input_path)

    try:
        document = load_and_normalize_mineru_file(input_path, lecture_id=lecture_id)
        if args.source_pdf:
            if not args.workspace_root:
                raise MinerUNormalizationError(
                    "--workspace-root is required with --source-pdf"
                )
            render_visual_slides(
                document,
                source_pdf=args.source_pdf,
                workspace_root=args.workspace_root,
            )
        validation = write_normalized_document_atomic(
            document,
            output_path,
            images_dir=args.images_dir,
            workspace_root=args.workspace_root,
        )
    except MinerUNormalizationError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2

    print(json.dumps(validation, ensure_ascii=False, indent=2))
    print(f"Output: {output_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
