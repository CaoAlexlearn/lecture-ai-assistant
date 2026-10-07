import { useEffect, useRef, useState } from "react"
import { ExecutionBackendSelect, useExecutionBackend } from "../workspace/ExecutionBackend"
import { workspaceApi } from "../workspace/workspaceApi"
import type { Agent2Results, Agent2Status } from "../workspace/types"

type Props = { courseId: string; lectureId: string; sessionId: string }
const STAGES: Record<string, string> = { retrieval: "Finding course material", correction: "Checking transcript", correction_expanded: "Checking more evidence", summary: "Updating summaries" }

export function CorrectionReview({ data, materialUrl }: { data: Agent2Results; materialUrl: string }) {
  return <div className="agent2-review__body">
    <p>{data.manifest.completed_block_count ?? 0}/{data.manifest.block_count ?? 0} blocks · {data.manifest.applied_edit_count ?? 0} corrections · {data.manifest.review_item_count ?? data.manifest.flagged_edit_count ?? 0} items to check</p>
    <p className="agent2-review__note">Corrections use extracted slide text. Unclear formulas and diagrams remain flagged for review. Candidate topics will be used for enrichment later.</p>
    {data.manifest.status === "completed_with_errors" && <p role="alert">Some blocks are unfinished. Retry to continue from saved progress.</p>}
    {data.blocks.map((block, index) => <details className="agent2-review__block" key={block.block_id} open={index === 0}>
      <summary>{block.title} <small>· {block.edits.filter((edit) => edit.decision === "apply").length} corrected · {block.edits.filter((edit) => edit.decision === "flag").length + (block.validation_issues ?? []).filter((issue) => issue.action === "skipped").length} to check</small></summary>
      <p className="agent2-review__topics">Candidate topics: {block.candidate_nodes.map((node) => node.title).join(" · ") || "No matching topic"}</p>
      {block.selection.unmatched_reason && <p>{block.selection.unmatched_reason}</p>}
      <p>Course material: {block.evidence_pages.length ? block.evidence_pages.map((page) => <a key={page} href={`${materialUrl}#page=${page}`} target="_blank" rel="noreferrer">Slide {page}</a>) : "No matching slide"}</p>
      {block.edits.map((edit, editIndex) => <article className={`agent2-review__edit is-${edit.decision}`} key={`${edit.unit_id}/${editIndex}`}>
        <strong>{edit.decision === "apply" ? "Corrected" : "Please check"}</strong>
        <p><span className="agent2-review__original">{edit.original}</span>{edit.replacement && <> → <span>{edit.replacement}</span></>}</p>
        <p>{edit.reason}</p>
        {edit.evidence.map((ref, refIndex) => <blockquote key={`${ref.page}/${refIndex}`}><a href={`${materialUrl}#page=${ref.page}`} target="_blank" rel="noreferrer">Slide {ref.page}</a>: {ref.quote}</blockquote>)}
      </article>)}
      {(block.validation_issues ?? []).filter((issue) => issue.action === "skipped").map((issue, issueIndex) => <article className="agent2-review__edit is-flag" key={`skipped/${issueIndex}`}>
        <strong>Original preserved · please check</strong>
        <p>A proposed correction could not be matched safely to the original transcript.</p>
        <p>{issue.error_message}</p>
      </article>)}
      {!block.edits.length && !block.validation_issues?.length && <p>No localized correction or uncertainty identified.</p>}
      <h3>Updated summary</h3>
      <ul>{block.corrected_summary.summary.map((text, i) => <li key={i}>{text}</li>)}</ul>
      {([ ["Teaching explanation", block.corrected_summary.how_teacher_explained], ["Examples", block.corrected_summary.examples], ["Emphasis", block.corrected_summary.emphasis] ] as const).map(([title, items]) => items.length > 0 && <section key={title}><h3>{title}</h3><ul>{items.map((text, i) => <li key={i}>{text}</li>)}</ul></section>)}
      <details><summary>Compare original and corrected transcript</summary><div className="agent2-review__comparison">
        <section><h3>Original transcript</h3><p>{block.original_units.map((unit) => unit.text).join(" ")}</p></section>
        <section><h3>Corrected transcript</h3><p>{block.corrected_units.map((unit) => unit.text).join(" ")}</p></section>
      </div></details>
    </details>)}
    {!data.blocks.length && <p>No completed blocks are available yet.</p>}
  </div>
}

