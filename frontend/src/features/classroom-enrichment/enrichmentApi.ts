import { request } from "../workspace/workspaceApi"
import type { ExecutionBackend } from "../workspace/ExecutionBackend"

export type SourceRef = {
  block_id: string; unit_id: string; start: number; end: number; quote: string; original_unit_text: string
  session_id: string; asr_fingerprint: string; scaffold_fingerprint: string
}
export type Correction = {
  correction_id: string; unit_id: string; original: string; replacement?: string | null
  decision: string; reason: string; evidence: { page: number; quote: string }[]
}
export type Fragment = {
  fragment_id: string; block_id: string; description: string; corrected_text: string
  types: string[]; status: "mapped" | "unmatched" | "needs_review" | "nonknowledge"
  reason: string; assessment: boolean; sources: SourceRef[]; corrections: Correction[]
  unresolved_correction_ids: string[]; review_code?: string
  uncertainty?: string | null
}
export type Relation = { fragment_id: string; node_id: string; reason: string; contribution: string; angle: string; types: string[] }
export type EnrichmentItem = {
  item_id: string; heading: string; text: string; source_nature: string; fragment_ids: string[]
  shared_content_ids: string[]; course_evidence: { page: number; quote: string; source?: "slide" | "scaffold" }[]
  sources: SourceRef[]; uncertainty?: string | null; needs_review: boolean
}
export type EnrichmentStatus = {
  course_id?: string; lecture_id?: string; session_id?: string
  provider?: string; model?: string
  status: string; asr_status: string; asr_ready: boolean; compatible: boolean; stale: boolean
  prerequisite_error?: string | null; block_count?: number; completed_block_count?: number
  node_count?: number; completed_node_count?: number; stage?: string; review_count?: number
  unmatched_count?: number; requirement_count?: number; failed_block_ids?: string[]
  failed_node_ids?: string[]; incomplete_block_ids?: string[]; asr_failed_block_ids?: string[]
  incomplete_node_ids?: string[]
  root_cause?: { error_type: string; error_message: string }; input_fingerprint?: string
  asr_root_cause?: { error_type: string; error_message: string } | null
}
export type EnrichmentResults = {
  manifest: EnrichmentStatus; fragments: Fragment[]; relations: Relation[]
  pending: Fragment[]; requirements: Fragment[]
  shared_content: { shared_content_id: string; fragment_id: string; description: string; text: string; sources: SourceRef[] }[]
  node_enrichment: { node_id: string; items: EnrichmentItem[]; fragment_ids: string[]; batch_count: number; omitted_fragments: { fragment_id: string; reason: string }[] }[]
}
export function enrichmentApi(courseId: string, lectureId: string, sessionId: string) {
  const path = `/api/courses/${encodeURIComponent(courseId)}/lectures/${encodeURIComponent(lectureId)}/sessions/${encodeURIComponent(sessionId)}/agent2/enrichment`
  return {
    status: () => request<EnrichmentStatus>(path),
    results: () => request<EnrichmentResults>(`${path}/results`),
    run: (backend: ExecutionBackend = "qwen") => request<EnrichmentStatus>(`${path}/run?backend=${backend}`, { method: "POST" }),
  }
}
