import type { SlideRange } from "../types/lectureMap"

const SLIDE_RANGE_PATTERN = /\bSlides?\s+(\d+)(?:\s*[\u2013\u2014-]\s*(\d+))?/i

export function parseSlideRange(value: string): SlideRange | undefined {
  const match = value.match(SLIDE_RANGE_PATTERN)
  if (!match) return undefined

  const start = Number(match[1])
  const end = Number(match[2] ?? match[1])
  if (!Number.isInteger(start) || !Number.isInteger(end) || start < 1 || end < start) {
    return undefined
  }

  return {
    start,
    end,
    slides: Array.from({ length: end - start + 1 }, (_, index) => start + index),
    label: start === end ? `Slide ${start}` : `Slides ${start}\u2013${end}`,
  }
}

export function stripInlineMarkdown(value: string): string {
  return value
    .replace(/!\[([^\]]*)\]\([^)]*\)/g, "$1")
    .replace(/\[([^\]]+)\]\([^)]*\)/g, "$1")
    .replace(/[*_`~]/g, "")
    .replace(/\\([\\`*{}\[\]()#+\-.!_>])/g, "$1")
    .trim()
}

export function slugify(value: string): string {
  const slug = value
    .normalize("NFKD")
    .toLowerCase()
    .replace(/[^\p{L}\p{N}]+/gu, "-")
    .replace(/^-+|-+$/g, "")
  return slug || "untitled"
}

export function parseList(lines: string[]): string[] {
  const items: string[] = []
  let current = ""

  const flush = () => {
    const value = stripInlineMarkdown(current.replace(/\s+/g, " "))
    if (value) items.push(value)
    current = ""
  }

  for (const rawLine of lines) {
    const line = rawLine.trim()
    const bullet = line.match(/^[-*+]\s+(.+)$/)
    const numbered = line.match(/^\d+[.)]\s+(.+)$/)
    const next = bullet?.[1] ?? numbered?.[1]

    if (next !== undefined) {
      flush()
      current = next
    } else if (line && !/^---+$/.test(line)) {
      current = current ? `${current} ${line}` : line
    }
  }
  flush()
  return items
}

export function uniqueStrings(values: string[]): string[] {
  const seen = new Set<string>()
  return values.filter((value) => {
    const key = value.trim().toLocaleLowerCase()
    if (!key || seen.has(key)) return false
    seen.add(key)
    return true
  })
}
