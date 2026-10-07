import { type ReactNode } from "react"
import { ReadingText } from "../ui/ReadingText"
import { MathFormula } from "../formulas/MathFormula"
import "./reading.css"
import { slideLabel, sortedChildren, sourcePages, type KnowledgeNode, type KnowledgeScaffold, type Relation } from "./scaffold"

type Props = {
  scaffold: KnowledgeScaffold
  node: KnowledgeNode
  onSelect: (id: string) => void
  onSlideFocus: (page: number) => void
  classroomEnrichment?: ReactNode
}

function Section({ title, children }: { title: string; children: ReactNode }) {
  return <section className="knowledge-inspector__section"><h2>{title}</h2>{children}</section>
}

function BulletList({ items }: { items: string[] }) {
  return <ul className="knowledge-inspector__examples">{items.map((item, index) => <li key={`${index}:${item}`}>{item}</li>)}</ul>
}

export function relationTargetId(relation: Relation, currentId: string): string {
  return relation.to_node_id === currentId ? relation.from_node_id : relation.to_node_id
}

function RelationCard({ relation, currentId, scaffold, onSelect, onSlideFocus }: {
  relation: Relation
  currentId: string
  scaffold: KnowledgeScaffold
  onSelect: (id: string) => void
  onSlideFocus: (page: number) => void
}) {
  const incoming = relation.to_node_id === currentId
  const targetId = relationTargetId(relation, currentId)
  const target = scaffold.nodes[targetId]
  const label = incoming && relation.type === "evolves_to" ? "Evolves from" : incoming && relation.type === "depends_on" ? "Required by" : relation.type.replaceAll("_", " ")
  return <article className="knowledge-inspector__relation" key={relation.relation_id}>
    <span className="knowledge-inspector__relation-type">{label}</span>
    <button type="button" data-target-node-id={targetId} onClick={() => onSelect(targetId)}>{target.title} <span aria-hidden="true">↗</span></button>
    <p>{relation.reason}</p>
    {relation.source_refs.length > 0 && <div className="knowledge-inspector__relation-sources">{relation.source_refs.map((ref) => <button key={ref.page} type="button" onClick={() => onSlideFocus(ref.page)}>Slide {ref.page}</button>)}</div>}
  </article>
}

