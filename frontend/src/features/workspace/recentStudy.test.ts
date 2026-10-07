import { describe, expect, it } from "vitest"
import { studyResume } from "./recentStudy"
import type { WorkspaceCourse, WorkspaceLecture } from "./types"

function lecture(id: string, ready = true, endedAt?: string): WorkspaceLecture {
  return { id, course_id: "course", workspace_dir: "", number: "01", title: id, preparationStatus: ready ? "ready" : "created", created_at: "", sessionCount: endedAt ? 1 : 0, outputs: { scaffold: ready, markdown: ready, hotwords: ready, asr_context: ready }, urls: {}, lastSession: endedAt ? { id: `session-${id}`, courseId: "course", lectureId: id, status: "completed", createdAt: endedAt, endedAt } : undefined }
}
function courses(...lectures: WorkspaceLecture[]): WorkspaceCourse[] { return [{ id: "course", title: "Course", created_at: "", lectures }] }

describe("continue learning", () => {
  it("resumes the valid local lecture and stage without overriding it with newer class history", () => {
    const result = studyResume(courses(lecture("visited"), lecture("latest", true, "2026-10-06T00:00:00Z")), { courseId: "course", lectureId: "visited", page: "live", review: true })
    expect(result?.href).toBe("/courses/course/lectures/visited/live?view=review")
    expect(result?.source).toBe("visit")
  })
  it("falls back to real class history if the saved lecture was removed", () => {
    const result = studyResume(courses(lecture("older", true, "2026-10-01T00:00:00Z"), lecture("latest", true, "2026-10-05T00:00:00Z")), { courseId: "course", lectureId: "deleted", page: "live", review: false })
    expect(result?.lecture.id).toBe("latest")
    expect(result?.source).toBe("session")
  })
  it("sends stale or unprepared lectures to preparation instead of live or review", () => {
    const result = studyResume(courses(lecture("unprepared", false)), { courseId: "course", lectureId: "unprepared", page: "live", review: true })
    expect(result?.href).toBe("/courses/course/lectures/unprepared/prepare")
    expect(studyResume([])).toBeUndefined()
  })
})
