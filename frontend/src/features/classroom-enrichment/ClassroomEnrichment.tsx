import { useRef, useState } from "react"
import { ReadingText } from "../ui/ReadingText"
import { ExecutionBackendSelect } from "../workspace/ExecutionBackend"
import type { KnowledgeScaffold } from "../knowledge-scaffold/scaffold"
import type { EnrichmentResults, EnrichmentItem, Fragment } from "./enrichmentApi"
import type { EnrichmentController } from "./useEnrichment"
import "./enrichment.css"

export function FragmentSources({ fragment, onSlideFocus }: { fragment: Fragment; onSlideFocus: (page: number) => void }) {
  return <details className="classroom-enrichment__sources"><summary>Sources · {fragment.block_id} · {fragment.sources.length} ranges</summary>
    {fragment.sources.map((r) => <div key={`${r.unit_id}:${r.start}:${r.end}`}>
      <small>{r.session_id} / {r.block_id} / {r.unit_id} [{r.start}, {r.end})</small>
      <blockquote>{r.quote}</blockquote>
      <details><summary>Original unit before ASR correction</summary><p>{r.original_unit_text}</p></details>
    </div>)}
    {fragment.corrections.map((c) => <div key={c.correction_id}>
      <strong>{c.decision === "flag" ? "Unresolved ASR question" : "ASR correction"}</strong>
      <p>{c.original}{c.replacement ? ` → ${c.replacement}` : ""}</p><p>{c.reason}</p>
      {c.evidence.map((e, i) => <button key={`${e.page}:${i}`} type="button" onClick={() => onSlideFocus(e.page)}>Slide {e.page}: {e.quote}</button>)}
    </div>)}
  </details>
}

function FragmentCard({ fragment, data, scaffold, onSelect, onSlideFocus }: {
  fragment: Fragment; data: EnrichmentResults; scaffold: KnowledgeScaffold
  onSelect: (nodeId: string) => void; onSlideFocus: (page: number) => void
}) {
  const links = data.relations.filter((l) => l.fragment_id === fragment.fragment_id)
  return <article className="classroom-enrichment__card" data-fragment-id={fragment.fragment_id}>
    <strong>{fragment.description}</strong><small>{fragment.status.replaceAll("_", " ")} · {fragment.types.join(", ")}</small>
    {(fragment.status !== "mapped" || fragment.unresolved_correction_ids.length > 0 || fragment.review_code) && <p className="classroom-enrichment__notice">{fragment.reason}{fragment.review_code ? ` · ${fragment.review_code}` : ""}</p>}
    {fragment.uncertainty && <p className="classroom-enrichment__notice">{fragment.uncertainty}</p>}
    {links.map((link) => <div key={link.node_id}><button type="button" data-target-node-id={link.node_id} onClick={() => onSelect(link.node_id)}>{scaffold.nodes[link.node_id]?.title ?? link.node_id} ↗</button>
      <p>{link.angle}</p><details><summary>Mapping evidence</summary><p>{link.reason}</p><p>{link.contribution}</p></details></div>)}
    <FragmentSources fragment={fragment} onSlideFocus={onSlideFocus} />
  </article>
}

export function EnrichmentReview({ data, scaffold, onSelect, onSlideFocus }: {
  data: EnrichmentResults; scaffold: KnowledgeScaffold; onSelect: (nodeId: string) => void; onSlideFocus: (page: number) => void
}) {
  const blocks = [...new Set(data.fragments.map((f) => f.block_id))]
  const [blockId, setBlockId] = useState("")
  const card = (f: Fragment) => <FragmentCard key={f.fragment_id} fragment={f} data={data} scaffold={scaffold} onSelect={onSelect} onSlideFocus={onSlideFocus} />
  return <div className="classroom-enrichment__review">
    <p>Range checks account for identified fragments and source positions. They do not prove that every classroom idea was identified.</p>
    {(data.manifest.incomplete_block_ids?.length || data.manifest.failed_node_ids?.length) ? <p role="alert">Unfinished blocks: {data.manifest.incomplete_block_ids?.join(", ") || "none"}. Unfinished nodes: {data.manifest.failed_node_ids?.join(", ") || "none"}.</p> : null}
    <details><summary>Unclassified knowledge · {data.pending.filter((f) => f.status === "unmatched").length}</summary>{data.pending.filter((f) => f.status === "unmatched").map(card)}</details>
    <details><summary>Needs review · {data.pending.filter((f) => f.status !== "unmatched").length}</summary>{data.pending.filter((f) => f.status !== "unmatched").map(card)}</details>
    <details><summary>Node enrichment questions · {data.node_enrichment.flatMap((n) => n.items).filter((i) => i.needs_review).length}</summary>{data.node_enrichment.map((n) => n.items.filter((i) => i.needs_review).map((i) => <article key={i.item_id}><button type="button" onClick={() => onSelect(n.node_id)}>{scaffold.nodes[n.node_id]?.title ?? n.node_id} ↗</button><p>{i.uncertainty}</p></article>))}</details>
    <details><summary>Lecture assessment and project requirements · {data.requirements.length}</summary>{data.requirements.map(card)}</details>
    <details><summary>Block → classroom fragments → nodes</summary>
      <label>Block <select value={blockId} onChange={(e) => setBlockId(e.target.value)}><option value="">All blocks</option>{blocks.map((b) => <option key={b}>{b}</option>)}</select></label>
      {data.fragments.filter((f) => !blockId || f.block_id === blockId).map(card)}
    </details>
  </div>
}

