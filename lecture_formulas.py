"""Read-only formula index from existing source evidence and knowledge nodes."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from mineru_normalizer import EQUATION_TYPES


def collect_lecture_formulas(root: Path, scaffold: dict[str, Any]) -> dict[str, Any]:
    source = root / "normalized/normalized_slides.json"
    normalized = json.loads(source.read_text(encoding="utf-8")) if source.is_file() else {}
    originals: list[dict[str, Any]] = []
    by_expression: dict[str, dict[str, Any]] = {}
    block_count = 0
    for slide in normalized.get("slides", []):
        page = slide.get("slide_number")
        if type(page) is not int or page < 1:
            continue
        for block in slide.get("blocks", []):
            if block.get("source_type") not in EQUATION_TYPES | {"equation_interline"}:
                continue
            raw = block.get("raw_content")
            raw = raw if isinstance(raw, dict) else {}
            expression = block.get("latex") or raw.get("math_content") or block.get("text")
            if not isinstance(expression, str) or not expression.strip():
                continue
            block_count += 1
            # Merge exact duplicates only. Never infer mathematical equivalence.
            key = expression.strip()
            row = by_expression.get(key)
            if row is None:
                row = {"id": f"source-{len(originals) + 1}", "expression": key, "pages": [], "block_refs": []}
                originals.append(row)
                by_expression[key] = row
            if page not in row["pages"]:
                row["pages"].append(page)
            row["block_refs"].append({"page": page, "block_id": block.get("block_id")})

    nodes = scaffold.get("nodes", {})
    formulas: list[dict[str, Any]] = []
    for node in sorted(nodes.values(), key=lambda item: item.get("order", 0)):
        if node.get("node_type") != "formula":
            continue
        content = node.get("content", {})
        expression = content.get("expression")
        if not isinstance(expression, str) or not expression.strip():
            continue
        pages = sorted({ref["page"] for ref in node.get("source_refs", []) if type(ref.get("page")) is int and ref["page"] > 0})
        parent = nodes.get(node.get("parent_id"), {})
        source_formulas = [row for row in originals if pages and pages[0] in row["pages"]]
        # Prose-compressed conditional formulas should show the original cases,
        # rather than attempting to invent a mathematical rewrite of the prose.
        cases = [row["expression"] for row in source_formulas if "\\left\\{" in row["expression"] or "\\begin{cases}" in row["expression"]]
        display = cases if "when" in expression.casefold() and cases else [expression]
        formulas.append({
            "id": node["node_id"], "title": node["title"],
            "group": parent.get("title") or "其他公式", "expression": expression,
            "display_expressions": display, "display_source": "slide" if display == cases and cases else "scaffold",
            "pages": pages, "variables": content.get("variables", []),
            "interpretation": content.get("interpretation"), "assumptions": content.get("assumptions", []),
            "source_formulas": source_formulas,
        })
    return {"formulas": formulas, "originals": originals, "source_block_count": block_count, "has_normalized_source": source.is_file()}
