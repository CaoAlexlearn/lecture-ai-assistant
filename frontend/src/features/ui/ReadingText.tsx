import type { ReactNode } from "react"

// Only add paragraph breaks at sentence boundaries; never summarize or rewrite evidence.
export function readingParagraphs(text: string): string[] {
  return text.split(/\n\s*\n/).filter(Boolean).flatMap((paragraph) => {
    if (paragraph.length < 520 || typeof Intl.Segmenter === "undefined") return [paragraph]
    const sentences = [...new Intl.Segmenter(undefined, { granularity: "sentence" }).segment(paragraph)]
    const result: string[] = []
    let current = ""
    for (const { segment } of sentences) {
      if (current.length >= 320) { result.push(current); current = "" }
      current += segment
    }
    if (current) result.push(current)
    return result
  })
}

export function ReadingText({ text, citations = [], onCitation }: {
  text: string; citations?: string[]; onCitation?: () => void
}) {
  const render = (paragraph: string): ReactNode => {
    if (!citations.length || !onCitation) return paragraph
    const pieces = paragraph.split(/(\[[^\]\n]+\])/g)
    return pieces.map((piece, index) => {
      const number = citations.indexOf(piece.slice(1, -1))
      return piece.startsWith("[") && number >= 0
        ? <sup key={index}><button className="reading-citation" title={piece.slice(1, -1)} aria-label={`查看课堂来源 ${number + 1}`} onClick={onCitation}>{number + 1}</button></sup>
        : piece
    })
  }
  return <>{readingParagraphs(text).map((paragraph, index) => <p key={index}>{render(paragraph)}</p>)}</>
}
