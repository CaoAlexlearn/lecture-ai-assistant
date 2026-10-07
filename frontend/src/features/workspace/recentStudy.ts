import { lectureDestination } from "./workspaceRouting"
import type { WorkspaceCourse } from "./types"

export const RECENT_STUDY_KEY = "classroom-assistant:recent-study"
export type RecentStudy = { courseId: string; lectureId: string; page: "prepare" | "live"; review: boolean }

export function readRecentStudy(): RecentStudy | undefined {
  try {
    const value = JSON.parse(localStorage.getItem(RECENT_STUDY_KEY) ?? "null")
    if (value && typeof value.courseId === "string" && typeof value.lectureId === "string" && (value.page === "prepare" || value.page === "live")) return { ...value, review: value.review === true }
  } catch { /* Storage may be disabled; course history remains available. */ }
}

export function studyResume(courses: WorkspaceCourse[], recent?: RecentStudy) {
  const choices = courses.flatMap((course) => course.lectures.map((lecture) => ({ course, lecture })))
  const visited = recent && choices.find(({ course, lecture }) => course.id === recent.courseId && lecture.id === recent.lectureId)
  const history = choices.filter(({ lecture }) => lecture.activeSession || lecture.lastSession).sort((a, b) => {
    const timestamp = (choice: typeof a) => { const session = choice.lecture.activeSession ?? choice.lecture.lastSession; return Date.parse(session?.endedAt ?? session?.startedAt ?? session?.createdAt ?? "") || 0 }
    const activeFirst = Number(Boolean(b.lecture.activeSession)) - Number(Boolean(a.lecture.activeSession))
    return activeFirst || timestamp(b) - timestamp(a)
  })[0]
  const choice = visited ?? history ?? choices.find(({ lecture }) => lecture.preparationStatus === "ready") ?? choices[0]
  if (!choice) return
  const ready = choice.lecture.preparationStatus === "ready"
  const prepare = !ready || (visited && recent?.page === "prepare")
  const path = prepare ? `/courses/${encodeURIComponent(choice.course.id)}/lectures/${encodeURIComponent(choice.lecture.id)}/prepare` : lectureDestination(choice.course.id, choice.lecture.id, choice.lecture.preparationStatus)
  return { ...choice, href: path + (visited && recent?.review && !prepare ? "?view=review" : ""), source: visited ? "visit" : history ? "session" : "first" }
}
