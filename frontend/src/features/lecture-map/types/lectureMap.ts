export type SlideRange = {
  start: number
  end: number
  slides: number[]
  label: string
}

export type LectureSubtopic = {
  id: string
  orderIndex: number
  title: string
  slideRange: SlideRange
}

export type LectureSection = {
  id: string
  orderIndex: number
  number?: number
  title: string
  kind: "context" | "section"
  slideRange?: SlideRange
  brief?: string
  subtopics: LectureSubtopic[]
}

export type LectureOverview = {
  title: string
  sections: LectureSection[]
}

export type SlidePreparation = {
  slideNumber: number
  title: string
  role?: string
  keyPoints: string[]
  examples: string[]
  questions: string[]
  terms: string[]
  visuals: string[]
  sourceAnswers: string[]
}

export type LectureMapData = {
  overview: LectureOverview
  slides: Map<number, SlidePreparation>
}

export type LectureSource = {
  overviewUrl: string
  slidesPreparationUrl: string
  scaffoldUrl?: string
  pdfUrl?: string
  pdfFileNames?: string[]
}

export type LectureOption = {
  id: string
  title: string
  preparationStatus?: "created" | "material_uploaded" | "preparing" | "ready" | "outdated" | "failed"
  realtime: {
    courseId: string
    lectureId: string
  }
  source: LectureSource
}

export type CourseOption = {
  id: string
  title: string
  lectures: LectureOption[]
}

export type LectureMapDataProvider = {
  load: (lecture: LectureOption) => Promise<LectureMapData>
}

export type LectureSelection =
  | { kind: "root" }
  | { kind: "section"; sectionId: string }
  | { kind: "subtopic"; sectionId: string; subtopicId: string }

export type LectureNodeKind = LectureSelection["kind"]

export type LectureNodeData = {
  kind: LectureNodeKind
  label: string
  eyebrow?: string
  slideLabel?: string
  accent: string
  selected: boolean
  activePath: boolean
  dimmed: boolean
  selection: LectureSelection
}

export type LectureGraphState = {
  selectedSectionId?: string
  selectedSubtopicId?: string
  expandedSectionId?: string
  expandAll: boolean
}

export type AggregatedTopic = {
  title: string
  sectionTitle: string
  slideRange: SlideRange
  slideTitles: string[]
  keyPoints: string[]
  examples: string[]
  questions: string[]
  terms: string[]
  visuals: string[]
  missingSlides: number[]
}
