import { AppHeader } from "../../ui/AppHeader"
import { Icon } from "../../ui/Icon"
import { StudyStages } from "../../ui/StudyStages"
import { paneWidth, transcriptHeight } from "../workspaceLayout"
import {
  useEffect,
  useMemo,
  useRef,
  useState,
  type CSSProperties,
  type KeyboardEvent as ReactKeyboardEvent,
  type PointerEvent as ReactPointerEvent,
} from "react"
import { CourseLectureSelector } from "../../lecture-map/components/CourseLectureSelector"
import { COURSE_REGISTRY, findLectureForPdf } from "../../lecture-map/data/courseRegistry"
import type { CourseOption, LectureOption } from "../../lecture-map/types/lectureMap"
import { KnowledgeTree } from "../../knowledge-scaffold/KnowledgeTree"
import { KnowledgeNodeInspector } from "../../knowledge-scaffold/KnowledgeNodeInspector"
import { firstContentNodeId, loadKnowledgeScaffold, sourcePages, type KnowledgeScaffold } from "../../knowledge-scaffold/scaffold"
import { LiveClassDock } from "../../live-classroom/LiveClassDock"
import { useEnrichment } from "../../classroom-enrichment/useEnrichment"
import { ClassroomEnrichment } from "../../classroom-enrichment/ClassroomEnrichment"
import type { ClassSession } from "../../workspace/types"
import { PdfPane } from "./PdfPane"
import "../../lecture-map/lecture-map.css"
import "../../knowledge-scaffold/knowledge-scaffold.css"
import "../lecture-workspace.css"

const DEFAULT_COURSE = COURSE_REGISTRY[0]
const DEFAULT_LECTURE = DEFAULT_COURSE.lectures[0]
type Props = {
  mode?: "class" | "review";
  courses?: CourseOption[]
  courseId?: string
  lectureId?: string
  onNavigateLecture?: (course: CourseOption, lecture: LectureOption) => void
  activeSession?: ClassSession | null
  lastSession?: ClassSession | null
  onSessionChange?: () => void
}

