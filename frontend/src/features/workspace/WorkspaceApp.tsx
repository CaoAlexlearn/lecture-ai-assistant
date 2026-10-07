import { lazy, Suspense, useCallback, useEffect, useMemo, useRef, useState, type FormEvent } from "react"
import { AgentLabPage } from "../agent-lab/AgentLabPage"
import { InstructionsPage } from "../instructions/InstructionsPage"
import { CourseLectureSelector } from "../lecture-map/components/CourseLectureSelector"
import type { CourseOption, LectureOption } from "../lecture-map/types/lectureMap"
import { LectureWorkspacePage } from "../lecture-workspace/components/LectureWorkspacePage"
import { workspaceApi } from "./workspaceApi"
import { lectureDestination, parseWorkspaceRoute, workspaceLinkDestination } from "./workspaceRouting"
import type { PreparationStatus, WorkspaceCourse, WorkspaceLecture, WorkspaceRoute } from "./types"
import { AppHeader } from "../ui/AppHeader"
import { Icon } from "../ui/Icon"
import { DisclosureMenu } from "../ui/DisclosureMenu"
import { StudyStages } from "../ui/StudyStages"
import { readRecentStudy, RECENT_STUDY_KEY, studyResume } from "./recentStudy"
import { parseScaffold, type KnowledgeScaffold } from "../knowledge-scaffold/scaffold"
import { PreparationKnowledgePreview } from "./PreparationKnowledgePreview"
import "./workspace.css"

const FormulaPage = lazy(() => import("../formulas/FormulaPage").then((module) => ({ default: module.FormulaPage })))

const STATUS_LABEL: Record<PreparationStatus, string> = {
  created: "Not prepared",
  material_uploaded: "Ready to prepare",
  preparing: "Preparing",
  ready: "Ready",
  outdated: "Outdated",
  failed: "Failed",
}

function courseOptions(courses: WorkspaceCourse[]): CourseOption[] {
  return courses.map((course) => ({
    id: course.id,
    title: course.title,
    lectures: course.lectures.map((lecture) => ({
      id: lecture.id,
      title: `Lecture ${lecture.number} · ${lecture.title}`,
      preparationStatus: lecture.preparationStatus,
      realtime: { courseId: course.id, lectureId: lecture.id },
      source: {
        overviewUrl: "",
        slidesPreparationUrl: "",
        scaffoldUrl: lecture.urls.scaffold ?? undefined,
        pdfUrl: lecture.urls.material ?? undefined,
        pdfFileNames: lecture.material ? [lecture.material.filename] : [],
      },
    })),
  }))
}

function WorkspaceHeader({ children }: { children?: React.ReactNode }) {
  return <AppHeader className={children ? "app-header--contextual" : ""}>{children}</AppHeader>
}

function EmptyState({ title, body, action }: { title: string; body: string; action?: React.ReactNode }) {
  return <section className="workspace-empty"><span>Workspace</span><h2>{title}</h2><p>{body}</p>{action}</section>
}

function Modal({ title, children, onClose }: { title: string; children: React.ReactNode; onClose: () => void }) {
  const dialogRef = useRef<HTMLDialogElement>(null)
  useEffect(() => {
    const previous = document.activeElement as HTMLElement | null
    const dialog = dialogRef.current!
    dialog.showModal()
    const initialFocus = dialog.querySelector<HTMLElement>("input, textarea") ?? dialog.querySelector<HTMLElement>("button")
    initialFocus?.focus()
    return () => { dialog.close(); previous?.focus() }
  }, [])
  return <dialog ref={dialogRef} className="workspace-modal" aria-label={title}
    onCancel={(event) => { event.preventDefault(); onClose() }}
    onKeyDown={(event) => {
      if (event.key !== "Tab") return
      const controls = Array.from(event.currentTarget.querySelectorAll<HTMLElement>("button:not(:disabled), input:not(:disabled), textarea:not(:disabled), select:not(:disabled), a[href], [tabindex='0']")).filter((element) => element.getClientRects().length)
      const first = controls[0]
      const last = controls[controls.length - 1]
      if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last?.focus() }
      else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first?.focus() }
    }}
    onClick={(event) => {
      if (event.target !== event.currentTarget) return
      const rect = event.currentTarget.getBoundingClientRect()
      if (event.clientX < rect.left || event.clientX > rect.right || event.clientY < rect.top || event.clientY > rect.bottom) onClose()
    }}>
    <header><h2>{title}</h2><button type="button" onClick={onClose} aria-label="Close"><Icon name="close" /></button></header>
    {children}
  </dialog>
}

