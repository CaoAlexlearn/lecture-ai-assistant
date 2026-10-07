"""Course-independent source coverage and backend transport regressions."""

import asyncio
import json
from copy import deepcopy
from types import SimpleNamespace

import pymupdf
import pytest

from knowledge_scaffold import derive_hotwords, compact_worker_evidence
from preagent_scaffold import QwenScaffoldModel, _slide_evidence
from slide_evidence import block_evidence_text


def source_page(root, text):
    (root / "source").mkdir(exist_ok=True)
    with pymupdf.open() as doc:
        page = doc.new_page()
        page.insert_text((50, 50), text)
        doc.save(root / "source/original.pdf")


@pytest.mark.parametrize("example", [
    '<html>\n<head>\n<title>Example</title>\n</head>\n<body>\n<p>Learning</p>\n</body>\n</html>',
    'def probability(x):\n    return x / 100',
    'def f():\n    return 1',
    'NPV = -1000 + 1100 / (1 + r)\nr = 0.05',
])
def test_native_source_repairs_omissions_across_subjects(tmp_path, example):
    source_page(tmp_path, "Lecture example\n" + example)
    slide = {"slide_number": 1, "blocks": [{"text": "Lecture example\nThe body contains <body>.",
              "include_in_semantic_context": True}], "vision": {"required": False}}
    original = deepcopy(slide)
    text = _slide_evidence(slide, tmp_path)["text"]
    for line in example.splitlines():
        assert line in text
    assert "PDF page 1 native text" in text
    assert slide == original  # No source/fingerprint mutation during reading.


def test_complete_native_text_is_not_duplicated_and_footer_stays_excluded(tmp_path):
    source_page(tmp_path, "Risk-adjusted return\nExpected return = 10 percent\nLecture footer")
    slide = {"slide_number": 1, "blocks": [
        {"text": "Risk-adjusted return\nExpected return = 10 percent", "include_in_semantic_context": True},
        {"text": "Lecture footer", "include_in_semantic_context": False},
    ]}
    text = _slide_evidence(slide, tmp_path)["text"]
    assert "native text" not in text
    assert "Lecture footer" not in text


def test_table_equation_and_caption_survive_semantic_serialization():
    block = {"table": '<table><tr><th>Selector</th><th>Meaning</th></tr>'
             '<tr><td>p &gt; a</td><td>Direct child &amp; link</td></tr>'
             '<tr><td colspan="2">a:hover</td></tr></table>',
             "table_caption": "Selector conditions"}
    text = block_evidence_text(block)
    assert "Selector\tMeaning" in text
    assert "p > a\tDirect child & link" in text
    assert "[colspan=2] a:hover" in text
    assert "Selector conditions" in text
    assert block_evidence_text({"latex": r"PV=\frac{FV}{(1+r)^n}"}) == r"PV=\frac{FV}{(1+r)^n}"


def test_long_code_and_case_sensitive_examples_are_not_silently_truncated():
    code = '<html>\n' + '<p>Course example</p>\n' * 60 + '</html>'
    rows = compact_worker_evidence([{"kind": "slide_example", "label": None, "text": code}])
    assert ''.join(row['text'] for row in rows) == code
    assert all(len(row['text']) <= 700 for row in rows)
    variants = compact_worker_evidence(['UserID = 1', 'userid = 1'])
    assert len(variants) == 2


@pytest.mark.parametrize("backend", ["codex", "qwen"])
def test_both_backends_receive_code_tables_and_equations(tmp_path, monkeypatch, backend):
    source_page(tmp_path, 'Example\n<p>Course example</p>')
    slide = {"slide_number": 1, "blocks": [
        {"text": "Example"},
        {"table": '<table><tr><td>a:hover</td><td>Pointer over link</td></tr></table>'},
        {"latex": r"PV=FV/(1+r)^n"},
    ]}
    requests = []
    class Client:
        def __init__(self, *, reasoning_effort=None):
            self.reasoning_effort = reasoning_effort
        model_name = "test-backend"
        async def create_chat_completion(self, **request):
            requests.append(request)
            return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content='{"ok":true}'))])
    if backend == "codex":
        import codex_scaffold
        monkeypatch.setattr(codex_scaffold, "CodexChatClient", Client)
        model = codex_scaffold.CodexScaffoldModel(workspace_root=tmp_path)
    else:
        model = QwenScaffoldModel(client=Client(), workspace_root=tmp_path)
    evidence = _slide_evidence(slide, tmp_path)
    node = {"node_id": "KN_0001", "title": "Course example", "node_type": "concept", "scope": "Source examples"}
    asyncio.run(model.generate("enriching_nodes", {"node": node, "slides": [evidence]}))
    system = requests[0]["messages"][0]["content"]
    user = json.loads(requests[0]["messages"][1]["content"])
    assert '<p>Course example</p>' in user['slides'][0]['text']
    assert 'a:hover\tPointer over link' in user['slides'][0]['text']
    assert 'PV=FV/(1+r)^n' in user['slides'][0]['text']
    assert 'Preserve relevant supplied code examples verbatim' in system
    assert 'an array field must remain an array' in system


@pytest.mark.parametrize("title,glossary", [
    ("CSS styling options and browser presentation", "Cascading Style Sheets"),
    ("Time value of money and financing decisions", "Net Present Value"),
    ("Data analysis methods and model evaluation", "Maximum Likelihood Estimation"),
])
def test_explicit_glossary_beats_long_node_title_under_tight_budget(tmp_path, title, glossary):
    # A real canonical scaffold contract, without copying the ranking algorithm.
    from test_knowledge_scaffold import run, MockModel
    scaffold = run(tmp_path, MockModel(), count=1)
    node = next(n for n in scaffold["nodes"].values() if n["node_type"] == "concept")
    node["title"] = title
    node["aliases"] = []
    node["content"]["key_terms"] = [glossary]
    words = derive_hotwords(scaffold, max_terms=1)["hotwords"]
    assert words[0]["term"] == glossary
    assert words[0]["source_slides"] == [1]
