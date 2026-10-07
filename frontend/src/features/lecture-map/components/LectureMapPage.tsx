import { useEffect, useState } from "react"
import { COURSE_REGISTRY } from "../data/courseRegistry"
import { markdownLectureMapProvider } from "../data/loadLectureMap"
import type { LectureMapData } from "../types/lectureMap"
import { CourseLectureSelector } from "./CourseLectureSelector"
import { LectureMap } from "./LectureMap"
import "../lecture-map.css"

const DEFAULT_COURSE = COURSE_REGISTRY[0]
const DEFAULT_LECTURE = DEFAULT_COURSE.lectures[0]

export function LectureMapPage() {
  const [selectedCourseId, setSelectedCourseId] = useState(DEFAULT_COURSE.id)
  const [selectedLectureId, setSelectedLectureId] = useState(DEFAULT_LECTURE.id)
  const [data, setData] = useState<LectureMapData>()
  const [error, setError] = useState("")

  const selectedCourse = COURSE_REGISTRY.find((course) => course.id === selectedCourseId) ?? DEFAULT_COURSE
  const selectedLecture = selectedCourse.lectures.find((lecture) => lecture.id === selectedLectureId)
    ?? selectedCourse.lectures[0]

  useEffect(() => {
    let current = true
    setData(undefined)
    setError("")
    markdownLectureMapProvider.load(selectedLecture)
      .then((next) => {
        if (current) setData(next)
      })
      .catch((caught: unknown) => {
        if (current) setError(caught instanceof Error ? caught.message : "Lecture data could not be loaded.")
      })
    return () => { current = false }
  }, [selectedLecture])

  const selectCourse = (courseId: string) => {
    const course = COURSE_REGISTRY.find((candidate) => candidate.id === courseId)
    if (!course || course.lectures.length === 0) return
    setSelectedCourseId(course.id)
    setSelectedLectureId(course.lectures[0].id)
  }

  return (
    <main className="lecture-map-page">
      <header className="lecture-map-header">
        <a className="lecture-map-brand" href="/" aria-label="Classroom Assistant home">
          <span aria-hidden="true">CA</span>
          <span>
            <strong>Lecture Map</strong>
            <small>Classroom Assistant</small>
          </span>
        </a>
        <CourseLectureSelector
          courses={COURSE_REGISTRY}
          selectedCourse={selectedCourse}
          selectedLecture={selectedLecture}
          onCourseChange={selectCourse}
          onLectureChange={setSelectedLectureId}
        />
        <a className="lecture-map-header__workspace-link" href="/lecture-workspace.html">
          Open study workspace <span aria-hidden="true">→</span>
        </a>
      </header>

      {error ? (
        <section className="lecture-map-state" role="alert">
          <span>Data unavailable</span>
          <h1>Lecture Map could not open this lecture.</h1>
          <p>{error}</p>
          <button type="button" onClick={() => window.location.reload()}>Try again</button>
        </section>
      ) : data ? (
        <LectureMap key={`${selectedCourse.id}:${selectedLecture.id}`} data={data} />
      ) : (
        <section className="lecture-map-state" aria-live="polite">
          <div className="lecture-map-loader" aria-hidden="true"><i /><i /><i /></div>
          <span>Preparing map</span>
          <h1>Organizing {selectedLecture.title}…</h1>
        </section>
      )}
    </main>
  )
}
