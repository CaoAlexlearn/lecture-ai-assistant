import { expect, test } from "vitest"
import { renderToStaticMarkup } from "react-dom/server"
import { ReadingText, readingParagraphs } from "./ReadingText"

test("long prose gains paragraph breaks without losing words, decimals or citations", () => {
  const text = ("A 3.5% return is compared with MARR. Keep this source [s:B001:abc] attached. ").repeat(16)
  const paragraphs = readingParagraphs(text)
  expect(paragraphs.length).toBeGreaterThan(1)
  expect(paragraphs.join("")).toBe(text)
})

test("only known source identifiers become citation buttons and unknown references remain readable", () => {
  const html = renderToStaticMarkup(<ReadingText text="First [s:B001:abc]. Unknown [s:B002:missing]. <script>alert(1)</script>" citations={["s:B001:abc"]} onCitation={() => {}} />)
  expect(html).toContain('title="s:B001:abc"')
  expect(html).toContain("查看课堂来源 1")
  expect(html).toContain("[s:B002:missing]")
  expect(html).not.toContain("<script>")
  expect(html).toContain("&lt;script&gt;")
})
