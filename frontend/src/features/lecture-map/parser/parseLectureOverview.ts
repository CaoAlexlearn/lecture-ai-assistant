import type {
  LectureOverview,
  LectureSection,
  LectureSubtopic,
  SlideRange,
} from "../types/lectureMap"
import { parseList, parseSlideRange, slugify, stripInlineMarkdown } from "./shared"

type HeadingBlock = {
  heading: string
  lines: string[]
}

function splitLevelThreeBlocks(markdown: string): HeadingBlock[] {
  const blocks: HeadingBlock[] = []
  let current: HeadingBlock | undefined

  for (const line of markdown.replace(/\r\n?/g, "\n").split("\n")) {
    const heading = line.match(/^###\s+(.+?)\s*$/)
    if (heading) {
      if (current) blocks.push(current)
      current = { heading: stripInlineMarkdown(heading[1]), lines: [] }
    } else if (current) {
      current.lines.push(line)
    }
  }
  if (current) blocks.push(current)
  return blocks
}

function sectionRange(lines: string[]): SlideRange | undefined {
  for (const line of lines) {
    const parsed = parseSlideRange(stripInlineMarkdown(line))
    if (parsed) return parsed
  }
  return undefined
}

function fieldLines(lines: string[], label: string): string[] {
  const start = lines.findIndex((line) => line.trim().toLowerCase() === `${label.toLowerCase()}:`)
  if (start < 0) return []

  const result: string[] = []
  for (let index = start + 1; index < lines.length; index += 1) {
    const line = lines[index]
    if (/^[A-Za-z][A-Za-z ]+:\s*$/.test(line.trim())) break
    if (/^####?\s+/.test(line)) break
    result.push(line)
  }
  return result
}

function parseStandardSection(block: HeadingBlock, index: number): LectureSection {
  const numbered = block.heading.match(/^(\d+)\.\s*(.+)$/)
  const number = numbered ? Number(numbered[1]) : undefined
  const title = numbered ? numbered[2].trim() : block.heading
  const range = sectionRange(block.lines)
  const brief = fieldLines(block.lines, "Brief")
    .map((line) => stripInlineMarkdown(line))
    .filter(Boolean)
    .join(" ") || undefined
  const topicLines = parseList(fieldLines(block.lines, "Subtopics"))

  const subtopics = topicLines.flatMap((line, topicIndex): LectureSubtopic[] => {
    const slideRange = parseSlideRange(line)
    if (!slideRange) return []
    const titlePart = line.replace(/^.*?\bSlides?\s+\d+(?:\s*[\u2013\u2014-]\s*\d+)?\s*[\u2013\u2014-]\s*/i, "")
    const topicTitle = titlePart.trim() || slideRange.label
    return [{
      id: `topic-${index + 1}-${topicIndex + 1}-${slugify(topicTitle)}`,
      orderIndex: topicIndex,
      title: topicTitle,
      slideRange,
    }]
  })

  return {
    id: `section-${number ?? index + 1}-${slugify(title)}`,
    orderIndex: index,
    number,
    title,
    kind: "section",
    slideRange: range,
    brief,
    subtopics,
  }
}

function parseContextSection(block: HeadingBlock): LectureSection {
  const subtopics: LectureSubtopic[] = []
  let currentHeading = ""

  for (const line of block.lines) {
    const heading = line.match(/^####\s+(.+?)\s*$/)
    if (!heading) continue
    currentHeading = stripInlineMarkdown(heading[1])
    const slideRange = parseSlideRange(currentHeading)
    if (!slideRange) continue
    const title = currentHeading.replace(/^.*?\bSlides?\s+\d+(?:\s*[\u2013\u2014-]\s*\d+)?\s*[\u2013\u2014-]\s*/i, "").trim()
    subtopics.push({
      id: `context-${slideRange.start}-${slugify(title)}`,
      orderIndex: subtopics.length,
      title: title || slideRange.label,
      slideRange,
    })
  }

  const slides = subtopics.flatMap((topic) => topic.slideRange.slides)
  const slideRange = slides.length > 0
    ? {
        start: Math.min(...slides),
        end: Math.max(...slides),
        slides: [...new Set(slides)].sort((left, right) => left - right),
        label: `Slides ${Math.min(...slides)}\u2013${Math.max(...slides)}`,
      }
    : undefined

  return {
    id: `section-context-${slugify(block.heading)}`,
    orderIndex: 0,
    title: block.heading.replace(/\s*\/\s*Prelude\s*$/i, ""),
    kind: "context",
    slideRange,
    subtopics,
  }
}

export function parseLectureOverview(markdown: string): LectureOverview {
  const normalized = markdown.replace(/\r\n?/g, "\n")
  const titleMatch = normalized.match(/^#\s+(.+?)\s*$/m)
  const title = titleMatch
    ? stripInlineMarkdown(titleMatch[1]).replace(/\s+[\u2013\u2014-]\s+Lecture Overview\s*$/i, "")
    : "Untitled lecture"

  const sections: LectureSection[] = []
  for (const block of splitLevelThreeBlocks(normalized)) {
    if (/^Front Matter$/i.test(block.heading)) continue
    if (/^Course Context(?:\s*\/\s*Prelude)?$/i.test(block.heading)) {
      sections.push(parseContextSection(block))
      continue
    }
    if (/^\d+\.\s+/.test(block.heading) || fieldLines(block.lines, "Subtopics").length > 0) {
      sections.push(parseStandardSection(block, sections.length))
    }
  }

  if (sections.length === 0) {
    throw new Error("lecture_overview.md does not contain any recognizable lecture sections.")
  }

  return { title, sections }
}
