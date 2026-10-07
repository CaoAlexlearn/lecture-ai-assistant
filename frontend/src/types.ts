export type RealtimeConnectionStatus =
  | "idle"
  | "connecting"
  | "resuming"
  | "listening"
  | "pausing"
  | "paused"
  | "interrupted"
  | "stopping"
  | "finished"
  | "error"

export type RealtimeText = { committed: string; unstable: string; display: string }
export type RealtimeSnapshot = { revision: number; source: RealtimeText; translation: RealtimeText }

export type ServerEvent =
  | { type: "session_started"; course_id: string; lecture_id: string; session_id: string; started_at: string; connection_generation: number; hotword_count: number; models: { asr: string; translation: string } }
  | { type: "realtime_snapshot"; revision: number; status: string; source: RealtimeText; translation: RealtimeText }
  | { type: "error"; message: string }
  | { type: "session_finished"; session_id: string }
