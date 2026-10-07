from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import pymupdf

from mineru_normalizer import (
    MinerUNormalizationError,
    normalize_mineru_document,
    render_visual_slides,
    validate_normalized_document,
    write_normalized_document_atomic,
)


def text_block(source_type: str, fragments: list[str], bbox: object = None) -> dict:
    key = {
        "title": "title_content",
        "paragraph": "paragraph_content",
        "page_header": "page_header_content",
        "page_footer": "page_footer_content",
        "page_number": "page_number_content",
    }[source_type]
    block = {
        "type": source_type,
        "content": {key: [{"type": "text", "content": part} for part in fragments]},
    }
    if bbox is not None:
        block["bbox"] = bbox
    return block


class MinerUNormalizerTests(unittest.TestCase):
    def normalize(self, source: object) -> dict:
        return normalize_mineru_document(
            source,
            lecture_id="lecture-1",
            source_file="lecture_content_list_v2.json",
        )

    def test_normalizes_title_and_paragraph(self) -> None:
        result = self.normalize(
            [
                [
                    text_block("title", ["Title"], [0, 0, 10, 10]),
                    text_block("paragraph", ["Body"], [0, 11, 10, 20]),
                ]
            ]
        )

        blocks = result["slides"][0]["blocks"]
        self.assertEqual(blocks[0]["text"], "Title")
        self.assertEqual(blocks[0]["content_type"], "text")
        self.assertEqual(blocks[1]["text"], "Body")

    def test_page_header_remains_page_header(self) -> None:
        result = self.normalize(
            [[text_block("page_header", ["What is Economics"], [0, 0, 10, 10])]]
        )
        block = result["slides"][0]["blocks"][0]
        self.assertEqual(block["source_type"], "page_header")
        self.assertEqual(block["content_type"], "text")

    def test_footer_and_page_number_are_preserved_but_excluded(self) -> None:
        result = self.normalize(
            [
                [
                    text_block("page_footer", ["COURSE101"], [0, 0, 10, 10]),
                    text_block("page_number", ["1"], [11, 0, 12, 10]),
                ]
            ]
        )
        footer, page_number = result["slides"][0]["blocks"]
        self.assertEqual(footer["text"], "COURSE101")
        self.assertFalse(footer["include_in_semantic_context"])
        self.assertEqual(page_number["text"], "1")
        self.assertFalse(page_number["include_in_semantic_context"])

    def test_extracts_image_path(self) -> None:
        source = [
            [
                {
                    "type": "image",
                    "content": {
                        "image_source": {"path": "images/a.jpg"},
                        "content": "",
                        "image_caption": [],
                        "image_footnote": [],
                    },
                    "bbox": [1, 2, 3, 4],
                }
            ]
        ]
        block = self.normalize(source)["slides"][0]["blocks"][0]
        self.assertEqual(block["content_type"], "image")
        self.assertEqual(block["image_path"], "images/a.jpg")

    def test_visual_detection_is_deterministic_and_narrow(self) -> None:
        source = [
            [text_block("paragraph", ["text"], [0, 0, 1, 1])],
            [{"type": "image", "content": {}, "bbox": [0, 0, 1, 1]}],
            [{"type": "chart", "content": {}, "bbox": [0, 0, 1, 1]}],
            [
                {"type": "equation_interline", "content": {}},
                {"type": "code", "content": {}},
            ],
        ]
        slides = self.normalize(source)["slides"]
        self.assertEqual([slide["vision"]["required"] for slide in slides], [False, True, True, False])
        self.assertEqual(slides[1]["vision"]["visual_types"], ["image"])
        self.assertEqual(slides[2]["vision"]["visual_types"], ["chart"])

    def test_many_fragments_remain_one_slide_vision_task(self) -> None:
        source = [
            [
                {"type": "image", "content": {"image_path": f"images/{index}.png"}}
                for index in range(36)
            ]
        ]
        slide = self.normalize(source)["slides"][0]
        self.assertTrue(slide["vision"]["required"])
        self.assertEqual(len(slide["vision"]["visual_block_ids"]), 36)
        self.assertEqual(slide["vision"]["slide_image_path"], "runtime/slide_images/slide_001.png")

    def test_same_slide_multiple_visual_types_is_one_render_task(self) -> None:
        slide = self.normalize(
            [[{"type": "image", "content": {}}, {"type": "chart", "content": {}}]]
        )["slides"][0]
        self.assertEqual(slide["vision"]["visual_types"], ["image", "chart"])
        self.assertEqual(slide["vision"]["pdf_page_number"], 1)

    def test_render_visual_slides_uses_explicit_one_based_mapping_once(self) -> None:
        document = self.normalize(
            [
                [text_block("paragraph", ["plain"], [0, 0, 1, 1])],
                [{"type": "image", "content": {}}],
                [{"type": "chart", "content": {}}],
            ]
        )
        calls: list[tuple[int, int]] = []

        def fake_renderer(_pdf: Path, page: int, target: Path, dpi: int) -> None:
            calls.append((page, dpi))
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(b"\x89PNG\r\n\x1a\n" + str(page).encode())

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            pdf = root / "source" / "original.pdf"
            pdf.parent.mkdir(parents=True)
            pdf.write_bytes(b"fake-pdf")
            validation = render_visual_slides(
                document,
                source_pdf=pdf,
                workspace_root=root,
                page_count=3,
                renderer=fake_renderer,
            )
            self.assertTrue(validation["valid"])
            self.assertFalse((root / "runtime/slide_images/slide_001.png").exists())
            self.assertTrue((root / "runtime/slide_images/slide_002.png").is_file())
            self.assertTrue((root / "runtime/slide_images/slide_003.png").is_file())
        self.assertEqual(calls, [(2, 144), (3, 144)])

    def test_default_pdf_backend_does_not_require_poppler_commands(self) -> None:
        document = self.normalize(
            [
                [text_block("paragraph", ["plain"], [0, 0, 1, 1])],
                [{"type": "image", "content": {}}],
            ]
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            pdf = root / "source" / "original.pdf"
            pdf.parent.mkdir(parents=True)
            with pymupdf.open() as source:
                source.new_page().insert_text((72, 72), "page one")
                source.new_page().insert_text((72, 72), "page two")
                source.save(str(pdf))

            with patch.dict("os.environ", {"PATH": ""}, clear=False):
                validation = render_visual_slides(
                    document,
                    source_pdf=pdf,
                    workspace_root=root,
                )

            rendered = root / "runtime" / "slide_images" / "slide_002.png"
            self.assertTrue(validation["valid"])
            self.assertFalse(
                (root / "runtime" / "slide_images" / "slide_001.png").exists()
            )
            self.assertTrue(rendered.read_bytes().startswith(b"\x89PNG"))

    def test_invalid_pdf_preserves_bounded_inspection_error(self) -> None:
        document = self.normalize([[{"type": "image", "content": {}}]])
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            pdf = root / "original.pdf"
            pdf.write_bytes(b"not-a-pdf")
            with self.assertRaisesRegex(
                MinerUNormalizationError, "Unable to inspect source PDF"
            ):
                render_visual_slides(
                    document,
                    source_pdf=pdf,
                    workspace_root=root,
                )

    def test_render_rejects_page_count_or_mapping_mismatch(self) -> None:
        document = self.normalize([[{"type": "image", "content": {}}]])
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            pdf = root / "original.pdf"
            pdf.write_bytes(b"fake")
            with self.assertRaisesRegex(MinerUNormalizationError, "page count"):
                render_visual_slides(
                    document,
                    source_pdf=pdf,
                    workspace_root=root,
                    page_count=2,
                    renderer=lambda *_: None,
                )
            document["slides"][0]["vision"]["pdf_page_number"] = 2
            with self.assertRaisesRegex(MinerUNormalizationError, "page/slide"):
                render_visual_slides(
                    document,
                    source_pdf=pdf,
                    workspace_root=root,
                    page_count=1,
                    renderer=lambda *_: None,
                )

    def test_missing_image_is_warning_not_error(self) -> None:
        source = [
            [
                {
                    "type": "image",
                    "content": {"image_source": {"path": "images/missing.jpg"}},
                    "bbox": [1, 2, 3, 4],
                }
            ]
        ]
        result = self.normalize(source)
        with tempfile.TemporaryDirectory() as directory:
            report = validate_normalized_document(result, images_dir=directory)
        self.assertTrue(report["valid"])
        self.assertEqual(
            [warning["code"] for warning in report["warnings"]], ["IMAGE_NOT_FOUND"]
        )

    def test_preserves_table_html(self) -> None:
        html = "<table><tr><td>42</td></tr></table>"
        source = [[{"type": "table", "content": {"html": html}, "bbox": [1, 2, 3, 4]}]]
        block = self.normalize(source)["slides"][0]["blocks"][0]
        self.assertEqual(block["content_type"], "table")
        self.assertEqual(block["table"], html)

    def test_joins_multiple_text_fragments_in_source_order(self) -> None:
        source = [
            [
                text_block(
                    "paragraph", ["first ", "second", "", " third"], [0, 0, 10, 10]
                )
            ]
        ]
        block = self.normalize(source)["slides"][0]["blocks"][0]
        self.assertEqual(block["text"], "first second third")

    def test_unknown_block_is_preserved_and_warned(self) -> None:
        raw_content = {"items": [{"type": "text", "content": "alpha"}], "value": 7}
        result = self.normalize(
            [[{"type": "future_block", "content": raw_content, "bbox": [0, 0, 1, 1]}]]
        )
        block = result["slides"][0]["blocks"][0]
        self.assertEqual(block["source_type"], "future_block")
        self.assertEqual(block["raw_content"], raw_content)
        self.assertEqual(block["text"], "alpha")
        report = validate_normalized_document(result)
        self.assertTrue(report["valid"])
        self.assertEqual(report["warnings"][0]["code"], "UNSUPPORTED_SOURCE_TYPE")

    def test_block_ids_are_stable_and_follow_source_order(self) -> None:
        source = [
            [text_block("paragraph", ["A"], [0, 0, 1, 1])],
            [
                text_block("paragraph", ["B"], [0, 0, 1, 1]),
                text_block("paragraph", ["C"], [0, 1, 1, 2]),
            ],
        ]
        result = self.normalize(source)
        self.assertEqual(result["slides"][0]["blocks"][0]["block_id"], "s001_b001")
        self.assertEqual(result["slides"][1]["blocks"][1]["block_id"], "s002_b002")
        self.assertEqual(result["slides"][1]["blocks"][1]["source_index"], 1)

    def test_slide_numbers_are_continuous(self) -> None:
        result = self.normalize([[], [], []])
        self.assertEqual(
            [slide["slide_number"] for slide in result["slides"]], [1, 2, 3]
        )
        self.assertTrue(validate_normalized_document(result)["valid"])

    def test_repeated_normalization_is_deterministic(self) -> None:
        source = [[text_block("paragraph", ["diferent 20%"], [0, 0, 1, 1])]]
        first = self.normalize(source)
        second = self.normalize(source)
        self.assertEqual(first, second)
        self.assertEqual(first["slides"][0]["blocks"][0]["text"], "diferent 20%")

    def test_invalid_top_level_is_fatal(self) -> None:
        with self.assertRaisesRegex(
            MinerUNormalizationError, "top level must be an array"
        ):
            self.normalize({"slides": []})

        report = validate_normalized_document([])
        self.assertFalse(report["valid"])
        self.assertEqual(report["errors"][0]["code"], "INVALID_TOP_LEVEL")

    def test_bbox_problem_is_warning(self) -> None:
        result = self.normalize([[text_block("paragraph", ["text"], [3, 2, 1, 4])]])
        report = validate_normalized_document(result)
        self.assertTrue(report["valid"])
        self.assertEqual(report["warnings"][0]["code"], "BBOX_INVALID")

    def test_equation_preserves_existing_latex(self) -> None:
        source = [
            [
                {
                    "type": "interline_equation",
                    "content": {"latex": "x^2 + y^2 = 1"},
                    "bbox": [0, 0, 10, 10],
                }
            ]
        ]
        block = self.normalize(source)["slides"][0]["blocks"][0]
        self.assertEqual(block["source_type"], "interline_equation")
        self.assertEqual(block["content_type"], "equation")
        self.assertEqual(block["latex"], "x^2 + y^2 = 1")

    def test_atomic_writer_does_not_replace_output_after_fatal_error(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "normalized_slides.json"
            output.write_text("previous", encoding="utf-8")
            with self.assertRaises(MinerUNormalizationError):
                write_normalized_document_atomic({"slides": []}, output)
            self.assertEqual(output.read_text(encoding="utf-8"), "previous")

    def test_atomic_writer_creates_valid_json(self) -> None:
        document = self.normalize([[]])
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "normalized_slides.json"
            report = write_normalized_document_atomic(document, output)
            self.assertTrue(report["valid"])
            self.assertEqual(json.loads(output.read_text(encoding="utf-8")), document)


if __name__ == "__main__":
    unittest.main()
