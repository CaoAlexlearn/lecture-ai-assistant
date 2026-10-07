import { useEffect, useState } from "react"
import { workspaceApi } from "../workspace/workspaceApi"
import type { Agent1Status } from "../workspace/types"
import { ExecutionBackendSelect, useExecutionBackend } from "../workspace/ExecutionBackend"

type Props = { courseId: string; lectureId: string; sessionId: string }

function progressLabel(value: Agent1Status): string {
  if (!value.block_count) return "Agent1 running…"
  return `Agent1 running · ${value.completed_block_count ?? 0}/${value.block_count}`
}

export function Agent1RunControl({ courseId, lectureId, sessionId }: Props) {
  const [result, setResult] = useState<Agent1Status>()
  const [loading, setLoading] = useState(true)
  const [starting, setStarting] = useState(false)
  const [error, setError] = useState("")
  const { backend, setBackend } = useExecutionBackend("agent1")

  useEffect(() => {
    let active = true
    setResult(undefined)
    setLoading(true)
    setError("")
    void workspaceApi.getAgent1Status(courseId, lectureId, sessionId)
      .then((next) => { if (active) setResult(next) })
      .catch((caught: unknown) => { if (active) setError(caught instanceof Error ? caught.message : "Agent1 status is unavailable.") })
      .finally(() => { if (active) setLoading(false) })
    return () => { active = false }
  }, [courseId, lectureId, sessionId])

  useEffect(() => {
    if (result?.status !== "processing") return
    let active = true
    const timer = window.setInterval(() => {
      void workspaceApi.getAgent1Status(courseId, lectureId, sessionId)
        .then((next) => { if (active) { setResult(next); setError("") } })
        .catch((caught: unknown) => { if (active) setError(caught instanceof Error ? caught.message : "Agent1 status is unavailable.") })
    }, 2_000)
    return () => { active = false; window.clearInterval(timer) }
  }, [courseId, lectureId, sessionId, result?.status])

  const run = async () => {
    if (starting || result?.status === "processing") return
    setStarting(true)
    setError("")
    try {
      const next = await workspaceApi.runAgent1(courseId, lectureId, sessionId, backend)
      setResult(next)
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "Agent1 could not be started.")
    } finally {
      setStarting(false)
    }
  }

  const failed = result?.status === "failed" || result?.status === "completed_with_errors"
  const label = loading ? "Checking Agent1…"
    : starting ? "Starting Agent1…"
      : result?.status === "processing" ? progressLabel(result)
        : result?.status === "completed" ? "Reuse / run Agent1"
          : failed ? "Retry Agent1" : "Run Agent1"
  const message = error || (failed ? result?.root_cause?.error_message
    ?? `${result?.failed_block_ids?.length ?? 0} blocks need another run.` : "")

  return <div className="agent1-run-control">
    <ExecutionBackendSelect label="Agent1 " value={backend} onChange={setBackend}
      disabled={loading || starting || result?.status === "processing"} provider={result?.segmenter_provider} model={result?.segmenter_model} />
    {result?.status === "completed" && <small>Agent1 complete · {result.completed_block_count ?? result.block_count ?? 0} blocks</small>}
    <button className={`agent1-run-control__button ${result?.status === "completed" ? "is-complete" : ""}`} type="button"
      onClick={() => void run()} disabled={loading || starting || result?.status === "processing"}
      aria-label={label}>{label}</button>
    {message && <span className="agent1-run-control__message" role="alert" title={message}>{message}</span>}
  </div>
}
