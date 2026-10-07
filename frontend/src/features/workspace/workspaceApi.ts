import type { Agent1Status, Agent2Status, Agent2Results, ClassSession, WorkspaceCourse, WorkspaceLecture } from "./types"
import type { ExecutionBackend } from "./ExecutionBackend"

export async function request<T>(url: string, options?: RequestInit): Promise<T> {
  const response = await fetch(url, options)
  if (!response.ok) {
    let message = `Request failed (${response.status}).`
    try {
      const body = await response.json() as { detail?: string }
      if (body.detail) message = body.detail
    } catch { /* keep the HTTP fallback */ }
    throw new Error(message)
  }
  return response.json() as Promise<T>
}

export const workspaceApi = {
  async listCourses(): Promise<WorkspaceCourse[]> {
    return (await request<{ courses: WorkspaceCourse[] }>("/api/courses")).courses
  },
  getCourse: (courseId: string) => request<WorkspaceCourse>(`/api/courses/${encodeURIComponent(courseId)}`),
  getLecture: (courseId: string, lectureId: string) => request<WorkspaceLecture>(`/api/courses/${encodeURIComponent(courseId)}/lectures/${encodeURIComponent(lectureId)}`),
  createCourse: (value: { title: string; code?: string; description?: string }) => request<WorkspaceCourse>("/api/courses", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(value),
  }),
  updateCourse: (courseId: string, value: { title: string; code?: string; description?: string }) => request<WorkspaceCourse>(`/api/courses/${encodeURIComponent(courseId)}`, {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(value),
  }),
  deleteCourse: (courseId: string) => request<{ deleted: true; id: string }>(`/api/courses/${encodeURIComponent(courseId)}`, { method: "DELETE" }),
  createLecture: (courseId: string, value: { number: string; title: string }) => request<WorkspaceLecture>(`/api/courses/${encodeURIComponent(courseId)}/lectures`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(value),
  }),
  updateLecture: (courseId: string, lectureId: string, value: { number: string; title: string }) => request<WorkspaceLecture>(`/api/courses/${encodeURIComponent(courseId)}/lectures/${encodeURIComponent(lectureId)}`, {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(value),
  }),
  deleteLecture: (courseId: string, lectureId: string) => request<{ deleted: true; id: string }>(`/api/courses/${encodeURIComponent(courseId)}/lectures/${encodeURIComponent(lectureId)}`, { method: "DELETE" }),
  uploadMaterial: (courseId: string, lectureId: string, file: File) => request<WorkspaceLecture>(`/api/courses/${encodeURIComponent(courseId)}/lectures/${encodeURIComponent(lectureId)}/material?filename=${encodeURIComponent(file.name)}`, {
    method: "POST",
    headers: { "Content-Type": file.type || "application/octet-stream" },
    body: file,
  }),
  runPreparation: (courseId: string, lectureId: string, backend: "qwen" | "codex" = "qwen") => request<WorkspaceLecture>(`/api/courses/${encodeURIComponent(courseId)}/lectures/${encodeURIComponent(lectureId)}/prepare?backend=${backend}`, { method: "POST" }),
  listSessions: async (courseId: string, lectureId: string) => (await request<{ sessions: ClassSession[] }>(`/api/courses/${encodeURIComponent(courseId)}/lectures/${encodeURIComponent(lectureId)}/sessions`)).sessions,
  getSession: (courseId: string, lectureId: string, sessionId: string) => request<ClassSession>(`/api/courses/${encodeURIComponent(courseId)}/lectures/${encodeURIComponent(lectureId)}/sessions/${encodeURIComponent(sessionId)}`),
  startSession: (courseId: string, lectureId: string) => request<ClassSession>(`/api/courses/${encodeURIComponent(courseId)}/lectures/${encodeURIComponent(lectureId)}/sessions`, { method: "POST" }),
  pauseSession: (courseId: string, lectureId: string, sessionId: string) => request<ClassSession>(`/api/courses/${encodeURIComponent(courseId)}/lectures/${encodeURIComponent(lectureId)}/sessions/${encodeURIComponent(sessionId)}/pause`, { method: "POST" }),
  resumeSession: (courseId: string, lectureId: string, sessionId: string) => request<ClassSession>(`/api/courses/${encodeURIComponent(courseId)}/lectures/${encodeURIComponent(lectureId)}/sessions/${encodeURIComponent(sessionId)}/resume`, { method: "POST" }),
  completeSession: (courseId: string, lectureId: string, sessionId: string) => request<ClassSession>(`/api/courses/${encodeURIComponent(courseId)}/lectures/${encodeURIComponent(lectureId)}/sessions/${encodeURIComponent(sessionId)}/complete`, { method: "POST" }),
  getAgent1Status: (courseId: string, lectureId: string, sessionId: string) => request<Agent1Status>(`/api/courses/${encodeURIComponent(courseId)}/lectures/${encodeURIComponent(lectureId)}/sessions/${encodeURIComponent(sessionId)}/agent1`),
  runAgent1: (courseId: string, lectureId: string, sessionId: string, backend: ExecutionBackend = "qwen") => request<Agent1Status>(`/api/courses/${encodeURIComponent(courseId)}/lectures/${encodeURIComponent(lectureId)}/sessions/${encodeURIComponent(sessionId)}/agent1/run?backend=${backend}`, { method: "POST" }),
  getAgent2Status: (courseId: string, lectureId: string, sessionId: string) => request<Agent2Status>(`/api/courses/${encodeURIComponent(courseId)}/lectures/${encodeURIComponent(lectureId)}/sessions/${encodeURIComponent(sessionId)}/agent2`),
  runAgent2: (courseId: string, lectureId: string, sessionId: string, backend: ExecutionBackend = "qwen") => request<Agent2Status>(`/api/courses/${encodeURIComponent(courseId)}/lectures/${encodeURIComponent(lectureId)}/sessions/${encodeURIComponent(sessionId)}/agent2/run?backend=${backend}`, { method: "POST" }),
  getAgent2Results: (courseId: string, lectureId: string, sessionId: string) => request<Agent2Results>(`/api/courses/${encodeURIComponent(courseId)}/lectures/${encodeURIComponent(lectureId)}/sessions/${encodeURIComponent(sessionId)}/agent2/results`),
}
