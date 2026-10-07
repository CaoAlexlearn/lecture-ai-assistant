import { existsSync, readFileSync } from "node:fs"
import { resolve } from "node:path"
import { createElement } from "react"
import { renderToStaticMarkup } from "react-dom/server"
import { expect, test } from "vitest"
import { KnowledgeNodeInspector, relationTargetId } from "./KnowledgeNodeInspector"
import { KnowledgeTree } from "./KnowledgeTree"
import { defaultExpandedIds, firstContentNodeId, parseScaffold, sortedChildren, sourcePages, type ComparisonContent, type KnowledgeNode, type KnowledgeScaffold, type Relation } from "./scaffold"

const path = resolve(process.cwd(), "../courses/open-system/lectures/lecture-03-transport-layer/prepared/lecture_scaffold.json")
const original = existsSync(path) ? JSON.parse(readFileSync(path, "utf8")) : null
const scaffold = original ? parseScaffold(original) : ({} as KnowledgeScaffold)
const fixtureTest = original?.schema_version === 1 ? test : test.skip
const v2FixtureTest = original?.schema_version === "2.0" ? test : test.skip
const noop = () => {}
const inspect = (node: KnowledgeNode, source: KnowledgeScaffold = scaffold) =>
  renderToStaticMarkup(createElement(KnowledgeNodeInspector, { scaffold: source, node, onSelect: noop, onSlideFocus: noop }))

test("comparison inspector reads a canonical item-to-value map", () => {
  const node: KnowledgeNode = {
    node_id: "KN_0001", title: "TCP vs UDP", node_type: "comparison", parent_id: null,
    children: [], order: 0, aliases: [], scope: "Compare transport service models.",
    evidence: [], source_refs: [{ type: "slide", page: 2 }],
    content: { items: ["TCP", "UDP"], dimensions: [{ dimension: "Delivery", values: { TCP: "Reliable", UDP: "Best effort" } }], similarities: [], differences: [] },
  }
  const source: KnowledgeScaffold = { schema_version: "2.0", title: "Transport", root_nodes: [node.node_id], nodes: { [node.node_id]: node }, relations: [] }
  const html = inspect(node, source)
  expect(html).toContain("<table>")
  expect(html).toContain("Reliable")
  expect(html).toContain("Best effort")
})

v2FixtureTest("real Transport layer v2 comparison renders normalized items and evidence", () => {
  const node = Object.values(scaffold.nodes).find((candidate) =>
    candidate.node_type === "comparison" && candidate.content.items.length >= 2 &&
    candidate.content.dimensions.length > 0 && candidate.evidence.some((item) => item.kind === "slide_example"))
  expect(node).toBeDefined()
  if (!node || node.node_type !== "comparison") return
  const html = inspect(node)
  expect(html).toContain("<thead>")
  expect(html).toContain("<table>")
  expect(html).toContain(`<th scope="row">${node.content.dimensions[0].dimension}</th>`)
  expect(html).toContain("<h2>Examples from the slides</h2>")
})

v2FixtureTest("current FSM rules render and older v2 procedures stay readable", () => {
  const node = Object.values(scaffold.nodes).find((candidate) =>
    candidate.node_type === "procedure" && candidate.content.rules.length > 0 && candidate.content.steps.length === 0)
  expect(node).toBeDefined()
  if (!node || node.node_type !== "procedure") return
  const html = inspect(node)
  expect(html).toContain("<h2>Keep in mind</h2>")
  expect(html).toContain(node.content.rules[0])
  expect(html).not.toContain("<h2>Step by step</h2>")
  const older = structuredClone(scaffold)
  delete (older.nodes[node.node_id].content as unknown as { rules?: string[] }).rules
  const restored = parseScaffold(older).nodes[node.node_id]
  expect(restored.node_type).toBe("procedure")
  if (restored.node_type === "procedure") expect(restored.content.rules).toEqual([])
})

fixtureTest("real Transport layer v1 opens as v2 without changing its hierarchy", () => {
  expect(original.schema_version).toBe(1)
  expect(scaffold.schema_version).toBe("2.0")
  expect(scaffold.relations).toEqual([])
  expect(firstContentNodeId(scaffold)).toBe("KN_0002")
  expect(sortedChildren(scaffold, scaffold.root_nodes).map((node) => node.node_id)).toEqual(["KN_0001", "KN_0004", "KN_0014"])
  expect(scaffold.nodes.KN_0003.parent_id).toBe("KN_0002")
  expect(defaultExpandedIds(scaffold)).toContain("KN_0011")
  const html = renderToStaticMarkup(createElement(KnowledgeTree, { scaffold, selectedNodeId: "KN_0002", onSelect: noop }))
  expect(html).toContain("Go-Back-N (GBN) Protocol")
  expect(html).toContain("Selective Repeat (SR) Protocol")
  expect(html).toContain('aria-current="true"')
})