function CoursesPage({ courses, refresh, navigate }: { courses: WorkspaceCourse[]; refresh: () => Promise<void>; navigate: (path: string) => void }) {
  const [creating, setCreating] = useState(false)
  const [search, setSearch] = useState("")
  const [editing, setEditing] = useState<WorkspaceCourse>()
  const [error, setError] = useState("")
  const createCourse = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault()
    const data = new FormData(event.currentTarget)
    try {
      const course = await workspaceApi.createCourse({ title: String(data.get("title") ?? ""), code: String(data.get("code") ?? ""), description: String(data.get("description") ?? "") })
      await refresh(); navigate(`/courses/${course.id}`)
    } catch (caught) { setError(caught instanceof Error ? caught.message : "Course could not be created.") }
  }
  const updateCourse = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault()
    if (!editing) return
    const data = new FormData(event.currentTarget)
    try {
      await workspaceApi.updateCourse(editing.id, { title: String(data.get("title") ?? ""), code: String(data.get("code") ?? ""), description: String(data.get("description") ?? "") })
      setEditing(undefined); setError(""); await refresh()
    } catch (caught) { setError(caught instanceof Error ? caught.message : "Course could not be updated.") }
  }
  const deleteCourse = async (course: WorkspaceCourse) => {
    if (!window.confirm(`Delete “${course.title}” and all ${course.lectures.length} lecture workspace${course.lectures.length === 1 ? "" : "s"}?\n\nThis permanently deletes course materials, preparation outputs, and class history.`)) return
    try { await workspaceApi.deleteCourse(course.id); setError(""); await refresh() }
    catch (caught) { setError(caught instanceof Error ? caught.message : "Course could not be deleted.") }
  }
  const query = search.trim().toLocaleLowerCase()
  const visibleCourses = courses.filter((course) => [course.title, course.code, course.description].filter(Boolean).join(" ").toLocaleLowerCase().includes(query))
  const newCourse = () => { setError(""); setCreating(true) }
  const resume = studyResume(courses, readRecentStudy())
  return <main className="workspace-page"><WorkspaceHeader /><div className="workspace-content">
    <div className="workspace-title-row">
      <div className="workspace-title"><h1>Your study workspace</h1><p>Pick up your latest lecture, or find a course below.</p></div>
      <button className="workspace-primary" onClick={newCourse}><Icon name="plus" />New Course</button>
    </div>
    {error && !creating && !editing && <p className="workspace-form-error workspace-page-error" role="alert">{error}</p>}
    {resume && !query && <section className="study-resume" aria-label="Continue learning">
      <div className="study-resume__content"><h2>{resume.source === "first" ? "Start studying" : "Continue learning"}</h2>
        <h3>{resume.course.title}</h3><p>Lecture {resume.lecture.number} · {resume.lecture.title}</p>
        <small>{resume.source === "visit" ? "Last opened on this device" : resume.source === "session" ? "From your latest class session" : "Your next lecture workspace"} · {STATUS_LABEL[resume.lecture.preparationStatus]}</small>
      </div>
      <div className="study-resume__actions"><a className="workspace-primary" href={resume.href}>{resume.lecture.preparationStatus !== "ready" || resume.href.endsWith("/prepare") ? "Continue preparation" : resume.href.includes("view=review") ? "Continue reviewing" : "Continue studying"}<Icon name="arrow" /></a><a href={`/courses/${encodeURIComponent(resume.course.id)}`} className="study-resume__course">View course</a></div>
    </section>}
    {courses.length > 0 && <div className="course-library-heading"><h2>Course library</h2><p>Your lectures, materials, and class history.</p></div>}
    {courses.length > 0 && <div className="course-library-toolbar">
      <label className="course-search"><Icon name="search" /><span className="ui-sr-only">Search courses</span><input type="search" placeholder="Search by course name or code…" value={search} onChange={(event) => setSearch(event.target.value)} />{search && <button onClick={() => setSearch("")} aria-label="Clear search"><Icon name="close" /></button>}</label>
      <span className="course-library-count" role="status">{query ? `${visibleCourses.length} of ${courses.length}` : courses.length} {courses.length === 1 ? "course" : "courses"}</span>
    </div>}
    {courses.length === 0 ? <EmptyState title="Your first course starts here" body="Add a course, upload your lecture slides, and build your study workspace." action={<button className="workspace-primary" onClick={newCourse}><Icon name="plus" />Create a Course</button>} /> : visibleCourses.length === 0 ? <EmptyState title="No matching courses" body="Try a different course name or code." action={<button className="workspace-secondary" onClick={() => setSearch("")}>Clear Search</button>} /> : <div className="course-grid">{visibleCourses.map((course) => {
      const ready = course.lectures.filter((lecture) => lecture.preparationStatus === "ready").length
      return <article className="course-card" key={course.id}>
        <div className="course-card__heading"><span className="course-card__symbol"><Icon name="book" /></span><span className="course-card__code">{course.code || "Course workspace"}</span></div>
        <a className="course-card__open" href={`/courses/${encodeURIComponent(course.id)}`}><h2>{course.title}</h2><p>{course.description || "Lecture slides, knowledge notes, and live interpretation."}</p></a>
        <div className="course-card__readiness"><span>{course.lectures.length} {course.lectures.length === 1 ? "lecture" : "lectures"}</span><span><Icon name="check" />{ready} prepared</span></div>
        <footer><a href={`/courses/${encodeURIComponent(course.id)}`} className="course-card__enter">Open Course<Icon name="arrow" /></a><DisclosureMenu className="course-card__menu" label={`Manage ${course.title}`} trigger={<Icon name="more" />}><button onClick={() => { setError(""); setEditing(course) }}>Edit course</button><button className="is-danger" onClick={() => void deleteCourse(course)}>Delete course</button></DisclosureMenu></footer>
      </article>
    })}</div>}
    {creating && <Modal title="New Course" onClose={() => setCreating(false)}><form className="workspace-form" onSubmit={createCourse}><label>Course Name<input name="title" required autoFocus /></label><label>Course Code <small>optional</small><input name="code" /></label><label>Description <small>optional</small><textarea name="description" rows={3} /></label>{error && <p className="workspace-form-error">{error}</p>}<footer><button type="button" onClick={() => setCreating(false)}>Cancel</button><button className="workspace-primary" type="submit">Create Course</button></footer></form></Modal>}{editing && <Modal title="Edit Course" onClose={() => setEditing(undefined)}><form className="workspace-form" onSubmit={updateCourse}><label>Course Name<input name="title" required autoFocus defaultValue={editing.title} /></label><label>Course Code <small>optional</small><input name="code" defaultValue={editing.code ?? ""} /></label><label>Description <small>optional</small><textarea name="description" rows={3} defaultValue={editing.description ?? ""} /></label>{error && <p className="workspace-form-error">{error}</p>}<footer><button type="button" onClick={() => setEditing(undefined)}>Cancel</button><button className="workspace-primary" type="submit">Save Changes</button></footer></form></Modal>}</div></main>
}

