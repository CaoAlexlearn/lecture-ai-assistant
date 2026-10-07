export type PreparationStatus = "created" | "material_uploaded" | "preparing" | "ready" | "outdated" | "failed"
export type SessionStatus = "created" | "live" | "paused" | "completed" | "aborted"

export type Agent1Status = {
  status: "not_started" | "processing" | "completed" | "completed_with_errors" | "failed"
  block_count?: number
  completed_block_count?: number
  failed_block_ids?: string[]
  provider?: string
  model?: string
  segmenter_provider?: string
  segmenter_model?: string
  root_cause?: { error_type: string; error_message: string }
}

export type Agent2Status = Agent1Status & {
  agent1_ready: boolean
  applied_edit_count?: number
  flagged_edit_count?: number
  validation_issue_count?: number
  skipped_edit_count?: number
  review_item_count?: number
  block_stages?: Record<string, string>
}
export type Agent2Edit = {
  unit_id: string
  original: string
  replacement: string | null
  decision: "apply" | "flag"
  kind: string
  reason: string
  evidence: { page: number; quote: string }[]
}
export type Agent2Block = {
  block_id: string
  title: string
  candidate_nodes: { node_id: string; title: string }[]
  selection: { unmatched_reason: string | null }
  evidence_pages: number[]
  original_units: { unit_id: string; text: string }[]
  corrected_units: { unit_id: string; text: string }[]
  corrected_summary: { summary: string[]; how_teacher_explained: string[]; examples: string[]; emphasis: string[] }
  edits: Agent2Edit[]
  validation_issues?: { block_id: string; edit_index: number; unit_id: string | null; action: "flagged" | "skipped"; error_message: string }[]
}
export type Agent2Results = { manifest: Agent2Status; blocks: Agent2Block[] }

export type ClassSession = {
  id: string
  courseId: string
  lectureId: string
  status: SessionStatus
  createdAt: string
  startedAt?: string | null
  endedAt?: string | null
  pausedAt?: string | null
  pauseIntervals?: Array<{ startedAt: string; endedAt?: string | null }>
  activeCaptureSeconds?: number
  lastResumedAt?: string | null
  runtimeState?: "disconnected" | "connecting" | "connected" | "pausing" | "paused" | "interrupted" | "stopping" | "stopped" | "drain_failed"
  connectionGeneration?: number
  transcriptPath?: string
  translationPath?: string
  realtimeSnapshotsPath?: string
  realtimeLatestPath?: string
  realtimeProtocolVersion?: number
  realtimeModels?: { asr: string; translation: string; semanticPunctuation: boolean }
  agent1Mode?: "detached" | string
  agent1NotesPath?: string
  agent1PromptVersionId?: string | null
  agent1ModelConfigSnapshot?: Record<string, unknown> | null
  instructionSnapshot?: Record<string, unknown> | null
  preparationRunId?: string | null
  preparationSnapshot?: Record<string, unknown> | null
  hotwordsSnapshot?: string[]
  hotwordVocabularySnapshot?: Record<string, number>
  agent1JobStatus?: { pending: number; running: number; written: number; failed: number }
  agent1SourceStatus?: { sourceCursor: number; rawSourceHead: number; bufferedSourceCount: number; durableJobCount: number; pendingJobs: number; runningJobs: number; failedJobs: number }
  notesDrained?: boolean
  legacy?: boolean
}

export type CourseMaterial = {
  filename: string
  content_type: string
  size_bytes: number
  uploaded_at: string
  pdf_path: string
}

export type WorkspaceLecture = {
  id: string
  course_id: string
  workspace_dir: string
  number: string
  title: string
  preparationStatus: PreparationStatus
  created_at: string
  prepared_at?: string | null
  material?: CourseMaterial | null
  preparation?: {
    executionBackend?: "qwen" | "codex"
    error?: string | null
    started_at?: string
    preAgentPromptVersionId?: string | null
    instructionSnapshot?: Record<string, unknown> | null
  }
  preparationStage?: string | null
  activeSession?: ClassSession | null
  lastSession?: ClassSession | null
  sessionCount: number
  outputs: { scaffold: boolean; markdown: boolean; hotwords: boolean; asr_context: boolean }
  urls: { material?: string | null; scaffold?: string | null; markdown?: string | null; hotwords?: string | null; asr_context?: string | null }
}

export type WorkspaceCourse = {
  id: string
  title: string
  code?: string | null
  description?: string | null
  created_at: string
  lectures: WorkspaceLecture[]
}

export type WorkspaceRoute =
  | { page: "courses" }
  | { page: "agent-lab" }
  | { page: "instructions" }
  | { page: "course"; courseId: string }
  | { page: "prepare" | "live" | "formulas"; courseId: string; lectureId: string }
  | { page: "not-found" }