export function LectureWorkspacePage({
  mode = "class",
  courses = COURSE_REGISTRY,
  courseId = DEFAULT_COURSE.id,
  lectureId = DEFAULT_LECTURE.id,
  onNavigateLecture,
  activeSession,
  lastSession,
  onSessionChange,
}: Props) {
  const [selectedCourseId, setSelectedCourseId] = useState(courseId)
  const [selectedLectureId, setSelectedLectureId] = useState(lectureId)
  const [scaffold, setScaffold] = useState<KnowledgeScaffold>()
  const [selectedNodeId, setSelectedNodeId] = useState<string>()
  const [error, setError] = useState("")
  const [uploadedPdf, setUploadedPdf] = useState<{ name: string; url: string; matched: boolean }>()
  const [pageNumber, setPageNumber] = useState(1)
  const [pdfExpanded, setPdfExpanded] = useState(false)
  const [sessionLocked, setSessionLocked] = useState(false)
  const [liveExpanded, setLiveExpanded] = useState(false)
  const [treeHidden, setTreeHidden] = useState(false)
  const [detailHidden, setDetailHidden] = useState(mode === "review")
  const [detailTab, setDetailTab] = useState<"knowledge" | "notes">("knowledge")
  const [mobileView, setMobileView] = useState<"pdf" | "tree" | "notes" | "live">(mode === "review" ? "notes" : "pdf")
  const [narrow, setNarrow] = useState(() => window.matchMedia("(max-width: 1100px)").matches)
  const resizeCleanup = useRef<(() => void) | undefined>(undefined)
  const [treeWidth, setTreeWidth] = useState(220)
  const [detailWidth, setDetailWidth] = useState(320)
  const [liveHeight, setLiveHeight] = useState(212)
  const workspaceRef = useRef<HTMLDivElement>(null)

  useEffect(() => {
    const media = window.matchMedia("(max-width: 1100px)")
    const update = () => setNarrow(media.matches)
    media.addEventListener("change", update)
    return () => media.removeEventListener("change", update)
  }, [])
  useEffect(() => {
    setDetailTab("knowledge")
    setDetailHidden(mode === "review")
    setMobileView(mode === "review" ? "notes" : "pdf")
    setDetailWidth(320)
  }, [mode])
  useEffect(() => () => resizeCleanup.current?.(), [])
  useEffect(() => {
    const workspace = workspaceRef.current
    if (!workspace || narrow) return
    const fit = () => {
      const styles = getComputedStyle(workspace)
      const width = workspace.clientWidth - parseFloat(styles.paddingLeft) - parseFloat(styles.paddingRight)
      const detail = paneWidth(detailWidth, width, treeHidden || pdfExpanded ? 0 : Math.min(treeWidth, 360), 280)
      const tree = paneWidth(treeWidth, width, detailHidden || pdfExpanded ? 0 : detail, 180, 360)
      setDetailWidth(detail)
      setTreeWidth(tree)
      const height = workspace.clientHeight - parseFloat(styles.paddingTop) - parseFloat(styles.paddingBottom)
      setLiveHeight(transcriptHeight(liveHeight, height))
    }
    const observer = new ResizeObserver(fit)
    observer.observe(workspace)
    return () => observer.disconnect()
  }, [narrow, treeWidth, detailWidth, liveHeight, treeHidden, detailHidden, pdfExpanded])

  const selectedCourse = courses.find((course) => course.id === selectedCourseId) ?? courses[0] ?? DEFAULT_COURSE
  const selectedLecture = selectedCourse.lectures.find((lecture) => lecture.id === selectedLectureId)
    ?? selectedCourse.lectures[0]
  const enrichment = useEnrichment(selectedLecture.realtime.courseId, selectedLecture.realtime.lectureId, lastSession)

  useEffect(() => {
    setSelectedCourseId(courseId)
    setSelectedLectureId(lectureId)
  }, [courseId, lectureId])

  useEffect(() => {
    let current = true
    setScaffold(undefined)
    setSelectedNodeId(undefined)
    setError("")
    setPageNumber(1)
    loadKnowledgeScaffold(selectedLecture)
      .then((next) => {
        if (!current) return
        setScaffold(next)
        const firstId = firstContentNodeId(next)
        setSelectedNodeId(firstId)
        if (firstId) setPageNumber(sourcePages(next.nodes[firstId])[0] ?? 1)
      })
      .catch((caught: unknown) => {
        if (current) setError(caught instanceof Error ? caught.message : "Lecture data could not be loaded.")
      })
    return () => { current = false }
  }, [selectedCourse.id, selectedLecture.id, selectedLecture.source.scaffoldUrl])

  useEffect(() => () => {
    if (uploadedPdf) URL.revokeObjectURL(uploadedPdf.url)
  }, [uploadedPdf])

  useEffect(() => {
    if (!pdfExpanded) return
    const restore = (event: KeyboardEvent) => {
      if (event.key === "Escape") setPdfExpanded(false)
    }
    window.addEventListener("keydown", restore)
    return () => window.removeEventListener("keydown", restore)
  }, [pdfExpanded])

  const selectCourse = (courseId: string) => {
    const course = courses.find((candidate) => candidate.id === courseId)
    if (!course?.lectures.length) return
    setSelectedCourseId(course.id)
    setSelectedLectureId(course.lectures[0].id)
    setUploadedPdf(undefined)
    onNavigateLecture?.(course, course.lectures[0])
  }

  const selectLecture = (lectureId: string) => {
    const lecture = selectedCourse.lectures.find((candidate) => candidate.id === lectureId)
    if (!lecture) return
    setSelectedLectureId(lectureId)
    setUploadedPdf(undefined)
    onNavigateLecture?.(selectedCourse, lecture)
  }

  const uploadPdf = (file: File) => {
    const matched = findLectureForPdf(file.name)
    const url = URL.createObjectURL(file)
    setUploadedPdf((previous) => {
      if (previous) URL.revokeObjectURL(previous.url)
      return { name: file.name, url, matched: Boolean(matched) }
    })
    if (matched) {
      setSelectedCourseId(matched.course.id)
      setSelectedLectureId(matched.lecture.id)
    }
  }

  const pdf = useMemo(() => ({
    name: uploadedPdf?.name ?? selectedLecture.source.pdfFileNames?.[0] ?? "Lecture PDF",
    url: uploadedPdf?.url ?? selectedLecture.source.pdfUrl,
  }), [selectedLecture, uploadedPdf])

  const selectNode = (id: string) => {
    const node = scaffold?.nodes[id]
    if (!node) return
    setSelectedNodeId(id)
    if (mode !== "review") setDetailHidden(false)
    if (narrow) setMobileView(mode === "review" ? "notes" : "pdf")
    const firstPage = sourcePages(node)[0]
    if (firstPage) setPageNumber(firstPage)
  }

  const updatePane = (kind: "tree" | "detail" | "live", value: number) => {
    const workspace = workspaceRef.current
    if (!workspace) return
    const bounds = workspace.getBoundingClientRect()
    const styles = window.getComputedStyle(workspace)
    const available = bounds.width - parseFloat(styles.paddingLeft) - parseFloat(styles.paddingRight)
    if (kind === "tree") setTreeWidth(paneWidth(value, available, detailHidden ? 0 : detailWidth, 180, 360))
    else if (kind === "detail") setDetailWidth(paneWidth(value, available, treeHidden ? 0 : treeWidth, 280))
    else setLiveHeight(transcriptHeight(value, bounds.height - parseFloat(styles.paddingTop) - parseFloat(styles.paddingBottom)))
  }

  const beginResize = (kind: "tree" | "detail" | "live", event: ReactPointerEvent<HTMLButtonElement>) => {
    event.preventDefault()
    event.currentTarget.focus()
    resizeCleanup.current?.()
    const startX = event.clientX
    const startY = event.clientY
    const initial = kind === "tree" ? treeWidth : kind === "detail" ? detailWidth : liveHeight
    const resize = (next: PointerEvent) => updatePane(kind, initial + (kind === "tree" ? next.clientX - startX : kind === "detail" ? startX - next.clientX : startY - next.clientY))
    const finish = () => {
      window.removeEventListener("pointermove", resize)
      window.removeEventListener("pointerup", finish)
      window.removeEventListener("pointercancel", finish)
      document.body.classList.remove("is-resizing-workspace")
      resizeCleanup.current = undefined
    }
    resizeCleanup.current = finish
    document.body.classList.add("is-resizing-workspace")
    window.addEventListener("pointermove", resize)
    window.addEventListener("pointerup", finish, { once: true })
    window.addEventListener("pointercancel", finish, { once: true })
  }

  const resizeWithKeyboard = (kind: "tree" | "detail" | "live", event: ReactKeyboardEvent<HTMLButtonElement>) => {
    const direction = kind === "tree" ? 1 : -1
    const delta = event.key === "ArrowLeft" || event.key === "ArrowUp" ? -24 : event.key === "ArrowRight" || event.key === "ArrowDown" ? 24 : 0
    if (!delta && event.key !== "Home") return
    event.preventDefault()
    const initial = kind === "tree" ? treeWidth : kind === "detail" ? detailWidth : liveHeight
    updatePane(kind, event.key === "Home" ? kind === "tree" ? 220 : kind === "detail" ? 320 : 212 : initial + delta * direction)
  }

  const moveTab = (event: ReactKeyboardEvent<HTMLElement>) => {
    if (!["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key)) return
    const buttons = [...event.currentTarget.querySelectorAll<HTMLButtonElement>('[role="tab"]')]
    const index = buttons.indexOf(document.activeElement as HTMLButtonElement)
    const next = event.key === "Home" ? 0 : event.key === "End" ? buttons.length - 1 : (index + (event.key === "ArrowRight" ? 1 : -1) + buttons.length) % buttons.length
    event.preventDefault()
    buttons[next]?.focus()
    buttons[next]?.click()
  }
  const base = `/courses/${encodeURIComponent(selectedLecture.realtime.courseId)}/lectures/${encodeURIComponent(selectedLecture.realtime.lectureId)}`
  const node = selectedNodeId && scaffold?.nodes[selectedNodeId]
  const focusSlide = (page: number) => { setPageNumber(page); if (mode === "review") setDetailHidden(false); if (narrow) setMobileView("pdf") }
  const toggleTree = () => {
    if (document.activeElement?.closest(".workspace-tree-panel")) document.querySelector<HTMLButtonElement>('.workspace-pane-toggle[aria-controls="workspace-directory"]')?.focus()
    setTreeHidden((hidden) => !hidden); setPdfExpanded(false)
  }
  const toggleDetail = () => {
    const panel = mode === "review" ? ".pdf-pane" : ".workspace-detail-panel"
    const control = mode === "review" ? "workspace-pdf" : "workspace-detail"
    if (document.activeElement?.closest(panel)) document.querySelector<HTMLButtonElement>(`button[aria-controls="${control}"]`)?.focus()
    setDetailHidden((hidden) => !hidden); setPdfExpanded(false)
  }

  return <main className={`lecture-workspace-page ${mode === "review" ? "is-review-mode" : ""}`}>
    <AppHeader subtitle="Study workspace" className="app-header--study">
      <CourseLectureSelector courses={courses} selectedCourse={selectedCourse} selectedLecture={selectedLecture} onCourseChange={selectCourse} onLectureChange={selectLecture} disabled={sessionLocked} />
      <StudyStages courseId={selectedLecture.realtime.courseId} lectureId={selectedLecture.realtime.lectureId} active={mode === "review" ? "review" : "live"} locked={sessionLocked} />
    </AppHeader>
    <div className="workspace-reading-toolbar">
      <div className="workspace-reading-heading"><h1>{scaffold?.title ?? selectedLecture.title}</h1></div>
      <div className="workspace-reading-actions">
        <button className="workspace-pane-toggle" aria-controls="workspace-directory" aria-pressed={!treeHidden && !pdfExpanded} onClick={toggleTree}><Icon name="sidebar" />目录</button>
        <button className="workspace-pane-toggle" aria-controls={mode === "review" ? "workspace-pdf" : "workspace-detail"} aria-pressed={!detailHidden && !pdfExpanded} onClick={toggleDetail}><Icon name="notes" />{mode === "review" ? "课件参考" : "笔记"}</button>
        <a href={`${base}/formulas`} target="_blank" rel="noreferrer">本讲公式 ↗</a>
      </div>
    </div>
    <nav className="workspace-mobile-tabs" role="tablist" aria-label="Workspace views" onKeyDown={moveTab}>
      {([{ id: "pdf", label: "课件", panel: "workspace-pdf" }, { id: "tree", label: "目录", panel: "workspace-directory" }, { id: "notes", label: "笔记", panel: "workspace-detail" }, { id: "live", label: "字幕", panel: "workspace-live" }] as const).map((view) => <button key={view.id} id={`view-${view.id}`} role="tab" aria-selected={mobileView === view.id} aria-controls={view.panel} tabIndex={mobileView === view.id ? 0 : -1} onClick={() => { setPdfExpanded(false); setMobileView(view.id) }}>{view.label}</button>)}
    </nav>
    <div ref={workspaceRef} className={`lecture-workspace-body view-${mobileView} ${pdfExpanded ? "is-pdf-expanded" : ""} ${liveExpanded ? "has-expanded-live" : ""} ${treeHidden ? "is-tree-hidden" : ""} ${detailHidden ? "is-detail-hidden" : ""}`}
      style={{ "--workspace-tree-width": `${treeHidden || pdfExpanded ? 0 : treeWidth}px`, "--workspace-tree-divider": treeHidden || pdfExpanded ? "0px" : "8px", "--workspace-detail-width": `${detailHidden || pdfExpanded ? 0 : detailWidth}px`, "--workspace-detail-divider": detailHidden || pdfExpanded ? "0px" : "8px", "--workspace-live-height": `${liveHeight}px` } as CSSProperties}>
      <section className="workspace-tree-panel" id="workspace-directory" aria-label="Knowledge directory">
        {scaffold ? <KnowledgeTree key={`${selectedCourse.id}:${selectedLecture.id}`} scaffold={scaffold} selectedNodeId={selectedNodeId} onSelect={selectNode} compact headerAction={<button className="workspace-pane-close" aria-label="Hide knowledge directory" onClick={toggleTree}><Icon name="close" /></button>} /> : <div className="workspace-content-state" role={error ? "alert" : "status"}><h2>{error ? "Knowledge unavailable" : "Loading knowledge…"}</h2><p>{error || "Preparing the lecture outline."}</p></div>}
      </section>
      <PdfPane id="workspace-pdf" fileName={pdf.name} pdfUrl={pdf.url} pageNumber={pageNumber} expanded={pdfExpanded} onFile={uploadPdf} onPageChange={setPageNumber} onToggleExpanded={() => setPdfExpanded((current) => !current)} notice={uploadedPdf && !uploadedPdf.matched ? `“${uploadedPdf.name}” is shown locally. The knowledge tree and node details remain sourced from ${selectedLecture.title}; run PreAgent before treating them as evidence for this PDF.` : undefined} />
      <section className="workspace-detail-panel" id="workspace-detail" aria-label="Knowledge and class notes">
        <div className="workspace-detail-tabs" role="tablist" aria-label="Note sources" onKeyDown={moveTab}>
          <button id="knowledge-tab" role="tab" aria-selected={detailTab === "knowledge"} aria-controls="knowledge-content" tabIndex={detailTab === "knowledge" ? 0 : -1} onClick={() => setDetailTab("knowledge")}>Knowledge</button>
          <button id="notes-tab" role="tab" aria-selected={detailTab === "notes"} aria-controls="notes-content" tabIndex={detailTab === "notes" ? 0 : -1} onClick={() => setDetailTab("notes")}>Class notes</button>
          {mode !== "review" && <button className="workspace-pane-close" aria-label="Hide note panel" onClick={toggleDetail}><Icon name="close" /></button>}
        </div>
        <div id="knowledge-content" role="tabpanel" aria-labelledby="knowledge-tab" hidden={detailTab !== "knowledge"} className="workspace-detail-content">
          {scaffold && node ? <KnowledgeNodeInspector key={node.node_id} scaffold={scaffold} node={node} onSelect={selectNode} onSlideFocus={focusSlide} classroomEnrichment={<ClassroomEnrichment embedded controller={enrichment} nodeId={node.node_id} scaffold={scaffold} onSelect={selectNode} onSlideFocus={focusSlide} />} /> : <div className="workspace-content-state"><h2>{error ? "Knowledge unavailable" : "Select a knowledge point"}</h2><p>{error || "Choose a point from the directory to read its details."}</p></div>}
        </div>
        <div id="notes-content" role="tabpanel" aria-labelledby="notes-tab" hidden={detailTab !== "notes"} className="workspace-notes-content">
          {scaffold && node ? <ClassroomEnrichment controller={enrichment} nodeId={node.node_id} scaffold={scaffold} onSelect={selectNode} onSlideFocus={focusSlide} /> : <div className="workspace-content-state"><h2>Class notes</h2><p>{error || "Notes will appear alongside the selected knowledge point."}</p></div>}
        </div>
      </section>
      {!pdfExpanded && <>
        {!treeHidden && <button className="workspace-resizer workspace-resizer--tree" type="button" aria-label="Resize knowledge directory" onPointerDown={(event) => beginResize("tree", event)} onKeyDown={(event) => resizeWithKeyboard("tree", event)} />}
        {!detailHidden && <button className="workspace-resizer workspace-resizer--detail" type="button" aria-label="Resize note panel" onPointerDown={(event) => beginResize("detail", event)} onKeyDown={(event) => resizeWithKeyboard("detail", event)} />}
      </>}
      {!narrow && liveExpanded && <button className="workspace-resizer workspace-resizer--live" type="button" aria-label="Resize live transcript" onPointerDown={(event) => beginResize("live", event)} onKeyDown={(event) => resizeWithKeyboard("live", event)} />}
      <div id="workspace-live" className="workspace-live-panel"><LiveClassDock context={{ courseId: selectedLecture.realtime.courseId, lectureId: selectedLecture.realtime.lectureId, courseTitle: selectedCourse.title, lectureTitle: selectedLecture.title }} expanded={narrow ? mobileView === "live" : liveExpanded} onExpandedChange={(expanded) => { if (narrow) setMobileView(expanded ? "live" : "pdf"); else setLiveExpanded(expanded) }} onSessionLockChange={setSessionLocked} activeSession={activeSession} lastSession={lastSession} onSessionChange={onSessionChange} /></div>
    </div>
  </main>
}