function CoursePage({ course, refresh, navigate }: { course: WorkspaceCourse; refresh: () => Promise<void>; navigate: (path: string) => void }) {
  const [creating, setCreating] = useState(false)
  const [editingCourse, setEditingCourse] = useState(false)
  const [editingLecture, setEditingLecture] = useState<WorkspaceLecture>()
  const [error, setError] = useState("")
  const createLecture = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault(); const data = new FormData(event.currentTarget)
    try { const lecture = await workspaceApi.createLecture(course.id, { number: String(data.get("number") ?? ""), title: String(data.get("title") ?? "") }); await refresh(); navigate(`/courses/${course.id}/lectures/${lecture.id}/prepare`) }
    catch (caught) { setError(caught instanceof Error ? caught.message : "Lecture could not be created.") }
  }
  const updateCourse = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault(); const data = new FormData(event.currentTarget)
    try { await workspaceApi.updateCourse(course.id, { title: String(data.get("title") ?? ""), code: String(data.get("code") ?? ""), description: String(data.get("description") ?? "") }); setEditingCourse(false); setError(""); await refresh() }
    catch (caught) { setError(caught instanceof Error ? caught.message : "Course could not be updated.") }
  }
  const updateLecture = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault(); if (!editingLecture) return; const data = new FormData(event.currentTarget)
    try { await workspaceApi.updateLecture(course.id, editingLecture.id, { number: String(data.get("number") ?? ""), title: String(data.get("title") ?? "") }); setEditingLecture(undefined); setError(""); await refresh() }
    catch (caught) { setError(caught instanceof Error ? caught.message : "Lecture could not be updated.") }
  }
  const deleteCourse = async () => {
    if (!window.confirm(`Delete “${course.title}” and all ${course.lectures.length} lecture workspace${course.lectures.length === 1 ? "" : "s"}?\n\nThis permanently deletes course materials, preparation outputs, and class history.`)) return
    try { await workspaceApi.deleteCourse(course.id); await refresh(); navigate("/courses") }
    catch (caught) { setError(caught instanceof Error ? caught.message : "Course could not be deleted.") }
  }
  const deleteLecture = async (lecture: WorkspaceLecture) => {
    if (!window.confirm(`Delete Lecture ${lecture.number} · ${lecture.title}?\n\nThis permanently deletes its material, preparation outputs, and class history.`)) return
    try { await workspaceApi.deleteLecture(course.id, lecture.id); setError(""); await refresh() }
    catch (caught) { setError(caught instanceof Error ? caught.message : "Lecture could not be deleted.") }
  }
  return <main className="workspace-page"><WorkspaceHeader><a className="workspace-header-link" href="/courses">All Courses</a></WorkspaceHeader><div className="workspace-content"><div className="workspace-title-row"><div className="workspace-title"><span>{course.code || "Course"}</span><h1>{course.title}</h1><p>{course.description}</p></div><div className="workspace-title-actions"><button className="workspace-secondary" onClick={() => { setError(""); setEditingCourse(true) }}>Edit Course</button><button className="workspace-danger" onClick={() => void deleteCourse()}>Delete Course</button></div></div>{error && !creating && !editingCourse && !editingLecture && <p className="workspace-form-error workspace-page-error">{error}</p>}<div className="lecture-list-heading"><h2>Lectures</h2><button className="workspace-primary" onClick={() => { setError(""); setCreating(true) }}>+ New Lecture</button></div>{course.lectures.length === 0 ? <EmptyState title="No lectures yet." body="Add the first lecture in this course." /> : <div className="lecture-list">{course.lectures.map((lecture) => <article className="lecture-row" key={lecture.id}><span className="lecture-number">{lecture.number}</span><div><h3>{lecture.title}</h3><p><strong>Preparation</strong> · {STATUS_LABEL[lecture.preparationStatus]}<br /><span>{lecture.activeSession ? `Current class · ${lecture.activeSession.status}` : lecture.lastSession ? `Last class · ${new Date(lecture.lastSession.endedAt ?? lecture.lastSession.startedAt ?? lecture.lastSession.createdAt).toLocaleDateString()} · ${lecture.lastSession.status}` : "No class sessions yet"}</span></p></div><span className={`lecture-status is-${lecture.preparationStatus}`}>{STATUS_LABEL[lecture.preparationStatus]}</span><div className="workspace-row-actions"><button onClick={() => navigate(lectureDestination(course.id, lecture.id, lecture.preparationStatus))}>{lecture.preparationStatus === "ready" ? "Open" : lecture.preparationStatus === "created" ? "Upload Material" : "Prepare"}</button><button onClick={() => { setError(""); setEditingLecture(lecture) }}>Edit</button><button className="is-danger" onClick={() => void deleteLecture(lecture)}>Delete</button></div></article>)}</div>}{creating && <Modal title="New Lecture" onClose={() => setCreating(false)}><form className="workspace-form" onSubmit={createLecture}><label>Lecture Number<input name="number" placeholder="02" required autoFocus /></label><label>Lecture Title<input name="title" placeholder="Application Layer" required /></label>{error && <p className="workspace-form-error">{error}</p>}<footer><button type="button" onClick={() => setCreating(false)}>Cancel</button><button className="workspace-primary" type="submit">Create Lecture</button></footer></form></Modal>}{editingCourse && <Modal title="Edit Course" onClose={() => setEditingCourse(false)}><form className="workspace-form" onSubmit={updateCourse}><label>Course Name<input name="title" required autoFocus defaultValue={course.title} /></label><label>Course Code <small>optional</small><input name="code" defaultValue={course.code ?? ""} /></label><label>Description <small>optional</small><textarea name="description" rows={3} defaultValue={course.description ?? ""} /></label>{error && <p className="workspace-form-error">{error}</p>}<footer><button type="button" onClick={() => setEditingCourse(false)}>Cancel</button><button className="workspace-primary" type="submit">Save Changes</button></footer></form></Modal>}{editingLecture && <Modal title="Edit Lecture" onClose={() => setEditingLecture(undefined)}><form className="workspace-form" onSubmit={updateLecture}><label>Lecture Number<input name="number" required autoFocus defaultValue={editingLecture.number} /></label><label>Lecture Title<input name="title" required defaultValue={editingLecture.title} /></label>{error && <p className="workspace-form-error">{error}</p>}<footer><button type="button" onClick={() => setEditingLecture(undefined)}>Cancel</button><button className="workspace-primary" type="submit">Save Changes</button></footer></form></Modal>}</div></main>
}

