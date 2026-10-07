import type { CourseOption, LectureOption } from "../types/lectureMap"

type Props = {
  courses: CourseOption[]
  selectedCourse: CourseOption
  selectedLecture: LectureOption
  onCourseChange: (courseId: string) => void
  onLectureChange: (lectureId: string) => void
  disabled?: boolean
}

export function CourseLectureSelector({
  courses,
  selectedCourse,
  selectedLecture,
  onCourseChange,
  onLectureChange,
  disabled = false,
}: Props) {
  return (
    <div className="lecture-source-selector" aria-label="Course and lecture selection">
      <label>
        <span>Course</span>
        <select disabled={disabled} title={selectedCourse.title} value={selectedCourse.id} onChange={(event) => onCourseChange(event.target.value)}>
          {courses.map((course) => <option key={course.id} value={course.id}>{course.title}</option>)}
        </select>
        <svg className="lecture-source-selector__chevron" viewBox="0 0 16 16" aria-hidden="true"><path d="m4 6 4 4 4-4" /></svg>
      </label>
      <svg className="lecture-source-selector__separator" viewBox="0 0 16 16" aria-hidden="true"><path d="m6 4 4 4-4 4" /></svg>
      <label>
        <span>Lecture</span>
        <select disabled={disabled} title={selectedLecture.title} value={selectedLecture.id} onChange={(event) => onLectureChange(event.target.value)}>
          {selectedCourse.lectures.map((lecture) => (
            <option key={lecture.id} value={lecture.id}>
              {lecture.title}{lecture.preparationStatus ? ` · ${lecture.preparationStatus === "ready" ? "Ready" : lecture.preparationStatus === "preparing" ? "Preparing" : "Not prepared"}` : ""}
            </option>
          ))}
        </select>
        <svg className="lecture-source-selector__chevron" viewBox="0 0 16 16" aria-hidden="true"><path d="m4 6 4 4 4-4" /></svg>
      </label>
    </div>
  )
}
