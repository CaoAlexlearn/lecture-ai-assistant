import { useEffect, useRef, useState } from "react"
import type { RealtimeConnectionStatus, RealtimeSnapshot, ServerEvent } from "../../types"
import { workspaceApi } from "../workspace/workspaceApi"
import type { ClassSession } from "../workspace/types"
import { Agent1RunControl } from "./Agent1RunControl"
import { Agent2RunControl } from "./Agent2RunControl"
import "./live-classroom.css"

type SessionContext = { courseId: string; lectureId: string; courseTitle: string; lectureTitle: string }
type Props = {
  context: SessionContext
  expanded: boolean
  onExpandedChange: (expanded: boolean) => void
  onSessionLockChange?: (locked: boolean) => void
  activeSession?: ClassSession | null
  lastSession?: ClassSession | null
  onSessionChange?: () => void
}

const EMPTY_SNAPSHOT: RealtimeSnapshot = {
  revision: 0,
  source: { committed: "", unstable: "", display: "" },
  translation: { committed: "", unstable: "", display: "" },
}
const CONNECTION_LABEL: Record<RealtimeConnectionStatus, string> = {
  idle: "Ready", connecting: "Connecting", resuming: "Resuming", listening: "Listening",
  pausing: "Pausing", paused: "Paused", interrupted: "Connection interrupted",
  stopping: "Saving transcript", finished: "Saved", error: "Disconnected",
}

function realtimeUrl(context: SessionContext, sessionId: string): string {
  const protocol = window.location.protocol === "https:" ? "wss:" : "ws:"
  const url = new URL(`${protocol}//${window.location.host}/ws`)
  url.searchParams.set("course_id", context.courseId)
  url.searchParams.set("lecture_id", context.lectureId)
  url.searchParams.set("session_id", sessionId)
  return url.toString()
}

function formatDuration(seconds: number): string {
  return [Math.floor(seconds / 3600), Math.floor((seconds % 3600) / 60), seconds % 60]
    .map((part) => String(part).padStart(2, "0")).join(":")
}

function errorMessage(value: unknown): string {
  return value instanceof Error ? value.message : "The realtime pipeline stopped unexpectedly."
}

function persistedStatus(session?: ClassSession | null): RealtimeConnectionStatus {
  if (!session) return "idle"
  if (session.status === "paused") return "paused"
  if (session.status === "completed") return "finished"
  if (session.runtimeState === "pausing") return "pausing"
  if (session.runtimeState === "stopping") return "stopping"
  if (session.status === "live") return "interrupted"
  return "idle"
}