const STAGE_LABELS: Record<string, string> = {
  parsing_slides: "Parsing Slides",
  understanding_slides: "Understanding Slides",
  extracting_knowledge: "Extracting Knowledge",
  building_scaffold: "Building Scaffold",
  enriching_nodes: "Enriching Nodes",
  generating_derived_outputs: "Generating Derived Outputs",
  completed: "Completed",
}

function PreparePage({ courses, course, lecture, refresh, navigate }: { courses: WorkspaceCourse[]; course: WorkspaceCourse; lecture: WorkspaceLecture; refresh: () => Promise<void>; navigate: (path: string) => void }) {
  const [backend, setBackend] = useState<"qwen" | "codex">(lecture.preparation?.executionBackend ?? "qwen")
  useEffect(() => { setBackend(lecture.preparation?.executionBackend ?? "qwen") }, [course.id, lecture.id, lecture.preparation?.executionBackend])
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState("")
  const [view, setView] = useState<{ title: string; content: string }>()
  const [scaffold, setScaffold] = useState<KnowledgeScaffold>()
  useEffect(() => {
    if (lecture.preparationStatus !== "preparing") return
    const timer = window.setInterval(() => void refresh(), 2000)
    return () => window.clearInterval(timer)
  }, [lecture.preparationStatus, refresh])
  useEffect(() => {
    if (lecture.preparationStatus !== "ready" || !lecture.urls.scaffold) { setScaffold(undefined); return }
    let active = true
    fetch(lecture.urls.scaffold).then((response) => {
      if (!response.ok) throw new Error(`Scaffold unavailable (${response.status})`)
      return response.json().then(parseScaffold)
    }).then((value) => { if (active) setScaffold(value) }).catch((caught: unknown) => { if (active) setError(caught instanceof Error ? caught.message : "Knowledge tree could not be opened.") })
    return () => { active = false }
  }, [lecture.preparationStatus, lecture.urls.scaffold])
  const upload = async (file: File) => {
    if (lecture.prepared_at && !window.confirm("Replacing the course material will make the existing preparation outdated. Continue?")) return
    setBusy(true); setError("")
    try { await workspaceApi.uploadMaterial(course.id, lecture.id, file); await refresh() }
    catch (caught) { setError(caught instanceof Error ? caught.message : "Upload failed.") }
    finally { setBusy(false) }
  }
  const prepare = async () => {
    setBusy(true); setError("")
    try { await workspaceApi.runPreparation(course.id, lecture.id, backend); await refresh() }
    catch (caught) { setError(caught instanceof Error ? caught.message : "Preparation failed.") }
    finally { setBusy(false) }
  }
  const openOutput = async (key: keyof WorkspaceLecture["outputs"], title: string) => {
    const url = lecture.urls[key]
    if (!url) return
    try {
      const response = await fetch(url)
      if (!response.ok) throw new Error(`Output unavailable (${response.status}).`)
      const raw = await response.text()
      setView({ title, content: key === "hotwords" || key === "scaffold" ? JSON.stringify(JSON.parse(raw), null, 2) : raw })
    } catch (caught) { setError(caught instanceof Error ? caught.message : "Output could not be opened.") }
  }
  const options = courseOptions(courses)
  const selectedCourse = options.find((item) => item.id === course.id)!
  const selectedLecture = selectedCourse.lectures.find((item) => item.id === lecture.id)!
  const ready = lecture.preparationStatus === "ready"
  const outputs: Array<[keyof WorkspaceLecture["outputs"], string, string]> = [
    ["scaffold", "Knowledge Scaffold", "lecture_scaffold.json"],
    ["markdown", "Readable Scaffold", "lecture_scaffold.md"],
    ["hotwords", "Hotwords", "hotwords.json"],
    ["asr_context", "ASR Context", "asr_context.txt"],
  ]
  return <main className="workspace-page"><WorkspaceHeader><CourseLectureSelector courses={options} selectedCourse={selectedCourse} selectedLecture={selectedLecture} onCourseChange={(id) => { const targetCourse = courses.find((item) => item.id === id); const target = targetCourse?.lectures[0]; navigate(targetCourse && target ? lectureDestination(targetCourse.id, target.id, target.preparationStatus) : `/courses/${id}`) }} onLectureChange={(id) => { const target = course.lectures.find((item) => item.id === id); if (target) navigate(lectureDestination(course.id, id, target.preparationStatus)) }} disabled={lecture.preparationStatus === "preparing"} /><span className={`lecture-status is-${lecture.preparationStatus}`}>{STATUS_LABEL[lecture.preparationStatus]}</span><StudyStages courseId={course.id} lectureId={lecture.id} active="prepare" ready={ready} /></WorkspaceHeader><div className="prepare-content"><a className="workspace-back" href={`/courses/${course.id}`}>← {course.title}</a><div className="workspace-title"><span>Lecture {lecture.number}</span><h1>{lecture.title}</h1><p>Build a knowledge scaffold from the lecture slides before class.</p></div>
  {(error || lecture.preparation?.error) && <div className="prepare-error" role="alert"><strong>Preparation failed</strong><p>{error || lecture.preparation?.error}</p></div>}
  <section className="prepare-card"><header><div><span>01</span><h2>Course Material</h2></div>{lecture.material && <label className="workspace-secondary">Replace<input type="file" accept=".pdf,.pptx,application/pdf" hidden onChange={(event) => { const file = event.target.files?.[0]; if (file) void upload(file) }} /></label>}</header>{lecture.material ? <div className="material-file"><span>PDF</span><div><strong>{lecture.material.filename}</strong><small>{(lecture.material.size_bytes / 1024 / 1024).toFixed(1)} MB · Uploaded {new Date(lecture.material.uploaded_at).toLocaleDateString()}</small></div>{lecture.urls.material && <a href={lecture.urls.material} target="_blank" rel="noreferrer">Preview</a>}</div> : <EmptyState title="No material uploaded" body="Upload your lecture slides to begin preparation." action={<label className="workspace-primary">Upload PPT / PDF<input type="file" accept=".pdf,.pptx,application/pdf" hidden onChange={(event) => { const file = event.target.files?.[0]; if (file) void upload(file) }} /></label>} />}{lecture.preparationStatus === "outdated" && <p className="prepare-warning">Preparation is outdated because the course material has changed.</p>}</section>
  <section className="prepare-card"><header><div><span>02</span><h2>PreAgent</h2></div></header>
  <div className="prepare-backend"><label htmlFor="preparation-backend">整理方式</label><select id="preparation-backend" value={backend} disabled={busy || lecture.preparationStatus === "preparing"} onChange={(event) => setBackend(event.target.value as "qwen" | "codex")}><option value="qwen">Qwen API</option><option value="codex">Codex · ChatGPT 账户额度</option></select><p>{backend === "codex" ? "使用本机已登录的 Codex 整理文字和图片，与日常 Codex 使用共享额度。额度不足时会提示失败，不会自动切换到 Qwen。课件解析仍使用 MinerU。" : "使用 Agent Lab 当前配置的 Qwen API 模型整理课件。"}</p></div>
  {lecture.preparationStatus === "preparing" ? <div className="preparing-state"><i /><div><strong>{STAGE_LABELS[lecture.preparationStage ?? ""] ?? "Preparing lecture"}</strong><p>Building the knowledge scaffold from bounded slide and node tasks.</p></div></div> : <div className="prepare-action"><div><strong>{lecture.preparationStatus === "created" ? "Material required" : ready ? "Knowledge Scaffold complete" : lecture.preparationStatus === "outdated" ? "Preparation is outdated" : lecture.preparationStatus === "failed" ? "Preparation failed" : "Ready to prepare"}</strong><p>{lecture.preparationStatus === "created" ? "Upload course material before running PreAgent." : "PreAgent extracts a knowledge tree, slide provenance, and ASR vocabulary."}</p></div><span className="prepare-action-buttons">{ready && <><button className="workspace-secondary" onClick={() => navigate(`/courses/${course.id}/lectures/${lecture.id}/formulas`)}>本讲公式 →</button><button className="workspace-primary" onClick={() => navigate(`/courses/${course.id}/lectures/${lecture.id}/live`)}>Open Study Workspace →</button></>}<button className={ready ? "workspace-secondary" : "workspace-primary"} disabled={!lecture.material || busy} onClick={() => void prepare()}>{busy ? "Starting…" : ready || lecture.preparationStatus === "outdated" ? "Re-run PreAgent" : "Run PreAgent"}</button></span></div>}</section>
  {ready && scaffold && <section className="prepare-card"><header><div><span>03</span><h2>Knowledge Tree</h2></div></header><PreparationKnowledgePreview key={`${course.id}:${lecture.id}`} scaffold={scaffold} materialUrl={lecture.urls.material} /></section>}
  <section className="prepare-card"><header><div><span>04</span><h2>Outputs</h2></div>{ready && <button className="workspace-primary" onClick={() => navigate(`/courses/${course.id}/lectures/${lecture.id}/live`)}>Open Live Workspace</button>}</header><div className="output-list">{outputs.map(([key, title, file]) => <div className="output-row" key={key}><span className={lecture.outputs[key] ? "is-ready" : ""}>{lecture.outputs[key] ? "✓" : "—"}</span><div><strong>{title}</strong><small>{file}</small></div><button disabled={!lecture.outputs[key]} onClick={() => void openOutput(key, title)}>View</button></div>)}</div></section>
  </div>{view && <Modal title={view.title} onClose={() => setView(undefined)}><pre className="output-preview">{view.content}</pre></Modal>}</main>
}

