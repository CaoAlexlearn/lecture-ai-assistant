import type { LectureOption } from "../lecture-map/types/lectureMap"

export type SourceRef = { page: number; type: "slide" }
export type NodeType = "topic" | "concept" | "procedure" | "formula" | "comparison" | "relationship"
export type RelationType = "depends_on" | "evolves_to" | "contrasts_with" | "applies_to" | "example_of" | "related_to"
export type Evidence = { kind: string; label: string | null; text: string }
export type Relation = { relation_id: string; from_node_id: string; to_node_id: string; type: RelationType; reason: string; source_refs: SourceRef[] }
export type TopicContent = { summary: string | null; key_points: string[] }
export type ConceptContent = { definition: string | null; assumptions: string[]; mechanism: string[]; key_points: string[]; key_terms: string[] }
export type ProcedureContent = { objective: string | null; conditions: string[]; steps: { order: number; content: string }[]; rules: string[]; result: string | null }
export type FormulaContent = { expression: string | null; variables: { symbol: string; meaning: string }[]; interpretation: string | null; assumptions: string[] }
export type ComparisonContent = { items: string[]; dimensions: { dimension: string; values: Record<string, string> }[]; similarities: string[]; differences: string[] }
export type RelationshipContent = { statement: string | null; key_points: string[] }

type NodeBase = {
  node_id: string
  title: string
  parent_id: string | null
  children: string[]
  order: number
  aliases: string[]
  scope: string
  evidence: Evidence[]
  source_refs: SourceRef[]
}
export type KnowledgeNode = NodeBase & (
  | { node_type: "topic"; content: TopicContent }
  | { node_type: "concept"; content: ConceptContent }
  | { node_type: "procedure"; content: ProcedureContent }
  | { node_type: "formula"; content: FormulaContent }
  | { node_type: "comparison"; content: ComparisonContent }
  | { node_type: "relationship"; content: RelationshipContent }
)
export type KnowledgeScaffold = {
  schema_version: "2.0"
  title: string
  root_nodes: string[]
  nodes: Record<string, KnowledgeNode>
  relations: Relation[]
}

type LegacyPrelecture = {
  core_idea: string | null
  definition: string | null
  key_terms: string[]
  formula: string | null
  variables: Record<string, string>
  steps: string[]
  comparison: Record<string, string> | null
  slide_examples: string[]
}
type LegacyNode = Omit<NodeBase, "scope" | "evidence"> & {
  node_type: NodeType
  prelecture: LegacyPrelecture
  relations: Record<string, string[]>
}

const contentFields: Record<NodeType, string[]> = {
  topic: ["summary", "key_points"],
  concept: ["definition", "assumptions", "mechanism", "key_points", "key_terms"],
  procedure: ["objective", "conditions", "steps", "rules", "result"],
  formula: ["expression", "variables", "interpretation", "assumptions"],
  comparison: ["items", "dimensions", "similarities", "differences"],
  relationship: ["statement", "key_points"],
}
const relationTypes = new Set<RelationType>(["depends_on", "evolves_to", "contrasts_with", "applies_to", "example_of", "related_to"])

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value)
}

