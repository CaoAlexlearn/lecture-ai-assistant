import { AppHeader } from "../ui/AppHeader"
import { useEffect, useMemo, useState } from "react"
import type { WorkspaceCourse } from "../workspace/types"
import { instructionsApi } from "./instructionsApi"
import type { InstructionDocument, InstructionPreview, InstructionVersion } from "./types"
import "./instructions.css"

type Props = { courses: WorkspaceCourse[] }

function formatDate(value: string): string {
  return new Date(value).toLocaleString([], { dateStyle: "medium", timeStyle: "short" })
}

export function InstructionsPage({ courses }: Props) {
  const [documents, setDocuments] = useState<InstructionDocument[]>([])
  const [document, setDocument] = useState<InstructionDocument>()
  const [version, setVersion] = useState<InstructionVersion>()
  const [content, setContent] = useState("")
  const [note, setNote] = useState("")
  const [agentId, setAgentId] = useState("agent1")
  const [courseId, setCourseId] = useState(courses[0]?.id ?? "")
  const course = courses.find((item) => item.id === courseId) ?? courses[0]
  const [lectureId, setLectureId] = useState(course?.lectures[0]?.id ?? "")
  const [preview, setPreview] = useState<InstructionPreview>()
  const [diff, setDiff] = useState("")
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState("")

  const selectVersion = (next?: InstructionVersion) => {
    setVersion(next)
    setContent(next?.content ?? "")
    setNote(next?.note ?? "")
    setDiff("")
  }

  const loadDocument = async (id: string) => {
    const next = await instructionsApi.getDocument(id)
    setDocument(next)
    const selected = next.versions?.find((item) => item.status === "draft")
      ?? next.versions?.find((item) => item.id === next.activeVersionId)
      ?? next.versions?.[0]
    selectVersion(selected)
    setError("")
  }

  const loadDocuments = async () => {
    const items = await instructionsApi.listDocuments()
    setDocuments(items)
    if (!document || !items.some((item) => item.id === document.id)) {
      if (items[0]) await loadDocument(items[0].id)
    }
  }

  useEffect(() => {
    loadDocuments().catch((caught) => setError(caught instanceof Error ? caught.message : "Instructions could not be loaded."))
  }, [])

  useEffect(() => {
    const nextCourse = courses.find((item) => item.id === courseId) ?? courses[0]
    if (nextCourse && !nextCourse.lectures.some((item) => item.id === lectureId)) setLectureId(nextCourse.lectures[0]?.id ?? "")
  }, [courseId, courses, lectureId])

  const createDraft = async (sourceVersionId?: string) => {
    if (!document) return
    setBusy(true); setError("")
    try { await instructionsApi.createDraft(document.id, sourceVersionId); await loadDocument(document.id) }
    catch (caught) { setError(caught instanceof Error ? caught.message : "Draft could not be created.") }
    finally { setBusy(false) }
  }

  const save = async () => {
    if (!document || !version) return
    setBusy(true); setError("")
    try { await instructionsApi.saveDraft(document.id, version.id, content, version.updatedAt, note); await loadDocument(document.id) }
    catch (caught) { setError(caught instanceof Error ? caught.message : "Draft could not be saved.") }
    finally { setBusy(false) }
  }

  const activate = async () => {
    if (!document || !version || !window.confirm(`Activate ${document.title} ${version.id}?\n\nOnly new Production runs will use this version.`)) return
    setBusy(true); setError("")
    try { await instructionsApi.activate(document.id, version.id); await loadDocument(document.id); await loadDocuments() }
    catch (caught) { setError(caught instanceof Error ? caught.message : "Version could not be activated.") }
    finally { setBusy(false) }
  }

  const compare = async () => {
    if (!document?.activeVersionId || !version) return
    try { setDiff((await instructionsApi.compare(document.id, document.activeVersionId, version.id)).diff || "No differences.") }
    catch (caught) { setError(caught instanceof Error ? caught.message : "Versions could not be compared.") }
  }

  const resolvePreview = async () => {
    setBusy(true); setError("")
    try {
      const versionOverrides = document && version ? { [document.id]: version.id } : undefined
      setPreview(await instructionsApi.preview({ agentId, courseId, lectureId, versionOverrides }))
    }
    catch (caught) { setError(caught instanceof Error ? caught.message : "Context could not be previewed.") }
    finally { setBusy(false) }
  }

  const grouped = useMemo(() => courses.map((item) => ({
    course: item,
    courseDocument: documents.find((doc) => doc.scope === "course" && doc.courseId === item.id),
    lectures: item.lectures.map((lecture) => ({
      lecture,
      document: documents.find((doc) => doc.scope === "lecture" && doc.courseId === item.id && doc.lectureId === lecture.id),
    })),
  })), [courses, documents])
  const active = document?.versions?.find((item) => item.id === document.activeVersionId)
  const editable = version?.status === "draft"
  const dirty = Boolean(editable && (content !== (version?.content ?? "") || note !== (version?.note ?? "")))
  const canPreview = Boolean(course && lectureId && !dirty)

  return <main className="instructions-page">
    <AppHeader active="instructions" subtitle="Course instructions"><small>{document?.activeVersionId ? `Active ${document.activeVersionId}` : "No active version"}</small></AppHeader>
    {error && <div className="instructions-error" role="alert">{error}</div>}
    <div className="instructions-layout">
      <aside className="scope-navigator"><span>Scope Navigator</span><button className={document?.id === "faculty" ? "is-selected" : ""} onClick={() => void loadDocument("faculty")}><strong>Faculty</strong><small>All courses</small></button><button className={document?.id === "user" ? "is-selected" : ""} onClick={() => void loadDocument("user")}><strong>User</strong><small>All courses</small></button><h2>Courses</h2>{grouped.map(({ course: item, courseDocument, lectures }) => <section key={item.id}><strong>{item.title}</strong>{courseDocument && <button className={document?.id === courseDocument.id ? "is-selected" : ""} onClick={() => void loadDocument(courseDocument.id)}>Course Instructions</button>}{lectures.map(({ lecture, document: lectureDocument }) => lectureDocument && <button className={document?.id === lectureDocument.id ? "is-selected" : ""} key={lecture.id} onClick={() => void loadDocument(lectureDocument.id)}>Lecture {lecture.number} · {lecture.title}</button>)}</section>)}</aside>
      <section className="instruction-editor">{document && <><header><div><span>{document.scope} scope</span><h1>{document.title}</h1><p>{document.subtitle}</p><strong>Applies to: {document.appliesTo}</strong></div>{!document.versions?.some((item) => item.status === "draft") && <button onClick={() => void createDraft()} disabled={busy}>Create Draft</button>}</header><div className="instruction-version-bar"><div><small>Active</small><strong>{document.activeVersionId ?? "None"}</strong></div><div><small>Viewing</small><strong>{version ? `${version.id} · ${version.status}${dirty ? " · unsaved" : ""}` : "No version"}</strong></div></div>{version ? <><textarea value={content} readOnly={!editable} onChange={(event) => setContent(event.target.value)} aria-label="Instruction content" spellCheck={false} /><label>Version note<input value={note} readOnly={!editable} onChange={(event) => setNote(event.target.value)} /></label><div className="instruction-actions">{editable && <><button onClick={() => { setContent(version.content ?? ""); setNote(version.note ?? "") }}>Reset Draft</button><button onClick={() => void save()} disabled={busy || !dirty}>Save Draft</button><button className="primary" onClick={() => void activate()} disabled={busy || dirty}>Activate</button></>}<button onClick={() => void compare()} disabled={!active || !version || active.id === version.id}>Compare with Active</button></div></> : <div className="instruction-empty"><span>No active instructions</span><p>This scope is omitted from model context until you create and activate a version.</p><button onClick={() => void createDraft()}>Create First Draft</button></div>}{diff && <pre className="instruction-diff">{diff}</pre>}<section className="instruction-history"><h2>Version History</h2>{document.versions?.length ? document.versions.map((item) => <button className={item.id === version?.id ? "is-selected" : ""} key={item.id} onClick={() => selectVersion(item)}><strong>{item.id} · {item.status}</strong><small>{formatDate(item.updatedAt)}</small>{item.status === "archived" && <em onClick={(event) => { event.stopPropagation(); void createDraft(item.id) }}>Restore as Draft</em>}</button>) : <p>No versions yet.</p>}</section></>}</section>
      <aside className="instruction-preview"><header><span>Context Preview</span><h2>Resolved Instructions</h2><p>No model call is made.</p></header><label>Agent<select value={agentId} onChange={(event) => setAgentId(event.target.value)}><option value="agent1">Agent1</option><option value="pre-agent">Pre-Agent</option></select></label><label>Course<select value={course?.id ?? ""} onChange={(event) => setCourseId(event.target.value)}>{courses.map((item) => <option value={item.id} key={item.id}>{item.title}</option>)}</select></label><label>Lecture<select value={lectureId} onChange={(event) => setLectureId(event.target.value)}>{course?.lectures.map((item) => <option value={item.id} key={item.id}>Lecture {item.number} · {item.title}</option>)}</select></label><button className="primary" onClick={() => void resolvePreview()} disabled={busy || !canPreview}>Preview Resolved Instructions</button>{preview && <><div className="instruction-precedence"><small>Load order</small><strong>{preview.loadOrder.join(" → ") || "No active instructions"}</strong><small>Conflict precedence</small><strong>{preview.precedence.join(" > ")}</strong></div><section className="instruction-blocks">{preview.blocks.length ? preview.blocks.map((block) => <details key={block.key} open><summary><span>{block.label}<small>{block.versionId} · {block.source}</small></span><em>{block.tokenCount} tokens</em></summary><pre>{block.content}</pre></details>) : <p>No active instructions for this context.</p>}</section></>}</aside>
    </div>
  </main>
}
