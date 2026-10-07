import { describe, expect, test, vi, afterEach } from "vitest"
import { renderToStaticMarkup } from "react-dom/server"
import { ClassroomEnrichment, EnrichmentReview, NodeEnrichment } from "./ClassroomEnrichment"
import { enrichmentApi, type EnrichmentResults, type Fragment } from "./enrichmentApi"
import type { KnowledgeScaffold } from "../knowledge-scaffold/scaffold"
import type { EnrichmentController } from "./useEnrichment"

const source = { block_id: "B001", unit_id: "U0001", start: 0, end: 23, quote: "Discount the cash flow.", original_unit_text: "Discount the cash flaw.", session_id: "s", asr_fingerprint: "a", scaffold_fingerprint: "tree" }
const fragment: Fragment = { fragment_id: "s:B001:f", block_id: "B001", description: "Discounting explanation", corrected_text: source.quote, types: ["reasoning"], status: "mapped", reason: "Explains present value.", assessment: false, sources: [source], unresolved_correction_ids: ["edit1"], corrections: [{ correction_id: "edit1", unit_id: "U0001", original: "flaw", replacement: "flow", decision: "flag", reason: "Check recording.", evidence: [{ page: 2, quote: "cash flow" }] }] }
const scaffold = { nodes: { KN_0001: { title: "Present value" }, KN_0002: { title: "Discounting" } } } as unknown as KnowledgeScaffold
const data: EnrichmentResults = {
  manifest: { status: "completed_with_errors", asr_status: "completed", asr_ready: true, compatible: true, stale: false, incomplete_block_ids: ["B002"], failed_node_ids: ["KN_0002"] },
  fragments: [fragment], relations: ["KN_0001", "KN_0002"].map((node_id) => ({ fragment_id: fragment.fragment_id, node_id, reason: "Actual contribution.", contribution: "Value calculation.", angle: `Use for ${node_id}`, types: ["reasoning"] })),
  pending: [{ ...fragment, status: "unmatched", assessment: true, types: ["assessment"] }], requirements: [{ ...fragment, status: "unmatched", assessment: true }],
  shared_content: [{ shared_content_id: "shared:f", fragment_id: fragment.fragment_id, description: "Shared reasoning", text: source.quote, sources: [source] }],
  node_enrichment: [{ node_id: "KN_0001", fragment_ids: [fragment.fragment_id], batch_count: 1, omitted_fragments: [], items: [{ item_id: "item1", heading: "Value calculation", text: "Use the shared reasoning to find present value.", source_nature: "uncertain", fragment_ids: [fragment.fragment_id], shared_content_ids: ["shared:f"], course_evidence: [], sources: [source], needs_review: true, uncertainty: "Check the rate." }] }],
}

describe("Classroom Enrichment", () => {
  test("node review keeps original-language content, shared references and verifiable provenance", () => {
    const html = renderToStaticMarkup(<NodeEnrichment nodeId="KN_0001" data={data} scaffold={scaffold} onSelect={() => {}} onSlideFocus={() => {}} />)
    expect(html).toContain("Value calculation")
    expect(html).toContain("Shared classroom example or reasoning")
    expect(html).toContain("Unresolved ASR question")
    expect(html).toContain("U0001 [0, 23)")
    expect(html).toContain("Discount the cash flaw.")
    expect(html).toContain('data-target-node-id="KN_0002"')
    expect(html).toContain("Node → classroom fragments")
    expect(html).not.toContain("老师未讲")
  })
  test("unmatched nodes make an evidence statement and mapped unfinished nodes show partial status", () => {
    const empty = renderToStaticMarkup(<NodeEnrichment nodeId="missing" data={data} scaffold={scaffold} onSelect={() => {}} onSlideFocus={() => {}} />)
    expect(empty).toContain("No matched classroom evidence.")
    const unfinished = renderToStaticMarkup(<NodeEnrichment nodeId="KN_0002" data={data} scaffold={scaffold} onSelect={() => {}} onSlideFocus={() => {}} />)
    expect(unfinished).toContain("enrichment is unfinished")
  })
  test("lecture review retains requirements, unclassified knowledge, failures and both navigation directions", () => {
    const html = renderToStaticMarkup(<EnrichmentReview data={data} scaffold={scaffold} onSelect={() => {}} onSlideFocus={() => {}} />)
    expect(html).toContain("Unclassified knowledge · 1")
    expect(html).toContain("Lecture assessment and project requirements · 1")
    expect(html).toContain("Block → classroom fragments → nodes")
    expect(html).toContain("Unfinished blocks: B002")
    expect(html).toContain("Unfinished nodes: KN_0002")
    expect(html).toContain("do not prove that every classroom idea was identified")
  })
  test("upstream processing disables manual start and hides stale results", () => {
    const controller = { sessions: [{ id: "s", createdAt: "today" }], sessionId: "s", status: { ...data.manifest, asr_status: "processing", asr_ready: false, compatible: false, stale: true }, data: { ...data, manifest: { ...data.manifest, compatible: false } }, busy: false, error: "", start: vi.fn(), refresh: vi.fn(), setSessionId: vi.fn() } as unknown as EnrichmentController
    const html = renderToStaticMarkup(<ClassroomEnrichment controller={controller} nodeId="KN_0001" scaffold={scaffold} onSelect={() => {}} onSlideFocus={() => {}} />)
    expect(html).toMatch(/<button[^>]*disabled=""[^>]*>Retry enrichment/)
    expect(html).toContain("older source version")
    expect(html).not.toContain("Value calculation")
    expect(controller.start).not.toHaveBeenCalled()
  })
})

describe("manual enrichment API client", () => {
  afterEach(() => vi.unstubAllGlobals())
  test("status and results are GET; only an explicit run uses POST with encoded session identity", async () => {
    const fetch = vi.fn().mockResolvedValue({ ok: true, json: async () => data })
    vi.stubGlobal("fetch", fetch)
    const api = enrichmentApi("c space", "lecture/1", "s+2")
    await api.status(); await api.results(); await api.run()
    expect(fetch.mock.calls[0][0]).toContain("c%20space/lectures/lecture%2F1/sessions/s%2B2/agent2/enrichment")
    expect(fetch.mock.calls[0][1]).toBeUndefined()
    expect(fetch.mock.calls[1][1]).toBeUndefined()
    expect(fetch.mock.calls[2][1]).toEqual({ method: "POST" })
  })
})

test("reading sections retain every item, shared example and review warning", () => {
  const originalItem = data.node_enrichment[0].items[0]
  const expanded = { ...data, node_enrichment: [{ ...data.node_enrichment[0], items: [
    originalItem,
    { ...originalItem, item_id: "item2", text: "A second explanation with a different source context." },
    { ...originalItem, item_id: "item3", heading: "Another topic", text: "Another topic must remain separate." },
  ] }] }
  const html = renderToStaticMarkup(<NodeEnrichment nodeId="KN_0001" data={expanded} scaffold={scaffold} onSelect={() => {}} onSlideFocus={() => {}} />)
  expect(html.match(/<h3>Value calculation<\/h3>/g)).toHaveLength(1)
  expect(html).toContain('data-item-id="item1"')
  expect(html).toContain('data-item-id="item2"')
  expect(html).toContain('data-item-id="item3"')
  expect(html).toContain("A second explanation with a different source context.")
  expect(html).toContain("Needs review: ")
  expect(html).toContain("Check the rate.")
  expect(html).toContain("Discount the cash flaw.")
})