function migrateLegacyNode(value: LegacyNode): KnowledgeNode {
  const pre = value.prelecture
  const evidence: Evidence[] = []
  const add = (kind: string, text: string, label: string | null = null) => evidence.push({ kind, label, text })
  const mapped = new Set<string>()
  let content: KnowledgeNode["content"]
  switch (value.node_type) {
    case "topic":
      content = { summary: pre.definition, key_points: [...pre.steps] }
      mapped.add("definition"); mapped.add("steps")
      break
    case "concept": {
      const terms = new Set(pre.key_terms.map((term) => term.toLowerCase()))
      const assumptions: string[] = []
      if (terms.has("no bit errors")) assumptions.push("No bit errors")
      if (terms.has("no packet loss") || terms.has("no loss of packets")) assumptions.push("No packet loss")
      content = { definition: pre.definition, assumptions, mechanism: [...pre.steps], key_points: [], key_terms: [...pre.key_terms] }
      mapped.add("definition"); mapped.add("steps"); mapped.add("key_terms")
      break
    }
    case "procedure":
      content = { objective: pre.definition, conditions: [], steps: pre.steps.map((step, index) => ({ order: index + 1, content: step })), rules: [], result: null }
      mapped.add("definition"); mapped.add("steps")
      break
    case "formula":
      content = { expression: pre.formula, variables: Object.entries(pre.variables).map(([symbol, meaning]) => ({ symbol, meaning })), interpretation: pre.definition, assumptions: [] }
      mapped.add("formula"); mapped.add("variables"); mapped.add("definition")
      break
    case "comparison":
      content = { items: Object.keys(pre.comparison ?? {}), dimensions: pre.comparison ? [{ dimension: "Overview", values: { ...pre.comparison } }] : [], similarities: [], differences: [] }
      mapped.add("comparison")
      break
    case "relationship":
      content = { statement: pre.definition, key_points: [...pre.steps] }
      mapped.add("definition"); mapped.add("steps")
      break
  }
  if (!mapped.has("definition") && pre.definition) add("legacy_definition", pre.definition)
  if (!mapped.has("formula") && pre.formula) add("legacy_formula", pre.formula)
  if (!mapped.has("steps")) pre.steps.forEach((text) => add("legacy_step", text))
  if (!mapped.has("key_terms")) pre.key_terms.forEach((text) => add("legacy_key_term", text))
  pre.slide_examples.forEach((text) => add("slide_example", text))
  if (!mapped.has("variables")) Object.entries(pre.variables).forEach(([label, text]) => add("legacy_variable", text, label))
  if (!mapped.has("comparison")) Object.entries(pre.comparison ?? {}).forEach(([label, text]) => add("legacy_comparison", text, label))
  const { prelecture: _prelecture, relations: _relations, ...base } = value
  void _prelecture; void _relations
  return { ...base, scope: pre.core_idea || value.title, content, evidence } as KnowledgeNode
}

function normalizeLegacy(value: Record<string, unknown>): KnowledgeScaffold {
  const legacyNodes = value.nodes as Record<string, LegacyNode>
  const nodes: Record<string, KnowledgeNode> = {}
  const relations: Relation[] = []
  const kindMap: Record<string, RelationType> = { prerequisites: "depends_on", related_to: "related_to", contrasts_with: "contrasts_with" }
  const seen = new Set<string>()
  for (const [id, node] of Object.entries(legacyNodes)) {
    if (!isRecord(node) || !isRecord(node.prelecture) || !isRecord(node.relations)) throw new Error(`Knowledge Scaffold node ${id} is invalid.`)
    nodes[id] = migrateLegacyNode(node)
    for (const [oldKind, kind] of Object.entries(kindMap)) {
      for (const target of node.relations[oldKind] ?? []) {
        const edge = `${id}:${target}:${kind}`
        if (seen.has(edge)) continue
        seen.add(edge)
        relations.push({ relation_id: `REL-${String(relations.length + 1).padStart(4, "0")}`, from_node_id: id, to_node_id: target, type: kind, reason: `Migrated from v1 ${oldKind}; rationale unavailable.`, source_refs: [] })
      }
    }
  }
  return { schema_version: "2.0", title: value.title as string, root_nodes: value.root_nodes as string[], nodes, relations }
}

