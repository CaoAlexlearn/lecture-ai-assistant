"""Isolated UI smoke harness. All post-class providers are fake/disabled.

Run: .venv/bin/python tests/manual_agent2_enrichment.py --port 8896
Temporary fixture data never enters a real session. Stop with Ctrl-C.
"""
import argparse
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from test_agent2_enrichment import FakeProvider, upstream_fixture, write


def create_mock_app(directory):
    import classroom_app
    import agent2_enrichment_api
    from config import Settings
    from workspace_catalog import WorkspaceCatalog
    catalog = WorkspaceCatalog(directory)
    write(directory / "courses/index.json", {"schema_version": 1, "course_ids": []})
    course = catalog.create_course(title="Agent2 Mock Finance")
    lecture = catalog.create_lecture(course["id"], number="01", title="Enrichment Mock")
    # A three-page minimal PDF, so slide navigation is exercised too.
    import fitz
    doc = fitz.open()
    for title in ("Net Present Value", "Loan and discounting", "Real options"):
        page = doc.new_page(); page.insert_text((50, 50), title)
    catalog.upload_material(course["id"], lecture["id"], filename="mock-slides.pdf", content=doc.tobytes())
    doc.close()
    prepared = catalog.lecture_root(course["id"], lecture["id"]) / "prepared"
    prepared.mkdir()
    for name in ("lecture_scaffold.json", "lecture_scaffold.md", "asr_context.txt", "hotwords.json"):
        (prepared / name).write_text("[]")
    catalog.mark_prepared(course["id"], lecture["id"])
    session = catalog.start_session(course["id"], lecture["id"])
    catalog.complete_session(course["id"], lecture["id"], session["id"])
    root, final = upstream_fixture(directory / "fixture", ids=(course["id"], lecture["id"], session["id"]))
    shutil.copytree(root, catalog.lecture_root(course["id"], lecture["id"]), dirs_exist_ok=True)
    shutil.copytree(final.parent, (directory / session["transcriptPath"]).parent, dirs_exist_ok=True)
    fake = FakeProvider(delay=.15, fail_node="KN_0002")
    agent2_enrichment_api.provider_for_agent2 = lambda spec: fake
    def disabled(*args, **kwargs):
        raise ValueError("Real providers are disabled in this mock harness")
    classroom_app.provider_for_agent2 = disabled
    classroom_app.provider_from_spec = disabled
    os.environ["AGENT2_ENRICHMENT_CONTEXT_WINDOW_TOKENS"] = "100000"
    app = classroom_app.create_app(Settings(api_key="mock-only", workspace_id=None, send_workspace_header=False,
        source_language="en", target_language="zh", debug=False, raw_event_debug=False, agent1_enabled=False), workspace_catalog=catalog)
    @app.get("/__mock/audit")
    async def audit():
        return {"mock": True, "calls": {f"{k[0]}:{k[1]}": v for k, v in fake.calls.items()}}
    @app.post("/__mock/allow-retry")
    async def allow_retry():
        fake.fail_node = None
        return {"mock": True}
    # The application's GET catch-all precedes newly added test-only routes.
    audit_route = next(r for r in app.router.routes if getattr(r, "path", None) == "/__mock/audit")
    app.router.routes.remove(audit_route); app.router.routes.insert(0, audit_route)
    return app, f"/courses/{course['id']}/lectures/{lecture['id']}/live"


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8896)
    args = parser.parse_args()
    import uvicorn
    with tempfile.TemporaryDirectory(prefix="agent2-enrichment-mock-") as path:
        app, route = create_mock_app(Path(path))
        print(json.dumps({"mock_url": f"http://127.0.0.1:{args.port}{route}", "root": path}), flush=True)
        uvicorn.run(app, host="127.0.0.1", port=args.port, log_level="warning")