export function groupStudyItems(items: EnrichmentItem[]): EnrichmentItem[][] {
  const groups: EnrichmentItem[][] = []
  for (const item of items) {
    const last = groups.at(-1)
    if (last?.[0].heading === item.heading) last.push(item)
    else groups.push([item])
  }
  return groups
}

function StudyItem({ item, data, scaffold, onSelect, onSlideFocus }: {
  item: EnrichmentItem; data: EnrichmentResults; scaffold: KnowledgeScaffold
  onSelect: (nodeId: string) => void; onSlideFocus: (page: number) => void
}) {
  const sources = useRef<HTMLDetailsElement>(null)
  const fragments = data.fragments.filter((fragment) => item.fragment_ids.includes(fragment.fragment_id))
  const showSources = () => {
    if (!sources.current) return
    sources.current.open = true
    sources.current.querySelector("summary")?.focus()
    sources.current.scrollIntoView({ block: "nearest" })
  }
  return <div className="classroom-study-item" data-item-id={item.item_id}>
    <small className="classroom-study-kind">{item.source_nature.replaceAll("_", " ")}</small>
    <ReadingText text={item.text} citations={fragments.map((fragment) => fragment.fragment_id)} onCitation={showSources} />
    {(item.needs_review || item.uncertainty) && <p className="classroom-enrichment__notice">Needs review: {item.uncertainty || "Please check the original classroom source."}</p>}
    {item.shared_content_ids.map((id) => { const shared = data.shared_content.find((entry) => entry.shared_content_id === id); return shared ? <details className="reading-source" key={id}><summary>Shared classroom example or reasoning · {shared.description}</summary><blockquote><ReadingText text={shared.text} /></blockquote></details> : null })}
    <details ref={sources} className="reading-source classroom-study-sources"><summary>查看来源 · {item.course_evidence.length} 处课件 · {fragments.length} 段课堂原文</summary>
      {item.course_evidence.map((evidence, index) => <div key={index}><button onClick={() => onSlideFocus(evidence.page)}>Slide {evidence.page}</button><small>{evidence.source === "scaffold" ? "Course scaffold" : "Slide evidence"}</small><blockquote>{evidence.quote}</blockquote></div>)}
      <p>Enrichment sources · {item.fragment_ids.length} fragments</p>
      {fragments.map((fragment) => <FragmentCard key={fragment.fragment_id} fragment={fragment} data={data} scaffold={scaffold} onSelect={onSelect} onSlideFocus={onSlideFocus} />)}
      {item.sources.length > 0 && <details><summary>Source excerpts</summary>{item.sources.map((source, index) => <blockquote key={index}><small>{source.session_id} / {source.block_id} / {source.unit_id} [{source.start}, {source.end})</small><p>{source.quote}</p></blockquote>)}</details>}
    </details>
  </div>
}

export function NodeEnrichment({ nodeId, data, scaffold, onSelect, onSlideFocus }: {
  nodeId: string; data: EnrichmentResults; scaffold: KnowledgeScaffold
  onSelect: (nodeId: string) => void; onSlideFocus: (page: number) => void
}) {
  const node = data.node_enrichment.find((n) => n.node_id === nodeId)
  const links = data.relations.filter((l) => l.node_id === nodeId)
  return <div className="classroom-study">
    {!links.length && <p>No matched classroom evidence.</p>}
    {links.length > 0 && !node && <p role="status">Classroom fragments are mapped; this node’s enrichment is unfinished.</p>}
    {node?.batch_count && node.batch_count > 1 ? <p className="classroom-enrichment__notice">This node was organized in {node.batch_count} batches. Check cross-batch repetitions and conflicts.</p> : null}
    {groupStudyItems(node?.items ?? []).map((items) => <section className="classroom-study-section" key={items[0].item_id}>
      <h3>{items[0].heading}</h3>
      {items.map((item) => <StudyItem key={item.item_id} item={item} data={data} scaffold={scaffold} onSelect={onSelect} onSlideFocus={onSlideFocus} />)}
    </section>)}
    {node?.omitted_fragments.map((o) => <p key={o.fragment_id}>No additional study item: {o.reason}</p>)}
    {links.length > 0 && <details><summary>Node → classroom fragments · {links.length}</summary>{data.fragments.filter((f) => links.some((l) => l.fragment_id === f.fragment_id)).map((f) => <FragmentCard key={f.fragment_id} fragment={f} data={data} scaffold={scaffold} onSelect={onSelect} onSlideFocus={onSlideFocus} />)}</details>}
  </div>
}

