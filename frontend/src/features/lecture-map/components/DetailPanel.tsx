import type { ReactNode } from "react"
import { aggregateSubtopic } from "../data/loadLectureMap"
import type {
  AggregatedTopic,
  LectureMapData,
  LectureSection,
  LectureSelection,
} from "../types/lectureMap"

type Props = {
  data: LectureMapData
  selection: LectureSelection
  open: boolean
  onClose: () => void
  onSelect: (selection: LectureSelection) => void
}

function ArrowIcon() {
  return (
    <svg viewBox="0 0 20 20" aria-hidden="true">
      <path d="m7.5 4.75 5 5-5 5" fill="none" stroke="currentColor" strokeLinecap="round" strokeLinejoin="round" strokeWidth="1.6" />
    </svg>
  )
}

function CloseIcon() {
  return (
    <svg viewBox="0 0 20 20" aria-hidden="true">
      <path d="m5 5 10 10M15 5 5 15" fill="none" stroke="currentColor" strokeLinecap="round" strokeWidth="1.5" />
    </svg>
  )
}

function DetailHeader({
  eyebrow,
  title,
  meta,
}: {
  eyebrow: string
  title: string
  meta?: string
}) {
  return (
    <header className="detail-header">
      <span className="detail-eyebrow">{eyebrow}</span>
      <h2>{title}</h2>
      {meta && <p>{meta}</p>}
    </header>
  )
}

function NumberedList({ items }: { items: string[] }) {
  return (
    <ol className="detail-numbered-list">
      {items.map((item, index) => (
        <li key={`${index}-${item}`}>
          <span>{String(index + 1).padStart(2, "0")}</span>
          <p>{item}</p>
        </li>
      ))}
    </ol>
  )
}

function BulletList({ items }: { items: string[] }) {
  return (
    <ul className="detail-bullet-list">
      {items.map((item, index) => <li key={`${index}-${item}`}>{item}</li>)}
    </ul>
  )
}

function DetailSection({ title, children }: { title: string; children: ReactNode }) {
  return (
    <section className="detail-section">
      <h3>{title}</h3>
      {children}
    </section>
  )
}

function RootDetail({ data, onSelect }: Pick<Props, "data" | "onSelect">) {
  const first = data.overview.sections[0]?.slideRange?.start
  const last = data.overview.sections.at(-1)?.slideRange?.end
  const coverage = first && last ? `Slides ${first}\u2013${last} · ${data.overview.sections.length} branches` : `${data.overview.sections.length} branches`

  return (
    <>
      <DetailHeader eyebrow="Lecture overview" title={data.overview.title} meta={coverage} />
      <DetailSection title="Structure">
        <div className="detail-navigation-list">
          {data.overview.sections.map((section, index) => (
            <button
              type="button"
              key={section.id}
              onClick={() => onSelect({ kind: "section", sectionId: section.id })}
            >
              <span>{section.kind === "context" ? "CTX" : String(section.number ?? index + 1).padStart(2, "0")}</span>
              <span>
                <strong>{section.title}</strong>
                <small>{section.slideRange?.label ?? `${section.subtopics.length} topics`}</small>
              </span>
              <ArrowIcon />
            </button>
          ))}
        </div>
      </DetailSection>
    </>
  )
}

function SectionDetail({ section, onSelect }: { section: LectureSection; onSelect: Props["onSelect"] }) {
  return (
    <>
      <DetailHeader
        eyebrow={section.kind === "context" ? "Course context" : `Section ${String(section.number ?? "").padStart(2, "0")}`}
        title={section.title}
        meta={section.slideRange?.label}
      />
      {section.brief && (
        <DetailSection title="Brief">
          <p className="detail-prose">{section.brief}</p>
        </DetailSection>
      )}
      <DetailSection title="Subtopics">
        <div className="detail-navigation-list detail-navigation-list--topics">
          {section.subtopics.map((topic, index) => (
            <button
              type="button"
              key={topic.id}
              onClick={() => onSelect({ kind: "subtopic", sectionId: section.id, subtopicId: topic.id })}
            >
              <span>{String(index + 1).padStart(2, "0")}</span>
              <span>
                <strong>{topic.title}</strong>
                <small>{topic.slideRange.label}</small>
              </span>
              <ArrowIcon />
            </button>
          ))}
        </div>
      </DetailSection>
    </>
  )
}

function TopicDetail({ topic }: { topic: AggregatedTopic }) {
  return (
    <>
      <DetailHeader eyebrow={topic.sectionTitle} title={topic.title} meta={topic.slideRange.label} />

      {topic.slideTitles.length > 0 && (
        <DetailSection title="Topic scope">
          <p className="detail-prose detail-prose--muted">{topic.slideTitles.join(" · ")}</p>
        </DetailSection>
      )}

      {topic.keyPoints.length > 0 && (
        <DetailSection title="Key points">
          <NumberedList items={topic.keyPoints} />
        </DetailSection>
      )}

      {topic.examples.length > 0 && (
        <DetailSection title="Examples">
          <BulletList items={topic.examples} />
        </DetailSection>
      )}

      {topic.questions.length > 0 && (
        <DetailSection title="Questions">
          <div className="detail-questions">
            {topic.questions.map((question, index) => (
              <blockquote key={`${index}-${question}`}>{question}</blockquote>
            ))}
          </div>
        </DetailSection>
      )}

      {topic.terms.length > 0 && (
        <DetailSection title="Terms">
          <div className="detail-chips">
            {topic.terms.map((term) => <span key={term.toLocaleLowerCase()}>{term}</span>)}
          </div>
        </DetailSection>
      )}

      {topic.visuals.length > 0 && (
        <details className="detail-visuals">
          <summary>Visual references <span>{topic.visuals.length}</span></summary>
          <BulletList items={topic.visuals} />
        </details>
      )}

      {topic.missingSlides.length > 0 && (
        <p className="detail-warning">No preparation entry was found for slide {topic.missingSlides.join(", ")}.</p>
      )}
    </>
  )
}

function selectionKey(selection: LectureSelection): string {
  if (selection.kind === "root") return "root"
  if (selection.kind === "section") return selection.sectionId
  return selection.subtopicId
}

export function DetailPanel({ data, selection, open, onClose, onSelect }: Props) {
  const section = selection.kind === "root"
    ? undefined
    : data.overview.sections.find((candidate) => candidate.id === selection.sectionId)
  const subtopic = selection.kind === "subtopic"
    ? section?.subtopics.find((candidate) => candidate.id === selection.subtopicId)
    : undefined
  const topic = section && subtopic ? aggregateSubtopic(data, section, subtopic) : undefined

  return (
    <aside className={`lecture-detail ${open ? "is-open" : ""}`} aria-label="Selected lecture node details">
      <button className="lecture-detail__close" type="button" aria-label="Close details" onClick={onClose}>
        <CloseIcon />
      </button>
      <div className="lecture-detail__scroll" key={selectionKey(selection)}>
        {selection.kind === "root" && <RootDetail data={data} onSelect={onSelect} />}
        {selection.kind === "section" && section && <SectionDetail section={section} onSelect={onSelect} />}
        {selection.kind === "subtopic" && topic && <TopicDetail topic={topic} />}
      </div>
    </aside>
  )
}
