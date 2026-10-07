import { useEffect, useRef, useState } from "react"
import { workspaceApi } from "../workspace/workspaceApi"
import type { ClassSession } from "../workspace/types"
import { useExecutionBackend } from "../workspace/ExecutionBackend"
import { enrichmentApi, type EnrichmentResults, type EnrichmentStatus } from "./enrichmentApi"

export function useEnrichment(courseId: string, lectureId: string, latest?: ClassSession | null) {
  const [sessions, setSessions] = useState<ClassSession[]>([])
  const [sessionId, setSessionId] = useState("")
  const [status, setStatus] = useState<EnrichmentStatus>()
  const [data, setData] = useState<EnrichmentResults>()
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState("")
  const [refresh, setRefresh] = useState(0)
  const { backend, setBackend } = useExecutionBackend("enrichment")
  const generation = useRef(0)
  const launchGuard = useRef(false)

  useEffect(() => {
    let current = true
    setSessions([]); setSessionId(""); setStatus(undefined); setData(undefined); setError("")
    workspaceApi.listSessions(courseId, lectureId).then((rows) => {
      if (!current) return
      const completed = rows.filter((s) => s.status === "completed")
      setSessions(completed)
      setSessionId(completed.at(-1)?.id ?? "")
    }).catch((e: unknown) => { if (current) setError(e instanceof Error ? e.message : "Sessions could not be loaded.") })
    return () => { current = false }
  }, [courseId, lectureId, latest?.id, latest?.status])

  useEffect(() => {
    let current = true
    const id = ++generation.current
    setStatus(undefined); setData(undefined); setError("")
    if (!sessionId) return
    const api = enrichmentApi(courseId, lectureId, sessionId)
    let timer: ReturnType<typeof setTimeout>
    const poll = async () => {
      try {
        const next = await api.status()
        if (!current || generation.current !== id) return
        setStatus(next)
        if (next.compatible) {
          const result = await api.results()
          if (current && generation.current === id) setData(result.manifest.compatible ? result : undefined)
        } else setData(undefined)
        if (next.status === "processing" || !next.asr_ready) timer = setTimeout(poll, 2500)
      } catch (e: unknown) {
        if (current) setError(e instanceof Error ? e.message : "Enrichment status could not be loaded.")
      }
    }
    void poll()
    return () => { current = false; clearTimeout(timer) }
  }, [courseId, lectureId, sessionId, refresh])

  const start = async () => {
    if (!sessionId || launchGuard.current) return
    launchGuard.current = true; setBusy(true); setError("")
    const id = generation.current
    try {
      const next = await enrichmentApi(courseId, lectureId, sessionId).run(backend)
      if (id === generation.current) { setStatus(next); setRefresh((v) => v + 1) }
    } catch (e: unknown) {
      if (id === generation.current) setError(e instanceof Error ? e.message : "Enrichment could not be started.")
    } finally { launchGuard.current = false; setBusy(false) }
  }
  const currentData = data?.manifest.course_id === courseId && data.manifest.lecture_id === lectureId
    && data.manifest.session_id === sessionId ? data : undefined
  return { sessions, sessionId, setSessionId, backend, setBackend, status, data: currentData, busy, error, start,
    refresh: () => setRefresh((v) => v + 1) }
}
export type EnrichmentController = ReturnType<typeof useEnrichment>
