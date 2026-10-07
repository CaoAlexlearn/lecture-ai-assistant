export type InstructionScope = "faculty" | "user" | "course" | "lecture"
export type InstructionStatus = "draft" | "active" | "archived"

export type InstructionVersion = {
  id: string
  documentId: string
  version: number
  content?: string
  status: InstructionStatus
  createdAt: string
  updatedAt: string
  note?: string
}

export type InstructionDocument = {
  id: string
  scope: InstructionScope
  scopeId: string
  courseId?: string | null
  lectureId?: string | null
  title: string
  subtitle: string
  appliesTo: string
  activeVersionId?: string | null
  nextVersion: number
  versions?: InstructionVersion[]
}

export type ResolvedInstruction = {
  scope: InstructionScope
  scopeId: string
  documentId: string
  versionId: string
  versionStatus: InstructionStatus
  content: string
  source: string
  title: string
  subtitle: string
}

export type InstructionPreview = {
  agentId: string
  courseId: string
  lectureId: string
  loadOrder: InstructionScope[]
  precedence: string[]
  instructions: ResolvedInstruction[]
  blocks: Array<{
    key: string
    label: string
    content: string
    tokenCount: number
    characterCount: number
    scope: string
    versionId: string
    source: string
  }>
}