export function LiveClassDock({ context, expanded, onExpandedChange, onSessionLockChange, activeSession, lastSession, onSessionChange }: Props) {
  const [status, setStatus] = useState<RealtimeConnectionStatus>(() => persistedStatus(activeSession))
  const [session, setSession] = useState<ClassSession | null>(activeSession ?? null)
  const [snapshot, setSnapshot] = useState<RealtimeSnapshot>(EMPTY_SNAPSHOT)
  const [elapsed, setElapsed] = useState(() => Math.floor(activeSession?.activeCaptureSeconds ?? 0))
  const [error, setError] = useState("")
  const socketRef = useRef<WebSocket | null>(null)
  const streamRef = useRef<MediaStream | null>(null)
  const audioContextRef = useRef<AudioContext | null>(null)
  const workletRef = useRef<AudioWorkletNode | null>(null)
  const recorderWorkerRef = useRef<Worker | null>(null)
  const workletStopResolverRef = useRef<(() => void) | null>(null)
  const recorderFlushResolverRef = useRef<(() => void) | null>(null)
  const startedAtRef = useRef<number | null>(null)
  const activeBaseSecondsRef = useRef(0)
  const statusRef = useRef<RealtimeConnectionStatus>(persistedStatus(activeSession))
  const sessionRef = useRef<ClassSession | null>(activeSession ?? null)
  const requestedActionRef = useRef<"pause" | "complete" | null>(null)
  const sourceTranscriptRef = useRef<HTMLDivElement>(null)
  const translationTranscriptRef = useRef<HTMLDivElement>(null)
  const requestedContextRef = useRef(context)

  const updateStatus = (next: RealtimeConnectionStatus) => {
    statusRef.current = next
    setStatus(next)
    onSessionLockChange?.(["connecting", "resuming", "listening", "pausing", "stopping"].includes(next))
  }
  const updateSession = (next: ClassSession | null) => {
    sessionRef.current = next
    setSession(next)
    onSessionChange?.()
  }

  useEffect(() => {
    if (["connecting", "resuming", "listening", "pausing", "stopping"].includes(statusRef.current)) return
    sessionRef.current = activeSession ?? null
    setSession(activeSession ?? null)
    if (activeSession) {
      activeBaseSecondsRef.current = activeSession.activeCaptureSeconds ?? 0
      setElapsed(Math.floor(activeSession.activeCaptureSeconds ?? 0))
      updateStatus(persistedStatus(activeSession))
    }
  }, [activeSession, context.courseId, context.lectureId])

  useEffect(() => {
    if (status !== "listening" || !startedAtRef.current) return
    const update = () => setElapsed(activeBaseSecondsRef.current + Math.floor((Date.now() - startedAtRef.current!) / 1000))
    update()
    const timer = window.setInterval(update, 1_000)
    return () => window.clearInterval(timer)
  }, [status])

  useEffect(() => { sourceTranscriptRef.current?.scrollTo({ top: sourceTranscriptRef.current.scrollHeight }) }, [snapshot.source.display])
  useEffect(() => { translationTranscriptRef.current?.scrollTo({ top: translationTranscriptRef.current.scrollHeight }) }, [snapshot.translation.display])

  const closeMedia = async (sendEof: boolean) => {
    const worklet = workletRef.current
    workletRef.current = null
    if (worklet) {
      await new Promise<void>((resolve) => {
        let settled = false
        const finish = () => {
          if (settled) return
          settled = true
          workletStopResolverRef.current = null
          resolve()
        }
        workletStopResolverRef.current = finish
        worklet.port.postMessage({ command: "stop" })
        window.setTimeout(finish, 250)
      })
      worklet.disconnect()
    }
    streamRef.current?.getTracks().forEach((track) => track.stop())
    streamRef.current = null
    const recorder = recorderWorkerRef.current
    recorderWorkerRef.current = null
    if (recorder && sendEof) {
      await new Promise<void>((resolve) => {
        let settled = false
        const finish = () => {
          if (settled) return
          settled = true
          recorderFlushResolverRef.current = null
          resolve()
        }
        recorderFlushResolverRef.current = finish
        recorder.postMessage({ command: "flush", requestId: crypto.randomUUID() })
        window.setTimeout(finish, 500)
      })
    }
    recorder?.terminate()
    const audioContext = audioContextRef.current
    audioContextRef.current = null
    if (audioContext && audioContext.state !== "closed") await audioContext.close()
    const socket = socketRef.current
    if (sendEof && socket?.readyState === WebSocket.OPEN) socket.send(new ArrayBuffer(0))
  }

  const cleanup = async () => {
    await closeMedia(false)
    const socket = socketRef.current
    socketRef.current = null
    if (socket && socket.readyState < WebSocket.CLOSING) socket.close(1000, "client cleanup")
  }
  useEffect(() => () => { void cleanup() }, [])

  const handleServerEvent = (message: ServerEvent) => {
    if (message.type === "session_started") {
      const requested = requestedContextRef.current
      if (message.course_id !== requested.courseId || message.lecture_id !== requested.lectureId || message.session_id !== sessionRef.current?.id) {
        setError("The realtime session did not match the selected course and was stopped.")
        updateStatus("error")
        void cleanup()
        return
      }
      startedAtRef.current = Date.now()
      activeBaseSecondsRef.current = sessionRef.current?.activeCaptureSeconds ?? 0
      updateStatus("listening")
    } else if (message.type === "realtime_snapshot") {
      setSnapshot({ revision: message.revision, source: message.source, translation: message.translation })
    } else if (message.type === "error") {
      setError(message.message)
      updateStatus("error")
    } else if (message.type === "session_finished" && requestedActionRef.current === null && statusRef.current === "listening") {
      updateStatus("interrupted")
    }
  }

  const connectSocket = (url: string): Promise<WebSocket> => new Promise((resolve, reject) => {
    const socket = new WebSocket(url)
    socket.binaryType = "arraybuffer"
    socket.onopen = () => resolve(socket)
    socket.onerror = () => reject(new Error("Unable to connect to the realtime server."))
    socket.onmessage = (event) => {
      try { handleServerEvent(JSON.parse(String(event.data)) as ServerEvent) }
      catch { setError("Received an invalid realtime server event.") }
    }
    socket.onclose = () => {
      if (socketRef.current === socket) socketRef.current = null
      if (requestedActionRef.current === null && ["connecting", "resuming", "listening"].includes(statusRef.current)) {
        setError("The classroom connection was interrupted. Saved realtime revisions remain available.")
        updateStatus("interrupted")
      }
    }
  })

  const startMicrophone = async (socket: WebSocket) => {
    const stream = await navigator.mediaDevices.getUserMedia({ audio: { channelCount: 1 }, video: false })
    const audioContext = new AudioContext()
    let worklet: AudioWorkletNode | null = null
    let recorder: Worker | null = null
    try {
      await audioContext.audioWorklet.addModule("/pcm_worklet.js")
      const source = audioContext.createMediaStreamSource(stream)
      worklet = new AudioWorkletNode(audioContext, "pcm-forwarder", { numberOfInputs: 1, numberOfOutputs: 0, channelCount: 1 })
      recorder = new Worker("/recorder_worker.js")
      recorder.postMessage({ command: "init", config: { sampleRate: audioContext.sampleRate, targetSampleRate: 16_000 } })
      recorder.onmessage = (event: MessageEvent<{ type: "pcm" | "flushed"; buffer?: ArrayBuffer }>) => {
        if (event.data.type === "flushed") {
          recorderFlushResolverRef.current?.()
        } else if (event.data.buffer && socket.readyState === WebSocket.OPEN) {
          socket.send(event.data.buffer)
        }
      }
      worklet.port.onmessage = (event: MessageEvent<{ type: "pcm" | "stopped"; buffer?: ArrayBuffer }>) => {
        if (event.data.type === "stopped") {
          workletStopResolverRef.current?.()
        } else if (event.data.buffer) {
          recorder?.postMessage({ command: "record", buffer: event.data.buffer }, [event.data.buffer])
        }
      }
      source.connect(worklet)
      streamRef.current = stream
      audioContextRef.current = audioContext
      workletRef.current = worklet
      recorderWorkerRef.current = recorder
    } catch (caught) {
      worklet?.disconnect()
      recorder?.terminate()
      stream.getTracks().forEach((track) => track.stop())
      if (audioContext.state !== "closed") await audioContext.close()
      throw caught
    }
  }

  const startClass = async () => {
    if (["connecting", "resuming", "listening", "pausing", "stopping"].includes(statusRef.current)) return
    requestedContextRef.current = context
    onExpandedChange(true)
    setError("")
    const existing = ["live", "paused"].includes(sessionRef.current?.status ?? "") ? sessionRef.current : null
    const paused = existing?.status === "paused"
    if (!existing) setSnapshot(EMPTY_SNAPSHOT)
    updateStatus(paused ? "resuming" : "connecting")
    try {
      const serverSession = existing ? await workspaceApi.getSession(context.courseId, context.lectureId, existing.id) : null
      const nextSession = serverSession?.status === "paused"
        ? await workspaceApi.resumeSession(context.courseId, context.lectureId, serverSession.id)
        : serverSession?.status === "live" ? serverSession : await workspaceApi.startSession(context.courseId, context.lectureId)
      updateSession(nextSession)
      const socket = await connectSocket(realtimeUrl(context, nextSession.id))
      socketRef.current = socket
      await startMicrophone(socket)
    } catch (caught) {
      await cleanup()
      setError(caught instanceof DOMException && caught.name === "NotAllowedError" ? "Microphone access was denied. Allow access and try again." : errorMessage(caught))
      updateStatus(sessionRef.current?.status === "live" ? "interrupted" : "error")
    }
  }

  const pauseClass = async () => {
    const current = sessionRef.current
    if (statusRef.current !== "listening" || !current) return
    try {
      requestedActionRef.current = "pause"
      updateStatus("pausing")
      await closeMedia(true)
      const paused = await workspaceApi.pauseSession(context.courseId, context.lectureId, current.id)
      updateSession(paused)
      activeBaseSecondsRef.current = paused.activeCaptureSeconds ?? elapsed
      startedAtRef.current = null
      await cleanup()
      requestedActionRef.current = null
      updateStatus("paused")
    } catch (caught) {
      requestedActionRef.current = null
      setError(errorMessage(caught))
      updateStatus("error")
    }
  }

  const stopClass = async () => {
    const current = sessionRef.current
    if (!current || !["live", "paused"].includes(current.status)) return
    try {
      requestedActionRef.current = "complete"
      updateStatus("stopping")
      await closeMedia(true)
      const completed = await workspaceApi.completeSession(context.courseId, context.lectureId, current.id)
      updateSession(completed)
      startedAtRef.current = null
      await cleanup()
      requestedActionRef.current = null
      updateStatus("finished")
    } catch (caught) {
      requestedActionRef.current = null
      setError(errorMessage(caught))
      updateStatus("error")
    }
  }

  const active = status === "listening"
  const busy = ["connecting", "resuming", "pausing", "stopping"].includes(status)
  const latestSession = session && ["completed", "aborted"].includes(session.status) ? session : lastSession
  const agent1Session = !["connecting", "resuming", "listening", "pausing", "stopping"].includes(status)
    && (!session || session.status === "completed") && latestSession?.status === "completed"
    && latestSession.courseId === context.courseId && latestSession.lectureId === context.lectureId
    ? latestSession : null
  const sessionLabel = active ? "Live" : ["connecting", "resuming", "pausing", "stopping", "error", "interrupted"].includes(status)
    ? CONNECTION_LABEL[status] : session?.status === "paused" ? "Paused" : session?.status === "live"
      ? "Connection interrupted · reconnect available" : latestSession ? `Last session ${latestSession.status}` : "Ready to start"

  return (
    <section className={`live-class-dock ${expanded ? "is-expanded" : ""}`} aria-label="Live interpretation">
      <header className="live-class-dock__bar">
        <button className="live-class-dock__toggle" type="button" onClick={() => onExpandedChange(!expanded)} aria-expanded={expanded}>
          <span className={`live-class-dock__dot is-${status}`} aria-hidden="true" />
          <span><strong>Live Interpretation</strong><small>{sessionLabel} · {context.lectureTitle}</small></span>
          <span className="live-class-dock__chevron" aria-hidden="true">{expanded ? "⌄" : "⌃"}</span>
        </button>
        <div className="live-class-dock__facts"><span>EN → 中文</span><time>{formatDuration(elapsed)}</time></div>
        {active || status === "pausing" || status === "stopping" ? (
          <div className="live-class-dock__actions"><button className="live-class-dock__action is-pause" type="button" onClick={() => void pauseClass()} disabled={busy}>{status === "pausing" ? "Pausing…" : "Pause"}</button><button className="live-class-dock__action is-stop" type="button" onClick={() => void stopClass()} disabled={busy}>{status === "stopping" ? "Saving…" : "Stop"}</button></div>
        ) : session?.status === "paused" || session?.status === "live" ? (
          <div className="live-class-dock__actions"><button className="live-class-dock__action is-start" type="button" onClick={() => void startClass()} disabled={busy}>{session.status === "paused" ? "Resume" : "Reconnect"}</button><button className="live-class-dock__action is-stop" type="button" onClick={() => void stopClass()} disabled={busy}>Stop</button></div>
        ) : <button className="live-class-dock__action is-start" type="button" onClick={() => void startClass()} disabled={busy}>{status === "connecting" ? "Connecting…" : status === "resuming" ? "Resuming…" : latestSession ? "Start New Session" : "Start Class"}</button>}
        {agent1Session && <button type="button" className="live-class-dock__postclass-button" popoverTarget={`postclass-${agent1Session.id}`}>课后处理</button>}
      </header>
      {agent1Session && <div popover="auto" id={`postclass-${agent1Session.id}`} className="live-class-dock__postclass" aria-label="课后处理">
        <header><strong>课后处理</strong><button type="button" popoverTarget={`postclass-${agent1Session.id}`} popoverTargetAction="hide">关闭</button></header>
        <p>分别选择运行方式，然后点击运行。切换方式会生成新结果，之前的结果会保留。</p>
        <div className="live-class-dock__postclass-controls"><Agent1RunControl
          key={`${context.courseId}/${context.lectureId}/${agent1Session.id}`}
          courseId={context.courseId} lectureId={context.lectureId} sessionId={agent1Session.id} />
        <Agent2RunControl
          key={`agent2/${context.courseId}/${context.lectureId}/${agent1Session.id}`}
          courseId={context.courseId} lectureId={context.lectureId} sessionId={agent1Session.id} /></div>
      </div>}

      {expanded && <div className="live-class-dock__content">
        <section className="live-class-transcript" aria-label="Bilingual live transcript">
          <div className="live-class-panel-heading"><strong>Live transcript</strong><span>Revision {snapshot.revision}</span></div>
          {error && <div className="live-class-error" role="alert">{error}</div>}
          <div className="live-class-transcript__columns">
            <article className="live-class-stream-block">
              <div className="live-class-stream-block__label"><span>English</span>{snapshot.source.display && <small>{snapshot.source.unstable ? "live correction" : "stable"}</small>}</div>
              <div className="live-class-stream-block__scroll" ref={sourceTranscriptRef} tabIndex={0}>
                {snapshot.source.display ? <p lang="en"><span>{snapshot.source.committed}</span>{snapshot.source.unstable && <span className="is-unstable">{snapshot.source.committed ? " " : ""}{snapshot.source.unstable}</span>}</p>
                  : <p className="live-class-empty">{active ? "Listening for speech…" : "Start the class to see English transcription."}</p>}
              </div>
            </article>
            <article className="live-class-stream-block is-translation">
              <div className="live-class-stream-block__label"><span>中文</span>{snapshot.translation.display && <small>{snapshot.translation.unstable ? "实时修正中" : "已稳定"}</small>}</div>
              <div className="live-class-stream-block__scroll" ref={translationTranscriptRef} tabIndex={0}>
                {snapshot.translation.display ? <p lang="zh-CN"><span>{snapshot.translation.committed}</span>{snapshot.translation.unstable && <span className="is-unstable">{snapshot.translation.unstable}</span>}</p>
                  : <p className="live-class-empty">{active ? "等待中文翻译…" : "开始课堂后在此显示中文翻译。"}</p>}
              </div>
            </article>
          </div>
        </section>
      </div>}
    </section>
  )
}