export function WorkspaceApp() {
  const [route, setRoute] = useState<WorkspaceRoute>(() => parseWorkspaceRoute(window.location.pathname))
  const [courses, setCourses] = useState<WorkspaceCourse[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState("")
  const navigate = useCallback((path: string, replace = false) => {
    const destination = new URL(path, window.location.href)
    if (replace || destination.href !== window.location.href) window.history[replace ? "replaceState" : "pushState"]({}, "", destination)
    setRoute(parseWorkspaceRoute(destination.pathname))
  }, [])
  const refresh = async () => { try { setCourses(await workspaceApi.listCourses()); setError("") } catch (caught) { setError(caught instanceof Error ? caught.message : "Workspace could not be loaded.") } finally { setLoading(false) } }
  useEffect(() => { void refresh(); const pop = () => setRoute(parseWorkspaceRoute(window.location.pathname)); window.addEventListener("popstate", pop); return () => window.removeEventListener("popstate", pop) }, [])
  useEffect(() => {
    const followLink = (event: MouseEvent) => {
      const anchor = event.target instanceof Element ? event.target.closest<HTMLAnchorElement>("a[href]") : null
      if (!anchor) return
      const path = workspaceLinkDestination({ href: anchor.href, target: anchor.target, download: anchor.hasAttribute("download"), button: event.button, defaultPrevented: event.defaultPrevented, metaKey: event.metaKey, ctrlKey: event.ctrlKey, shiftKey: event.shiftKey, altKey: event.altKey }, window.location.href)
      if (path === undefined) return
      event.preventDefault()
      navigate(path)
    }
    document.addEventListener("click", followLink)
    return () => document.removeEventListener("click", followLink)
  }, [navigate])
  useEffect(() => { document.title = route.page === "agent-lab" ? "Agent Lab · Classroom Assistant" : route.page === "instructions" ? "Instructions · Classroom Assistant" : route.page === "formulas" ? "本讲公式 · Classroom Assistant" : "Courses · Classroom Assistant" }, [route.page])
  useEffect(() => { if (!loading && window.location.pathname === "/lecture-workspace.html") { const course = courses.find((item) => item.lectures.some((lecture) => lecture.preparationStatus === "ready")); const lecture = course?.lectures.find((item) => item.preparationStatus === "ready"); if (course && lecture) navigate(`/courses/${course.id}/lectures/${lecture.id}/live`, true); else navigate("/courses", true) } }, [courses, loading])
  const selected = useMemo(() => { if (route.page !== "course" && route.page !== "prepare" && route.page !== "live" && route.page !== "formulas") return {}; const course = courses.find((item) => item.id === route.courseId); const lecture = route.page === "prepare" || route.page === "live" || route.page === "formulas" ? course?.lectures.find((item) => item.id === route.lectureId) : undefined; return { course, lecture } }, [courses, route])
  useEffect(() => {
    if ((route.page === "live" || route.page === "prepare") && selected.course && selected.lecture) {
      try { localStorage.setItem(RECENT_STUDY_KEY, JSON.stringify({ courseId: selected.course.id, lectureId: selected.lecture.id, page: route.page, review: route.page === "live" && new URLSearchParams(window.location.search).get("view") === "review" })) } catch { /* Continue without local preferences. */ }
    }
  }, [route, selected.course, selected.lecture])
  useEffect(() => {
    if (route.page === "live" && selected.course && selected.lecture && selected.lecture.preparationStatus !== "ready") {
      navigate(`/courses/${selected.course.id}/lectures/${selected.lecture.id}/prepare`, true)
    }
  }, [route, selected.course, selected.lecture])
  if (loading) return <main className="workspace-page"><WorkspaceHeader /><EmptyState title="Opening workspace…" body="Loading courses and lectures." /></main>
  if (error) return <main className="workspace-page"><WorkspaceHeader /><EmptyState title="Workspace unavailable" body={error} action={<button className="workspace-primary" onClick={() => void refresh()}>Retry</button>} /></main>
  if (route.page === "agent-lab") return <AgentLabPage courses={courses} />
  if (route.page === "instructions") return <InstructionsPage courses={courses} />
  if (route.page === "courses") return <CoursesPage courses={courses} refresh={refresh} navigate={navigate} />
  if (route.page === "course" && selected.course) return <CoursePage course={selected.course} refresh={refresh} navigate={navigate} />
  if (route.page === "prepare" && selected.course && selected.lecture) return <PreparePage courses={courses} course={selected.course} lecture={selected.lecture} refresh={refresh} navigate={navigate} />
  if (route.page === "formulas" && selected.course && selected.lecture) return <Suspense fallback={<p role="status">正在打开公式页面…</p>}><FormulaPage course={selected.course} lecture={selected.lecture} /></Suspense>
  if (route.page === "live" && selected.course && selected.lecture) {
    if (selected.lecture.preparationStatus !== "ready") return null
    const options = courseOptions(courses)
    return <LectureWorkspacePage mode={new URLSearchParams(window.location.search).get("view") === "review" ? "review" : "class"} courses={options} courseId={selected.course.id} lectureId={selected.lecture.id} activeSession={selected.lecture.activeSession} lastSession={selected.lecture.lastSession} onSessionChange={() => void refresh()} onNavigateLecture={(course, lecture) => navigate(lectureDestination(course.id, lecture.id, lecture.preparationStatus!) + (new URLSearchParams(window.location.search).get("view") === "review" && lecture.preparationStatus === "ready" ? "?view=review" : ""))} />
  }
  return <main className="workspace-page"><WorkspaceHeader /><EmptyState title="Workspace not found" body="The requested course or lecture does not exist." action={<button className="workspace-primary" onClick={() => navigate("/courses")}>Return to Courses</button>} /></main>
}
