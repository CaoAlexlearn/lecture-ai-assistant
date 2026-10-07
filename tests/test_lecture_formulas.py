from __future__ import annotations

import copy
import json
from pathlib import Path

from fastapi.testclient import TestClient

from classroom_app import create_app
from config import Settings
from lecture_formulas import collect_lecture_formulas
from workspace_catalog import WorkspaceCatalog

CASES = r"F = \begin{cases} A_1\frac{(1+i)^N-(1+f)^N}{i-f}, & f\ne i \\ A_1 N(1+i)^{N-1}, & f=i \end{cases}"


def scaffold() -> dict:
    return {"nodes": {
        "parent": {"node_id": "parent", "title": "Geometric gradient", "node_type": "topic", "order": 0},
        "formula": {
            "node_id": "formula", "title": "Future worth", "node_type": "formula", "order": 1,
            "parent_id": "parent", "source_refs": [{"page": 51}, {"page": 52}],
            "content": {"expression": "F = A_1[(1+i)^N-(1+f)^N]/(i-f) when f!=i",
                        "variables": [{"symbol": "i", "meaning": "interest rate"}],
                        "interpretation": "Future value of the gradient", "assumptions": ["End-of-period payments"]},
        },
    }}


def write_source(root: Path, slides: list) -> Path:
    source = root / "normalized/normalized_slides.json"
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_text(json.dumps({"slides": slides}), encoding="utf-8")
    return source


def test_original_cases_and_all_provenance_preserved_without_writes(tmp_path: Path) -> None:
    source = write_source(tmp_path, [
        {"slide_number": 51, "blocks": [
            {"block_id": "s051_b001", "source_type": "equation_interline", "raw_content": {"math_content": CASES}},
            {"block_id": "s051_b002", "source_type": "formula", "latex": CASES},
            {"block_id": "s051_b003", "source_type": "paragraph", "text": "Not an equation"},
        ]},
        {"slide_number": 52, "blocks": [{"block_id": "s052_b001", "source_type": "equation", "text": CASES}]},
    ])
    original = source.read_bytes()
    knowledge = scaffold()
    before = copy.deepcopy(knowledge)
    result = collect_lecture_formulas(tmp_path, knowledge)
    assert result["source_block_count"] == 3
    assert len(result["originals"]) == 1
    row = result["originals"][0]
    assert row["expression"] == CASES
    assert row["pages"] == [51, 52]
    assert [ref["block_id"] for ref in row["block_refs"]] == ["s051_b001", "s051_b002", "s052_b001"]
    formula = result["formulas"][0]
    assert formula["display_expressions"] == [CASES]
    assert formula["display_source"] == "slide"
    assert formula["expression"] == knowledge["nodes"]["formula"]["content"]["expression"]
    assert formula["group"] == "Geometric gradient"
    assert formula["variables"][0]["symbol"] == "i"
    assert source.read_bytes() == original
    assert knowledge == before
    assert sorted(path.relative_to(tmp_path).as_posix() for path in tmp_path.rglob("*") if path.is_file()) == ["normalized/normalized_slides.json"]


def test_missing_source_retains_existing_knowledge_formula(tmp_path: Path) -> None:
    result = collect_lecture_formulas(tmp_path, scaffold())
    assert not result["has_normalized_source"]
    assert result["originals"] == []
    assert result["formulas"][0]["display_source"] == "scaffold"
    assert result["formulas"][0]["source_formulas"] == []


def test_exact_only_dedup_and_canonical_page_not_slide_id(tmp_path: Path) -> None:
    write_source(tmp_path, [
        {"slide_id": 900, "slide_number": 7, "blocks": [
            {"block_id": "a", "source_type": "interline_equation", "text": "F=P(1+i)^N", "raw_content": "legacy"},
            {"block_id": "b", "source_type": "equation_interline", "text": "F = P (1 + i)^N"},
            {"block_id": "c", "source_type": "equation", "text": ""},
        ]},
        {"slide_number": True, "blocks": [{"source_type": "equation", "text": "bad"}]},
    ])
    result = collect_lecture_formulas(tmp_path, {"nodes": {}})
    assert len(result["originals"]) == 2
    assert result["originals"][0]["pages"] == [7]
    assert result["source_block_count"] == 2


def test_formula_api_ready_read_only_and_unprepared_guard(tmp_path: Path) -> None:
    (tmp_path / "courses").mkdir()
    (tmp_path / "courses/index.json").write_text('{"schema_version":1,"course_ids":[]}', encoding="utf-8")
    catalog = WorkspaceCatalog(tmp_path)
    course = catalog.create_course(title="Economics")
    lecture = catalog.create_lecture(course["id"], number="03", title="Time Value of Money")
    settings = Settings(api_key="test-key", workspace_id=None, send_workspace_header=False,
                        source_language="en", target_language="zh", debug=False, raw_event_debug=False,
                        agent1_enabled=False, agent1_output_path=tmp_path / "data")
    client = TestClient(create_app(settings, workspace_catalog=catalog))
    url = f"/api/courses/{course['id']}/lectures/{lecture['id']}/formulas"
    assert client.get(url).status_code == 400
    catalog.upload_material(course["id"], lecture["id"], filename="lecture.pdf", content=b"%PDF")
    root = catalog.lecture_root(course["id"], lecture["id"])
    prepared = root / "prepared"
    prepared.mkdir()
    for filename, content in {"lecture_scaffold.json": json.dumps(scaffold()), "lecture_scaffold.md": "# Lecture",
                              "asr_context.txt": "Economics", "hotwords.json": "[]"}.items():
        (prepared / filename).write_text(content, encoding="utf-8")
    write_source(root, [{"slide_number": 51, "blocks": [{"block_id": "eq", "source_type": "equation_interline", "text": CASES}]}])
    catalog.mark_prepared(course["id"], lecture["id"])
    before = {path: path.read_bytes() for path in root.rglob("*") if path.is_file()}
    response = client.get(url)
    assert response.status_code == 200
    assert response.json()["formulas"][0]["display_expressions"] == [CASES]
    assert {path: path.read_bytes() for path in root.rglob("*") if path.is_file()} == before

