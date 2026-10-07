import type { InstructionDocument, InstructionPreview, InstructionVersion } from "./types"

async function request<T>(url: string, options?: RequestInit): Promise<T> {
  const response = await fetch(url, options)
  if (!response.ok) {
    const body = await response.json().catch(() => ({})) as { detail?: string }
    throw new Error(body.detail || `Request failed (${response.status}).`)
  }
  return response.json() as Promise<T>
}

const json = (value: unknown): RequestInit => ({
  method: "POST",
  headers: { "Content-Type": "application/json" },
  body: JSON.stringify(value),
})

export const instructionsApi = {
  listDocuments: async () => (await request<{ documents: InstructionDocument[] }>("/api/instructions/documents")).documents,
  getDocument: (id: string) => request<InstructionDocument>(`/api/instructions/documents/${encodeURIComponent(id)}`),
  listVersions: async (id: string) => (await request<{ versions: InstructionVersion[] }>(`/api/instructions/documents/${encodeURIComponent(id)}/versions`)).versions,
  createDraft: (id: string, sourceVersionId?: string) => request<InstructionVersion>(`/api/instructions/documents/${encodeURIComponent(id)}/drafts`, json({ sourceVersionId })),
  saveDraft: (documentId: string, versionId: string, content: string, expectedUpdatedAt: string, note: string) => request<InstructionVersion>(`/api/instructions/documents/${encodeURIComponent(documentId)}/versions/${versionId}`, {
    ...json({ content, expectedUpdatedAt, note }),
    method: "PUT",
  }),
  activate: (documentId: string, versionId: string) => request<InstructionDocument>(`/api/instructions/documents/${encodeURIComponent(documentId)}/versions/${versionId}/activate`, { method: "POST" }),
  restore: (documentId: string, versionId: string) => request<InstructionVersion>(`/api/instructions/documents/${encodeURIComponent(documentId)}/versions/${versionId}/restore`, { method: "POST" }),
  compare: (documentId: string, base: string, target: string) => request<{ diff: string; added: number; removed: number }>(`/api/instructions/documents/${encodeURIComponent(documentId)}/compare?base=${encodeURIComponent(base)}&target=${encodeURIComponent(target)}`),
  preview: (value: Record<string, unknown>) => request<InstructionPreview>("/api/instructions/preview-context", json(value)),
}
