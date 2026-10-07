import { AppHeader } from "../ui/AppHeader"
import { useEffect, useMemo, useState } from "react"
import type { WorkspaceCourse } from "../workspace/types"
import { agentLabApi } from "./agentLabApi"
import type { AgentDefinition, AgentTestRun, PromptVersion, ResolvedContext } from "./types"
import "./agent-lab.css"

type Props = { courses: WorkspaceCourse[] }

function formatDate(value: string): string {
  return new Date(value).toLocaleString([], { dateStyle: "medium", timeStyle: "short" })
}

export function AgentLabPage({ courses }: Props) {
  const [agents, setAgents] = useState<AgentDefinition[]>([])
  const [agent, setAgent] = useState<AgentDefinition>()
  const [version, setVersion] = useState<PromptVersion>()
  const [content, setContent] = useState("")
  const [note, setNote] = useState("")
  const [courseId, setCourseId] = useState(courses[0]?.id ?? "")
  const course = courses.find((item) => item.id === courseId) ?? courses[0]
  const [lectureId, setLectureId] = useState(course?.lectures[0]?.id ?? "")
  const [transcript, setTranscript] = useState("")
  const [hardMaxReached, setHardMaxReached] = useState(false)
  const [resolved, setResolved] = useState<ResolvedContext>()
  const [testRun, setTestRun] = useState<AgentTestRun>()
  const [history, setHistory] = useState<AgentTestRun[]>([])
  const [diff, setDiff] = useState("")
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState("")
  const [model, setModel] = useState("")

  const selectVersion = (next: PromptVersion) => {
    setVersion(next)
    setContent(next.content ?? "")
    setNote(next.note ?? "")
    setDiff("")
  }

  const loadAgent = async (agentId: string) => {
    const next = await agentLabApi.getAgent(agentId)
    setAgent(next)
    setModel(next.modelConfig.model ?? "")
    const selected = next.versions.find((item) => item.status === "draft")
      ?? next.versions.find((item) => item.id === next.activePromptVersionId)
      ?? next.versions[0]
    if (selected) selectVersion(selected)
    else { setVersion(undefined); setContent("") }
    setHistory(next.executable ? await agentLabApi.listTests(agentId) : [])
    setResolved(undefined); setTestRun(undefined); setError("")
  }

  useEffect(() => {
    agentLabApi.listAgents().then((items) => {
      setAgents(items)
      const first = items.find((item) => item.agentId === "agent1") ?? items[0]
      if (first) void loadAgent(first.agentId)
    }).catch((caught) => setError(caught instanceof Error ? caught.message : "Agent Lab could not be loaded."))
  }, [])

  useEffect(() => {
    const nextCourse = courses.find((item) => item.id === courseId) ?? courses[0]
    if (nextCourse && !nextCourse.lectures.some((item) => item.id === lectureId)) setLectureId(nextCourse.lectures[0]?.id ?? "")
  }, [courseId, courses, lectureId])

  const payload = useMemo(() => ({
    agentId: agent?.agentId,
    promptVersionId: version?.id,
    courseId,
    lectureId,
    transcript,
    hardMaxReached,
  }), [agent, version, courseId, lectureId, transcript, hardMaxReached])

  const createDraft = async (sourceVersionId?: string) => {
    if (!agent) return
    setBusy(true); setError("")
    try { selectVersion(await agentLabApi.createDraft(agent.agentId, sourceVersionId)); await loadAgent(agent.agentId) }
    catch (caught) { setError(caught instanceof Error ? caught.message : "Draft could not be created.") }
    finally { setBusy(false) }
  }

  const save = async () => {
    if (!agent || !version) return
    setBusy(true); setError("")
    try { selectVersion(await agentLabApi.saveDraft(agent.agentId, version.id, content, version.updatedAt, note)); await loadAgent(agent.agentId) }
    catch (caught) { setError(caught instanceof Error ? caught.message : "Draft could not be saved.") }
    finally { setBusy(false) }
  }

  const activate = async () => {
    if (!agent || !version) return
    const effect = agent.executable
      ? "This version will be used by new production runs."
      : "This version will become the canonical prompt. Its runtime is not implemented yet."
    if (!window.confirm(`Activate ${agent.name} Prompt ${version.id}?\n\n${effect}`)) return
    setBusy(true)
    try { await agentLabApi.activate(agent.agentId, version.id); await loadAgent(agent.agentId) }
    catch (caught) { setError(caught instanceof Error ? caught.message : "Version could not be activated.") }
    finally { setBusy(false) }
  }

  const compare = async () => {
    if (!agent?.activePromptVersionId || !version) return
    try { setDiff((await agentLabApi.compare(agent.agentId, agent.activePromptVersionId, version.id)).diff || "No differences.") }
    catch (caught) { setError(caught instanceof Error ? caught.message : "Versions could not be compared.") }
  }

  const saveModel = async () => {
    if (!agent) return
    setBusy(true); setError("")
    try {
      const next = await agentLabApi.updateModel(agent.agentId, model)
      setAgent(next)
      setAgents((current) => current.map((item) => item.agentId === next.agentId ? next : item))
      setModel(next.modelConfig.model ?? "")
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "Model configuration could not be saved.")
    } finally { setBusy(false) }
  }

  const resolveContext = async () => {
    setBusy(true); setError("")
    try { setResolved(await agentLabApi.resolveContext(payload)); setTestRun(undefined) }
    catch (caught) { setError(caught instanceof Error ? caught.message : "Context could not be resolved.") }
    finally { setBusy(false) }
  }

  const runTest = async () => {
    const scope = agent?.agentId === "pre-agent"
      ? "This runs the production Pre-Agent flow in an isolated workspace using existing normalized evidence. It may make multiple real model requests, but cannot overwrite lecture outputs."
      : "This sends one real model request and cannot write production Agent1 notes."
    if (!window.confirm(`Run ${agent?.name} test with ${version?.id} on ${course?.title}?\n\n${scope}`)) return
    setBusy(true); setError("")
    try { const run = await agentLabApi.runTest(payload); setTestRun(run); setResolved(run.resolvedContext); if (agent) setHistory(await agentLabApi.listTests(agent.agentId)) }
    catch (caught) { setError(caught instanceof Error ? caught.message : "Test run failed.") }
    finally { setBusy(false) }
  }

  const active = agent?.versions.find((item) => item.id === agent.activePromptVersionId)
  const selectedCourse = courses.find((item) => item.id === courseId) ?? courses[0]
  const selectedLecture = selectedCourse?.lectures.find((item) => item.id === lectureId) ?? selectedCourse?.lectures[0]
  const editable = version?.status === "draft"
  const dirty = Boolean(editable && (content !== (version?.content ?? "") || note !== (version?.note ?? "")))
  const runnable = Boolean(selectedCourse && selectedLecture && version && !dirty)
  const modelDirty = Boolean(agent?.executable && model.trim() && model.trim() !== agent.modelConfig.model)

  return <main className="agent-lab-page">
    <AppHeader active="agent-lab" subtitle="Agent configuration">{active && <small>Active {active.id}</small>}</AppHeader>
    {error && <div className="agent-lab-error" role="alert">{error}</div>}
    {agent?.executable && <section className="model-config-editor" aria-label="Production model configuration"><div><small>{agent.name} · {agent.modelConfig.provider}</small><strong>Production model</strong></div><input value={model} onChange={(event) => setModel(event.target.value)} aria-label="Production model" spellCheck={false} /><button onClick={() => void saveModel()} disabled={busy || !modelDirty}>Save model</button><p>Applied to new production runs and tests. Active runs keep their original model snapshot.</p></section>}
    <div className="agent-lab-layout">
      <aside className="agent-list"><span>Agents</span>{agents.map((item) => <button key={item.agentId} className={item.agentId === agent?.agentId ? "is-selected" : ""} onClick={() => void loadAgent(item.agentId)}><strong>{item.name}</strong><small>Active {item.activePromptVersionId}{!item.executable && " · Runtime pending"}</small></button>)}</aside>
      <section className="prompt-workbench">{agent ? <>
        <header><div><span>Agent definition</span><h1>{agent.name}</h1><p>{agent.description}</p></div>{agent.promptEditable && !agent.versions.some((item) => item.status === "draft") && <button onClick={() => void createDraft()} disabled={busy}>Create Draft</button>}</header>
        <div className="prompt-version-bar"><div><small>Active</small><strong>{agent.activePromptVersionId}</strong></div><div><small>Viewing</small><strong>{version?.id} · {version?.status}{dirty && " · unsaved"}</strong></div></div>
        <textarea className="prompt-editor" value={content} readOnly={!editable} onChange={(event) => setContent(event.target.value)} spellCheck={false} aria-label="System prompt" />
        <label className="prompt-note">Version note<input value={note} readOnly={!editable} onChange={(event) => setNote(event.target.value)} /></label>
        <div className="prompt-actions">{editable && <><button onClick={() => { setContent(version?.content ?? ""); setNote(version?.note ?? "") }}>Reset Draft</button><button onClick={() => void save()} disabled={busy || !dirty}>Save Draft</button><button className="primary" onClick={() => void activate()} disabled={busy || dirty}>Activate</button></>}<button onClick={() => void compare()} disabled={!version || version.id === agent.activePromptVersionId}>Compare with Active</button></div>
        {diff && <pre className="prompt-diff">{diff}</pre>}
        {agent.executable ? <section className="agent-config"><h2>Production configuration</h2><dl><div><dt>Provider</dt><dd>{agent.modelConfig.provider}</dd></div><div><dt>Model</dt><dd>{agent.modelConfig.model}</dd></div><div><dt>Temperature</dt><dd>{agent.modelConfig.temperature ?? "Provider default"}</dd></div><div><dt>Max output</dt><dd>{agent.modelConfig.maxOutputTokens ?? "Provider default"}</dd></div></dl>{agent.agentId === "agent1" && <p>One shared model for Lab tests, post-class segmentation, and block summaries. Segmentation keeps its separate runtime output budget.</p>}<h3>Context sources</h3>{agent.contextPolicy.map((item) => <span key={item}>{item}</span>)}</section> : <div className="coming-later"><span>Runtime pending</span><p>The active prompt is stored as the canonical Agent definition. Test and production execution remain unavailable until this Agent runtime is implemented.</p></div>}
        <section className="version-history"><h2>Version history</h2>{agent.versions.map((item) => <button key={item.id} className={item.id === version?.id ? "is-selected" : ""} onClick={() => selectVersion(item)}><strong>{item.id} · {item.status}</strong><small>{formatDate(item.updatedAt)}</small>{item.status === "archived" && <em onClick={(event) => { event.stopPropagation(); void createDraft(item.id) }}>Restore as Draft</em>}</button>)}</section>
      </> : null}</section>
      <aside className="test-inspector">
        <header><span>Test & Inspector</span><h2>Production context</h2></header>
        {agent?.executable && <>
          <label>Course<select value={selectedCourse?.id ?? ""} onChange={(event) => setCourseId(event.target.value)}>{courses.map((item) => <option value={item.id} key={item.id}>{item.title}</option>)}</select></label>
          <label>Lecture<select value={selectedLecture?.id ?? ""} onChange={(event) => setLectureId(event.target.value)}>{selectedCourse?.lectures.map((item) => <option value={item.id} key={item.id}>Lecture {item.number} · {item.title}</option>)}</select></label>
          {(agent.agentId === "agent1" || agent.agentId === "agent1-boundary") && <label>{agent.agentId === "agent1-boundary" ? "Finalized source sentences" : "Transcript input"}<textarea value={transcript} onChange={(event) => setTranscript(event.target.value)} placeholder={agent.agentId === "agent1-boundary" ? "[S1] First finalized sentence.\n[S2] Next finalized sentence." : "Paste a sentence-safe classroom transcript chunk…"} /></label>}
          {agent.agentId === "agent1-boundary" && <label className="boundary-hard-max"><input type="checkbox" checked={hardMaxReached} onChange={(event) => setHardMaxReached(event.target.checked)} /> Simulate hard max reached</label>}
          <div className="test-actions"><button onClick={() => void resolveContext()} disabled={busy || !runnable}>Resolve Context</button><button className="primary" onClick={() => void runTest()} disabled={busy || !runnable}>Run Test</button></div>
          {resolved && <section className="context-blocks"><h3>Resolved Context · {resolved.promptVersionId}</h3>{resolved.blocks.map((block) => <details key={block.key} open={block.key === "system_prompt"}><summary><span>{block.label}{!block.sentToModel && " · tool accessible"}{block.scope && <em>{block.scope} · {block.versionId} · {block.source}</em>}</span><small>{block.tokenCount} tokens · {block.characterCount} chars</small></summary><pre>{block.content}</pre></details>)}</section>}
          {testRun && <section className="test-output"><h3>Output</h3><div><span>{testRun.model}</span><span>{testRun.latencyMs.toFixed(0)} ms</span><span>{testRun.validation.status}</span></div><pre>{testRun.output}</pre>{testRun.usage && <small>{JSON.stringify(testRun.usage)}</small>}</section>}
          <section className="test-history"><h3>Recent tests</h3>{history.length ? history.map((item) => <button key={item.id} onClick={() => { setTestRun(item); setResolved(item.resolvedContext) }}><span>{formatDate(item.createdAt)}</span><strong>{item.promptVersionId} · {item.model}</strong></button>) : <p>No test runs yet.</p>}</section>
        </>}
      </aside>
    </div>
  </main>
}