fixtureTest("RDT 1.0 concept has scope, assumptions, and mechanism rather than steps", () => {
  const node = scaffold.nodes.KN_0005
  expect(node.node_type).toBe("concept")
  const html = inspect(node)
  expect(html).toContain("SCOPE")
  expect(html).toContain("No bit errors")
  expect(html).toContain("No packet loss")
  expect(html).toContain("<h2>MECHANISM</h2>")
  expect(html).not.toContain("<h2>Step by step</h2>")
  expect(html).toContain('aria-label="Jump to slide 15"')
})

fixtureTest("procedure inspector uses numbered typed steps", () => {
  const html = inspect(scaffold.nodes.KN_0003)
  expect(html).toContain("<h2>Step by step</h2><ol")
  expect(html).toContain("Sender treats segment contents")
  expect(html).not.toContain("<h2>MECHANISM</h2>")
  expect(html).toContain('aria-label="Jump to slide 9"')
})

fixtureTest("formula inspector shows expression and variables", () => {
  const node = scaffold.nodes.KN_0010
  expect(node.node_type).toBe("formula")
  const html = inspect(node)
  expect(html).toContain("<h2>FORMULA</h2>")
  expect(html).toContain("<h2>VARIABLES</h2>")
  expect(html).not.toContain("<h2>Step by step</h2>")
})

fixtureTest("comparison inspector renders dimensions as cards", () => {
  const base = scaffold.nodes.KN_0002
  const content: ComparisonContent = {
    items: ["TCP", "UDP"],
    dimensions: [{ dimension: "Delivery", values: { TCP: "Reliable", UDP: "Best effort" } }],
    similarities: ["Both are transport protocols"],
    differences: ["Reliability differs"],
  }
  const node = { ...base, node_type: "comparison" as const, content }
  const html = inspect(node)
  expect(html).toContain("<thead>")
  expect(html).toContain("<table>")
  expect(html).toContain('<th scope="row">Delivery</th>')
  expect(html).toContain("<h4>TCP</h4>")
  expect(html).toContain("<h4>UDP</h4>")
  expect(html).toContain("<h2>SIMILARITIES</h2>")
})

fixtureTest("relation target buttons navigate to the opposite node in either direction", () => {
  const edge: Relation = { relation_id: "REL-0001", from_node_id: "KN_0005", to_node_id: "KN_0006", type: "evolves_to", reason: "RDT 2.0 adds bit-error handling.", source_refs: [{ type: "slide", page: 17 }] }
  expect(relationTargetId(edge, "KN_0005")).toBe("KN_0006")
  expect(relationTargetId(edge, "KN_0006")).toBe("KN_0005")
  const withRelation = { ...scaffold, relations: [edge] }
  const forward = inspect(scaffold.nodes.KN_0005, withRelation)
  const backward = inspect(scaffold.nodes.KN_0006, withRelation)
  expect(forward).toContain('data-target-node-id="KN_0006"')
  expect(backward).toContain('data-target-node-id="KN_0005"')
  expect(backward).toContain("Evolves from")
  expect(forward).toContain("RDT 2.0 adds bit-error handling.")
})

fixtureTest("legacy extra formula, variables, and comparison stay visible as migrated evidence", () => {
  const html = inspect(scaffold.nodes.KN_0009)
  expect(html).toContain("<h2>PPT EVIDENCE</h2>")
  expect(html).toContain("U_sender = (L / R) / (RTT + L / R)")
  expect(html).toContain("Timeout Too Early")
  expect(html).toContain("<h2>MECHANISM</h2>")
  expect(html).not.toContain("<h2>Step by step</h2>")
  expect(html).toContain(`Jump to slide ${sourcePages(scaffold.nodes.KN_0009)[0]}`)
})

fixtureTest("empty topic content shows children and source without blank sections", () => {
  const html = inspect(scaffold.nodes.KN_0001)
  expect(html).toContain("CHILD NODES")
  expect(html).toContain("SOURCE")
  expect(html).not.toContain("<h2>SUMMARY</h2>")
})

fixtureTest("v2 reader rejects cross-type content fields and dangling relations", () => {
  const invalidContent = structuredClone(scaffold)
  Object.assign(invalidContent.nodes.KN_0005.content, { steps: [] })
  expect(() => parseScaffold(invalidContent)).toThrow("KN_0005")
  const dangling = structuredClone(scaffold)
  dangling.relations = [{ relation_id: "REL-0001", from_node_id: "KN_0005", to_node_id: "KN_9999", type: "evolves_to", reason: "Unsupported", source_refs: [] }]
  expect(() => parseScaffold(dangling)).toThrow("REL-0001")
})