export function parseScaffold(value: unknown): KnowledgeScaffold {
  if (!isRecord(value) || !isRecord(value.nodes) || !Array.isArray(value.root_nodes) || typeof value.title !== "string") {
    throw new Error("Knowledge Scaffold has an invalid structure.")
  }
  const scaffold = value.schema_version === 1 ? normalizeLegacy(value) : value as KnowledgeScaffold
  if (scaffold.schema_version !== "2.0" || !Array.isArray(scaffold.relations)) throw new Error("Unsupported Knowledge Scaffold version.")
  for (const node of Object.values(scaffold.nodes)) {
    if (node?.node_type === "procedure" && !Array.isArray((node.content as unknown as Record<string, unknown>).rules)) {
      node.content = { ...node.content, rules: [] } // committed v2 packages before procedure.rules
    }
  }
  const seen = new Set<string>()
  const visit = (id: string, parent: string | null) => {
    const node = scaffold.nodes[id]
    if (!node || node.node_id !== id || node.parent_id !== parent || !Array.isArray(node.children) ||
      !Array.isArray(node.source_refs) || !Array.isArray(node.evidence) || typeof node.scope !== "string" ||
      !isRecord(node.content) || !(node.node_type in contentFields) ||
      Object.keys(node.content).sort().join() !== [...contentFields[node.node_type]].sort().join()) {
      throw new Error(`Knowledge Scaffold node ${id} is invalid.`)
    }
    if (seen.has(id)) throw new Error(`Knowledge Scaffold contains a repeated or circular node: ${id}.`)
    seen.add(id)
    node.children.forEach((childId) => visit(childId, id))
  }
  scaffold.root_nodes.forEach((id) => visit(id, null))
  if (seen.size !== Object.keys(scaffold.nodes).length) throw new Error("Knowledge Scaffold has unreachable nodes.")
  const edges = new Set<string>()
  for (const relation of scaffold.relations) {
    const key = `${relation.from_node_id}:${relation.to_node_id}:${relation.type}`
    if (!scaffold.nodes[relation.from_node_id] || !scaffold.nodes[relation.to_node_id] || relation.from_node_id === relation.to_node_id ||
      !relationTypes.has(relation.type) || edges.has(key)) throw new Error(`Knowledge Scaffold relation ${relation.relation_id} is invalid.`)
    edges.add(key)
  }
  return scaffold
}

export function sortedChildren(scaffold: KnowledgeScaffold, ids: string[]): KnowledgeNode[] {
  return ids.map((id) => scaffold.nodes[id]).filter((node): node is KnowledgeNode => Boolean(node))
    .sort((a, b) => a.order - b.order || a.node_id.localeCompare(b.node_id))
}

export function sourcePages(node: KnowledgeNode): number[] {
  return [...new Set(node.source_refs.map((ref) => ref.page).filter((page) => Number.isInteger(page) && page > 0))]
    .sort((a, b) => a - b)
}

export function slideLabel(node: KnowledgeNode): string {
  const pages = sourcePages(node)
  if (!pages.length) return ""
  if (pages.length === 1) return `Slide ${pages[0]}`
  return `Slides ${pages[0]}–${pages.at(-1)}`
}

function hasContent(node: KnowledgeNode): boolean {
  if (node.node_type === "topic") return false
  return Object.values(node.content).some((value) => typeof value === "string" ? Boolean(value) : Array.isArray(value) && value.length > 0) || node.evidence.length > 0
}

export function firstContentNodeId(scaffold: KnowledgeScaffold): string | undefined {
  const visit = (ids: string[]): string | undefined => {
    for (const node of sortedChildren(scaffold, ids)) {
      if (hasContent(node)) return node.node_id
      const nested = visit(node.children)
      if (nested) return nested
    }
    return undefined
  }
  return visit(scaffold.root_nodes) ?? sortedChildren(scaffold, scaffold.root_nodes)[0]?.node_id
}

export function defaultExpandedIds(scaffold: KnowledgeScaffold): string[] {
  const ids: string[] = []
  const visit = (children: string[], depth: number) => {
    for (const node of sortedChildren(scaffold, children)) {
      if (node.children.length && depth < 3) ids.push(node.node_id)
      if (depth < 3) visit(node.children, depth + 1)
    }
  }
  visit(scaffold.root_nodes, 0)
  return ids
}

export async function loadKnowledgeScaffold(lecture: LectureOption): Promise<KnowledgeScaffold> {
  const url = lecture.source.scaffoldUrl
  if (!url) throw new Error("This lecture has no Knowledge Scaffold. Run PreAgent to prepare it.")
  const response = await fetch(url, { headers: { Accept: "application/json" } })
  if (!response.ok) throw new Error(`Could not load Knowledge Scaffold (${response.status}).`)
  return parseScaffold(await response.json() as unknown)
}
