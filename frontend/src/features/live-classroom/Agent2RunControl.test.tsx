import { describe, expect, test } from "vitest"
import { renderToStaticMarkup } from "react-dom/server"
import { CorrectionReview } from "./Agent2RunControl"
import type { Agent2Results } from "../workspace/types"

describe("correction review", () => {
  test("shows corrections, unresolved numbers, original text and slide provenance separately", () => {
    const data: Agent2Results = { manifest: { status: "completed_with_errors", agent1_ready: true, block_count: 2, completed_block_count: 1, applied_edit_count: 1, flagged_edit_count: 1 }, blocks: [{
      block_id: "B001", title: "Discounting", candidate_nodes: [{ node_id: "KN_0001", title: "Net Present Value" }],
      selection: { unmatched_reason: null }, evidence_pages: [3],
      original_units: [{ unit_id: "U0001", text: "net prison value at 4.16%" }],
      corrected_units: [{ unit_id: "U0001", text: "Net Present Value at 4.16%" }],
      corrected_summary: { summary: ["Discount future cash flows."], how_teacher_explained: [], examples: [], emphasis: [] },
      edits: [
        { unit_id: "U0001", original: "net prison value", replacement: "Net Present Value", decision: "apply", kind: "terminology", reason: "Course terminology agrees.", evidence: [{ page: 3, quote: "Net Present Value" }] },
        { unit_id: "U0001", original: "4.16%", replacement: null, decision: "flag", kind: "number", reason: "Same-example evidence is insufficient.", evidence: [] },
      ],
    }] }
    const html = renderToStaticMarkup(<CorrectionReview data={data} materialUrl="/material" />)
    expect(html).toContain("Corrected")
    expect(html).toContain("Please check")
    expect(html).toContain("Original transcript")
    expect(html).toContain("Corrected transcript")
    expect(html).toContain('/material#page=3')
    expect(html).toContain("Net Present Value at 4.16%")
    expect(html).toContain("Some blocks are unfinished")
  })

  test("shows skipped proposals as unresolved without pretending the source was changed", () => {
    const data: Agent2Results = { manifest: { status: "completed", agent1_ready: true, block_count: 1, completed_block_count: 1, applied_edit_count: 0, flagged_edit_count: 0, review_item_count: 1 }, blocks: [{
      block_id: "B013", title: "Go-Back-N", candidate_nodes: [], selection: { unmatched_reason: null }, evidence_pages: [],
      original_units: [{ unit_id: "U0001", text: "Keep the original speech." }], corrected_units: [{ unit_id: "U0001", text: "Keep the original speech." }],
      corrected_summary: { summary: [], how_teacher_explained: [], examples: [], emphasis: [] }, edits: [],
      validation_issues: [{ block_id: "B013", edit_index: 2, unit_id: "U9999", action: "skipped", error_message: "B013, edit 2, U9999: Edit references a unit outside the block" }],
    }] }
    const html = renderToStaticMarkup(<CorrectionReview data={data} materialUrl="/material" />)
    expect(html).toContain("1 items to check")
    expect(html).toContain("Original preserved")
    expect(html).toContain("B013, edit 2, U9999")
    expect(html).not.toContain("No localized correction or uncertainty identified")
    expect(html).not.toContain("Some blocks are unfinished")
  })
})