export function Agent2RunControl({ courseId, lectureId, sessionId }: Props) {
  const [status, setStatus] = useState<Agent2Status>()
  const [starting, setStarting] = useState(false)
  const { backend, setBackend } = useExecutionBackend("asr")
  const [error, setError] = useState("")
  const [refresh, setRefresh] = useState(0)
  const [data, setData] = useState<Agent2Results>()
  const [reviewing, setReviewing] = useState(false)
  const [loadingReview, setLoadingReview] = useState(false)
  const dialog = useRef<HTMLDialogElement>(null)
  const identity = useRef(0)

  useEffect(() => {
    const generation = ++identity.current
    let active = true
    let timer: ReturnType<typeof setTimeout> | undefined
    const poll = async () => {
      let again = true
      try {
        const next = await workspaceApi.getAgent2Status(courseId, lectureId, sessionId)
        if (!active) return
        setStatus(next)
        again = next.status === "processing" || !next.agent1_ready
      } catch (caught) {
        if (active) setError(caught instanceof Error ? caught.message : "Correction status is unavailable.")
      }
      if (active && again) timer = setTimeout(() => void poll(), 2_000)
    }
    void poll()
    return () => { active = false; if (identity.current === generation) identity.current++; clearTimeout(timer) }
  }, [courseId, lectureId, sessionId, refresh])

  useEffect(() => {
    const element = dialog.current
    if (reviewing && element && !element.open) element.showModal()
    return () => { if (element?.open) element.close() }
  }, [reviewing])

  const run = async () => {
    if (starting || status?.status === "processing") return
    const generation = identity.current
    setStarting(true)
    setError("")
    try {
      const next = await workspaceApi.runAgent2(courseId, lectureId, sessionId, backend)
      if (generation !== identity.current) return
      setStatus(next)
      setReviewing(false)
      setRefresh((value) => value + 1)
    } catch (caught) {
      if (generation === identity.current) setError(caught instanceof Error ? caught.message : "Correction could not be started.")
    } finally {
      if (generation === identity.current) setStarting(false)
    }
  }
  const review = async () => {
    const generation = identity.current
    setLoadingReview(true)
    setError("")
    try {
      const next = await workspaceApi.getAgent2Results(courseId, lectureId, sessionId)
      if (generation === identity.current) { setData(next); setReviewing(true) }
    } catch (caught) {
      if (generation === identity.current) setError(caught instanceof Error ? caught.message : "Corrections could not be loaded.")
    } finally {
      if (generation === identity.current) setLoadingReview(false)
    }
  }
  const failed = status?.status === "failed" || status?.status === "completed_with_errors"
  const currentStage = Object.values(status?.block_stages ?? {}).find((stage) => STAGES[stage])
  const label = starting ? "Starting correction…" : !status ? "Checking correction…"
    : status.status === "processing" ? `${currentStage ? STAGES[currentStage] : "Correcting"} · ${status.completed_block_count ?? 0}/${status.block_count ?? 0}`
      : !status.agent1_ready ? "Complete Agent1 first" : status.status === "completed" ? "Review corrections"
        : failed ? "Retry ASR correction" : "Run ASR correction"
  const materialUrl = `/api/courses/${encodeURIComponent(courseId)}/lectures/${encodeURIComponent(lectureId)}/files/material`

  return <div className="agent2-run-control">
    <ExecutionBackendSelect label="ASR 纠错 " value={backend} onChange={setBackend}
      disabled={!status || starting || status.status === "processing"} provider={status?.provider} model={status?.model} />
    <button className="agent1-run-control__button" type="button" onClick={() => void (status?.status === "completed" ? review() : run())}
      disabled={!status || !status.agent1_ready || starting || loadingReview || status.status === "processing"}>{label}</button>
    {status?.status === "completed" && <button className="agent2-run-control__review" type="button" onClick={() => void run()} disabled={starting || !status.agent1_ready}>Reuse / run ASR correction</button>}
    {failed && (status.completed_block_count ?? 0) > 0 && <button type="button" className="agent2-run-control__review" onClick={() => void review()} disabled={loadingReview}>Review saved blocks</button>}
    {(error || (failed && status.root_cause?.error_message)) && <span className="agent1-run-control__message agent2-run-control__message" role="alert" title={error || status?.root_cause?.error_message}>{error || status?.root_cause?.error_message}</span>}
    <dialog ref={dialog} className="agent2-review" onClose={() => setReviewing(false)} aria-labelledby="agent2-review-title">
      <header><h2 id="agent2-review-title">Transcript corrections</h2><button type="button" onClick={() => setReviewing(false)}>Close</button></header>
      {data && <CorrectionReview data={data} materialUrl={materialUrl} />}
      <footer><button className="agent1-run-control__button" type="button" onClick={() => void run()} disabled={starting || status?.status === "processing"}>Check / rerun correction</button><small>Unchanged inputs reuse saved results.</small></footer>
    </dialog>
  </div>
}