export function KnowledgeNodeInspector({ scaffold, node, onSelect, onSlideFocus, classroomEnrichment }: Props) {
  const pages = sourcePages(node)
  const children = sortedChildren(scaffold, node.children)
  const relations = scaffold.relations.filter((relation) => relation.from_node_id === node.node_id || relation.to_node_id === node.node_id)
  const examples = node.evidence.filter((item) => item.kind === "slide_example")
  const migratedEvidence = node.evidence.filter((item) => item.kind !== "slide_example")
  const lead = node.node_type === "topic" ? node.content.summary : node.node_type === "concept" ? node.content.definition : node.node_type === "procedure" ? node.content.objective : node.node_type === "formula" ? node.content.interpretation : node.node_type === "relationship" ? node.content.statement : node.scope
  const conditions = node.node_type === "concept" || node.node_type === "formula" ? node.content.assumptions : node.node_type === "procedure" ? node.content.conditions : []
  const comparisonItems = node.node_type === "comparison" ? [...new Set([...node.content.items, ...node.content.dimensions.flatMap((dimension) => Object.keys(dimension.values))])] : []

  return <aside className="knowledge-inspector reading-document" aria-label="KnowledgeNode Inspector">
    <div className="knowledge-inspector__scroll">
      <article className="reading-document__body">
        <header className="knowledge-inspector__header">
          <div className="reading-document__provenance"><span>课件知识</span><span>{node.node_type}</span></div>
          <h1>{node.title}</h1>
          {lead && <div className="reading-document__lead"><ReadingText text={lead} /></div>}
          {node.scope && node.scope !== lead && <p className="reading-document__scope">{node.scope}</p>}
          {pages.length > 0 && <div className="knowledge-inspector__sources" aria-label="课件来源">{pages.map((page) => <button type="button" key={page} onClick={() => onSlideFocus(page)} aria-label={"Jump to slide " + page}>Slide {page}</button>)}</div>}
        </header>
        {node.node_type === "topic" && node.content.key_points.length > 0 && <Section title="Key ideas">{node.content.key_points.map((text, i) => <ReadingText key={i} text={text} />)}</Section>}
        {node.node_type === "concept" && <>
          {node.content.mechanism.length > 0 && <Section title="How it works">{node.content.mechanism.map((text, i) => <ReadingText key={i} text={text} />)}</Section>}
          {node.content.key_points.length > 0 && <Section title="What to remember"><BulletList items={node.content.key_points} /></Section>}
        </>}
        {node.node_type === "procedure" && <>
          {node.content.steps.length > 0 && <Section title="Step by step"><ol className="reading-steps">{node.content.steps.map((step, index) => <li key={index} value={step.order}><ReadingText text={step.content} /></li>)}</ol></Section>}
          {node.content.result && <Section title="The result"><ReadingText text={node.content.result} /></Section>}
        </>}
        {node.node_type === "formula" && node.content.expression && <Section title="The formula">
          <MathFormula expression={node.content.expression} />
          <details className="reading-source"><summary>原始表达式</summary><code>{node.content.expression}</code></details>
        </Section>}
        {node.node_type === "formula" && node.content.variables.length > 0 && <Section title="Reading the notation"><dl className="knowledge-inspector__variables">{node.content.variables.map(({ symbol, meaning }, i) => <div key={i}><dt>{symbol}</dt><dd>{meaning}</dd></div>)}</dl></Section>}
        {node.node_type === "comparison" && <>
          {comparisonItems.length > 0 && <Section title="Side by side"><div className="reading-comparison" tabIndex={0} role="region" aria-label="Comparison table"><table><thead><tr><th scope="col">Compare</th>{comparisonItems.map((item) => <th scope="col" key={item}>{item}</th>)}</tr></thead><tbody>{node.content.dimensions.map((dimension, i) => <tr key={i}><th scope="row">{dimension.dimension}</th>{comparisonItems.map((item) => <td key={item}>{dimension.values[item] ? <ReadingText text={dimension.values[item]} /> : "—"}</td>)}</tr>)}</tbody></table></div></Section>}
          {node.content.similarities.length > 0 && <Section title="What they share">{node.content.similarities.map((text, i) => <ReadingText key={i} text={text} />)}</Section>}
          {node.content.differences.length > 0 && <Section title="The important difference">{node.content.differences.map((text, i) => <ReadingText key={i} text={text} />)}</Section>}
        </>}
        {node.node_type === "relationship" && node.content.key_points.length > 0 && <Section title="Understanding the connection">{node.content.key_points.map((text, i) => <ReadingText key={i} text={text} />)}</Section>}
        {examples.length > 0 && <Section title="Examples from the slides"><div className="reading-examples">{examples.map((item, i) => <div key={i}><ReadingText text={item.text} /></div>)}</div></Section>}
        {classroomEnrichment}
        {conditions.length > 0 && <Section title="When this applies"><div className="reading-caution"><BulletList items={conditions} /></div></Section>}
        {node.node_type === "procedure" && node.content.rules.length > 0 && <Section title="Keep in mind"><BulletList items={node.content.rules} /></Section>}
        {node.node_type === "concept" && node.content.key_terms.length > 0 && <details className="reading-source reading-document__appendix"><summary>Key terms · {node.content.key_terms.length}</summary><p>{node.content.key_terms.join(" · ")}</p></details>}
        {children.length > 0 && <Section title="Explore this topic"><div className="knowledge-inspector__children">{children.map((child) => <button key={child.node_id} type="button" onClick={() => onSelect(child.node_id)}><span>{child.title}</span><small>{slideLabel(child)}</small></button>)}</div></Section>}
        {relations.length > 0 && <Section title="Connected ideas"><div className="knowledge-inspector__relations">{relations.map((relation) => <RelationCard key={relation.relation_id} relation={relation} currentId={node.node_id} scaffold={scaffold} onSelect={onSelect} onSlideFocus={onSlideFocus} />)}</div></Section>}
        <details className="reading-source reading-document__appendix"><summary>Source details</summary><p>Knowledge node <code>{node.node_id}</code></p>{migratedEvidence.map((item, index) => <div key={index}><strong>{item.label ?? item.kind.replace("legacy_", "").replaceAll("_", " ")}</strong><ReadingText text={item.text} /></div>)}</details>
      </article>
    </div>
  </aside>
}