export function ClassroomEnrichment({ controller: c, nodeId, scaffold, onSelect, onSlideFocus, embedded = false }: {
  embedded?: boolean; controller: EnrichmentController; nodeId: string; scaffold: KnowledgeScaffold
  onSelect: (nodeId: string) => void; onSlideFocus: (page: number) => void
}) {
  const [reviewOpen, setReviewOpen] = useState(false)
  const status = c.status
  const disabled = c.busy || status?.status === "processing" || !status?.asr_ready
  return <section className="knowledge-inspector__section classroom-enrichment" aria-label="Classroom Enrichment">
    <h2>课堂补充</h2>
    {!embedded && <h3 className="classroom-reading-title">{scaffold.nodes[nodeId]?.title}</h3>}
    {c.sessions.length ? <label className="classroom-enrichment__session">Class session <select disabled={c.busy} value={c.sessionId} onChange={(e) => c.setSessionId(e.target.value)}>{c.sessions.map((s) => {
      const date = new Date(s.createdAt)
      const label = Number.isNaN(date.getTime()) ? s.createdAt : date.toLocaleString(undefined, { year: "numeric", month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" })
      return <option value={s.id} key={s.id}>{label} · {s.id.slice(0, 6)}</option>
    })}</select></label> : <p>Complete a class session and ASR correction to prepare classroom enrichment.</p>}
    {c.sessionId && <>
      {status?.status === "processing" && <p role="status">课堂笔记正在生成，已完成 {(status.completed_node_count ?? 0)} / {(status.node_count ?? 0)} 个知识点。</p>}
      <details className="reading-source enrichment-tools"><summary>笔记生成与状态</summary>
      <ExecutionBackendSelect label="Agent2 " value={c.backend} onChange={c.setBackend}
        disabled={c.busy || status?.status === "processing"} provider={status?.provider} model={status?.model} />
      <p role="status">{status ? `${status.status.replaceAll("_", " ")} · ASR ${status.asr_status.replaceAll("_", " ")}` : "Loading enrichment status…"}</p>
      {status?.status === "processing" && <progress aria-label="Enrichment progress" value={(status.completed_block_count ?? 0) + (status.completed_node_count ?? 0)} max={(status.block_count ?? 0) + (status.node_count ?? 0) || 1} />}
      {status && <p>Mapping {status.completed_block_count ?? 0}/{status.block_count ?? 0} · Nodes {status.completed_node_count ?? 0}/{status.node_count ?? 0} · Review {status.review_count ?? 0} · Unclassified {status.unmatched_count ?? 0}</p>}
      <div className="classroom-enrichment__actions"><button type="button" disabled={disabled} onClick={() => void c.start()}>{c.busy ? "Starting…" : status?.status === "failed" || status?.status === "completed_with_errors" ? "Retry enrichment" : status?.status === "completed" && !status.stale ? "Reuse / run enrichment" : "Run enrichment"}</button><button type="button" onClick={c.refresh}>Refresh</button></div>
      </details>
      {status?.stale && <p className="classroom-enrichment__notice">Saved results belong to an older source version and are hidden.</p>}
      {status?.prerequisite_error && <p role="alert">{status.prerequisite_error}</p>}
      {status?.asr_failed_block_ids?.length ? <p role="alert">ASR unfinished blocks: {status.asr_failed_block_ids.join(", ")}</p> : null}
      {status?.asr_root_cause && <p role="alert">ASR: {status.asr_root_cause.error_message}</p>}
      {status?.root_cause && <p role="alert">{status.root_cause.error_message}</p>}
      {status?.status !== "processing" && status?.incomplete_block_ids?.length ? <p role="alert">Unfinished blocks: {status.incomplete_block_ids.join(", ")}</p> : null}
      {status?.status !== "processing" && (status?.incomplete_node_ids?.length || status?.failed_node_ids?.length) ? <p role="alert">Unfinished nodes: {(status.incomplete_node_ids ?? status.failed_node_ids)?.join(", ")}</p> : null}

    </>}
    {c.error && <p role="alert">{c.error}</p>}
    {c.data?.manifest.compatible && <>
      <NodeEnrichment nodeId={nodeId} data={c.data} scaffold={scaffold} onSelect={onSelect} onSlideFocus={onSlideFocus} />
      <button type="button" aria-expanded={reviewOpen} onClick={() => setReviewOpen((v) => !v)}>{reviewOpen ? "Hide lecture review" : "View blocks, requirements and review queue"}</button>
      {reviewOpen && <EnrichmentReview data={c.data} scaffold={scaffold} onSelect={onSelect} onSlideFocus={onSlideFocus} />}
    </>}
  </section>
}
