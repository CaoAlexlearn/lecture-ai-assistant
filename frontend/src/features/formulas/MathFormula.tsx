import katex from "katex"
import "katex/dist/katex.min.css"
import { formulaTex, unwrapMath } from "./math"

export function mathMarkup(expression: string, originalLatex = false): string | null {
  const tex = originalLatex ? unwrapMath(expression) : formulaTex(expression)
  if (!tex) return null
  try {
    return katex.renderToString(tex, { displayMode: true, throwOnError: true, trust: false, strict: "ignore", maxExpand: 200, maxSize: 20 })
  } catch { return null }
}

export function MathFormula({ expression, originalLatex = false }: { expression: string; originalLatex?: boolean }) {
  const markup = mathMarkup(expression, originalLatex)
  return markup ? <div className="formula-math" dangerouslySetInnerHTML={{ __html: markup }} />
    : <div className="formula-math formula-math--fallback"><p>{expression}</p><small>原文表达式 · 排版未识别，请核对来源 PPT</small></div>
}
