"""Source-preserving lecture evidence shared by every course and model backend."""

from __future__ import annotations

import json
import re
import unicodedata
from html.parser import HTMLParser
from pathlib import Path
from typing import Any

import pymupdf


class _TableText(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.rows: list[list[str]] = []
        self.row: list[str] = []
        self.cell: list[str] | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "tr":
            self.row = []
        elif tag in {"td", "th"}:
            self.cell = []
            spans = [f"{key}={value}" for key, value in attrs
                     if key in {"rowspan", "colspan"} and value not in {None, "1"}]
            if spans:
                self.cell.append("[" + ", ".join(spans) + "] ")
        elif tag == "br" and self.cell is not None:
            self.cell.append("\n")

    def handle_data(self, data: str) -> None:
        if self.cell is not None:
            self.cell.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag in {"td", "th"} and self.cell is not None:
            self.row.append("".join(self.cell).strip())
            self.cell = None
        elif tag == "tr" and self.row:
            self.rows.append(self.row)
            self.row = []


def _source_value(value: Any) -> str:
    if value is None:
        return ""
    return value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)


def block_evidence_text(block: dict[str, Any]) -> str:
    """Keep tables, formulas and captions instead of consuming text fields alone."""
    parts = []
    for field in ("text", "table", "latex", "table_caption", "table_footnote",
                  "image_caption", "image_footnote"):
        value = _source_value(block.get(field)).strip()
        if not value:
            continue
        if field == "table" and re.search(r"<table\b|<tr\b", value, re.I):
            parser = _TableText()
            parser.feed(value)
            # Preserve the original when the evolving parser format is not a table.
            value = "\n".join("\t".join(row) for row in parser.rows) or value
        if value not in parts:
            parts.append(value)
    return "\n".join(parts)


def _comparison_key(value: str) -> str:
    # Matching tolerates PDF wrapping and bullets; emitted evidence is unchanged.
    return re.sub(r"[\s•●·]+", "", unicodedata.normalize("NFKC", value))


def supplement_native_pdf_text(slide: dict[str, Any], root: Path, text: str) -> str:
    """Repair detectable parser omissions from the same PDF page, without inference.

    A full native page is included when it contains meaningful missing text. This
    keeps opening/closing tags and neighbouring code lines together even if some
    tokens also appear in prose. Image-only pages still use the visual route.
    """
    source = root / "source/original.pdf"
    if not source.is_file():
        return text
    try:
        with pymupdf.open(source) as document:
            number = slide["slide_number"]
            if not 1 <= number <= len(document):
                return text
            blocks = document[number - 1].get_text("blocks")
            page_height = document[number - 1].rect.height
    except pymupdf.FileDataError:
        # Some legacy fixtures/materials have no readable native text layer.
        return text
    excluded = [block_evidence_text(block) for block in slide.get("blocks", [])
                if block.get("include_in_semantic_context") is False]
    excluded_keys = {_comparison_key(value) for value in excluded}
    excluded_keys.update(_comparison_key(line) for value in excluded for line in value.splitlines())
    native = []
    for block in blocks:
        if block[6] != 0:  # Native image descriptions are not lecture text.
            continue
        value = "\n".join(line for line in block[4].rstrip("\n").splitlines()
                          if _comparison_key(line) not in excluded_keys)
        key = _comparison_key(value)
        if not key or key in excluded_keys or (key == str(number) and block[1] > page_height * .85):
            continue
        native.append(value)
    known = _comparison_key(text)
    for value in native:
        for line in value.splitlines():
            key = _comparison_key(line)
            substantive = len(re.findall(r"\w", key)) >= 8
            code = bool(re.search(r"</?[A-Za-z][^>]*>|<![A-Za-z]|[{}]|\w\s*(?:=|:=|\+=)"
                                  r"|\b(?:def|class|return|SELECT|FROM|function|import)\b"
                                  r"|\b[A-Za-z_]\w*\([^)]*\)", line))
            if key and key not in known and (substantive or code):
                return text + f"\n\nPDF page {slide['slide_number']} native text (verbatim source supplement):\n" + "\n".join(native)
    return text
