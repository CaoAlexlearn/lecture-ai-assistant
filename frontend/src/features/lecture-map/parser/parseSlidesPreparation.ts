import type { SlidePreparation } from "../types/lectureMap"
import { parseList, stripInlineMarkdown, uniqueStrings } from "./shared"

type SlideBlock = {
  slideNumber: number
  title: string
  lines: string[]
}

function splitSlideBlocks(markdown: string): SlideBlock[] {
  const blocks: SlideBlock[] = []
  let current: SlideBlock | undefined

  for (const line of markdown.replace(/\r\n?/g, "\n").split("\n")) {
    const match = line.match(/^#{2,4}\s+Slide\s+(\d+)\s*[\u2013\u2014-]\s*(.+?)\s*$/i)
    if (match) {
      if (current) blocks.push(current)
      current = {
        slideNumber: Number(match[1]),
        title: stripInlineMarkdown(match[2]),
        lines: [],
      }
    } else if (current) {
      current.lines.push(line)
    }
  }
  if (current) blocks.push(current)
  return blocks
}

function classifyHeading(value: string): keyof Omit<SlidePreparation, "slideNumber" | "title" | "role"> | undefined {
  const heading = stripInlineMarkdown(value).toLowerCase().replace(/\s+\d+$/, "")
  if (heading === "key points" || heading === "key point") return "keyPoints"
  if (heading === "example" || heading === "examples") return "examples"
  if (heading === "question" || heading === "questions") return "questions"
  if (heading === "terms" || heading === "term") return "terms"
  if (heading === "visual" || heading === "visuals") return "visuals"
  if (heading === "source-provided answer" || heading === "source-provided answers") return "sourceAnswers"
  return undefined
}

function parseSlide(block: SlideBlock): SlidePreparation {
  const fields: Record<"keyPoints" | "examples" | "questions" | "terms" | "visuals" | "sourceAnswers", string[]> = {
    keyPoints: [],
    examples: [],
    questions: [],
    terms: [],
    visuals: [],
    sourceAnswers: [],
  }
  const buckets = new Map<keyof typeof fields, string[]>()
  let active: keyof typeof fields | undefined
  const preamble: string[] = []
  let role: string | undefined

  for (const line of block.lines) {
    const roleMatch = line.match(/^\s*(?:\*\*)?Role:(?:\*\*)?\s*(.+?)\s*$/i)
    if (roleMatch) {
      role = stripInlineMarkdown(roleMatch[1])
      continue
    }

    const heading = line.match(/^###\s+(.+?)\s*$/)
    if (heading) {
      active = classifyHeading(heading[1])
      if (active && !buckets.has(active)) buckets.set(active, [])
      continue
    }

    if (active) buckets.get(active)?.push(line)
    else preamble.push(line)
  }

  for (const key of Object.keys(fields) as Array<keyof typeof fields>) {
    fields[key] = parseList(buckets.get(key) ?? [])
  }
  if (fields.keyPoints.length === 0) fields.keyPoints = parseList(preamble)

  return {
    slideNumber: block.slideNumber,
    title: block.title,
    role,
    ...fields,
    terms: uniqueStrings(fields.terms),
  }
}

export function parseSlidesPreparation(markdown: string): Map<number, SlidePreparation> {
  const slides = new Map<number, SlidePreparation>()
  for (const block of splitSlideBlocks(markdown)) {
    if (slides.has(block.slideNumber)) {
      throw new Error(`slides_preparation.md contains duplicate Slide ${block.slideNumber}.`)
    }
    slides.set(block.slideNumber, parseSlide(block))
  }
  if (slides.size === 0) {
    throw new Error("slides_preparation.md does not contain any recognizable slides.")
  }
  return slides
}
