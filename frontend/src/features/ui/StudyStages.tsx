export function StudyStages({ courseId, lectureId, active, ready = true, locked = false }: {
  courseId: string; lectureId: string; active: "prepare" | "live" | "review"; ready?: boolean; locked?: boolean
}) {
  const base = `/courses/${encodeURIComponent(courseId)}/lectures/${encodeURIComponent(lectureId)}`
  return <nav className="study-stages" aria-label="Study stages">
    {([{ id: "prepare", label: "课前", href: `${base}/prepare` }, { id: "live", label: "上课", href: `${base}/live` }, { id: "review", label: "课后", href: `${base}/live?view=review` }] as const).map((stage) => {
      const disabled = (!ready && stage.id !== "prepare") || (locked && stage.id !== active)
      return <a key={stage.id} href={stage.href} aria-current={active === stage.id ? "page" : undefined} aria-disabled={disabled || undefined}
        title={disabled ? locked ? "请先暂停课堂，再切换学习阶段" : "请先完成课前准备" : undefined}
        onClick={(event) => { if (disabled) event.preventDefault() }}>{stage.label}</a>
    })}
  </nav>
}
