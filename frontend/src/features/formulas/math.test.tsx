import { createElement } from "react"
import { renderToStaticMarkup } from "react-dom/server"
import { describe, expect, it } from "vitest"
import { formulaTex } from "./math"
import { MathFormula, mathMarkup } from "./MathFormula"

describe("formula presentation without changing evidence", () => {
  it("formats arithmetic fractions, powers, and subscripts", () => {
    const tex = formulaTex("F = A_1*((1+i)^N - (1+f)^N)/(i-f)")!
    expect(tex).toContain("A_{1}")
    expect(tex).toContain("\\frac")
    expect(tex).toContain("^{N}")
    expect(mathMarkup("P = F/(1+i)^N")).toContain("<mfrac>")
    expect(mathMarkup("P = F/(1+i)^N")).toContain("<msup>")
  })
  it("renders original conditional cases rather than flattening them", () => {
    const cases = String.raw`F = \left\{\begin{array}{ll} A_1\frac{(1+i)^N-(1+f)^N}{i-f}, & f\ne i \\ A_1 N(1+i)^{N-1}, & f=i \end{array}\right.`
    const html = mathMarkup(cases, true)!
    expect(html).toContain("<mtable")
    expect(html).toContain("<mfrac>")
    expect(html).toContain("≠")
  })
  it("preserves original factor notation slash and native LaTeX", () => {
    expect(mathMarkup("F/P", true)).not.toContain("<mfrac>")
    expect(formulaTex(String.raw`\frac{F}{P}\text{ when }i>0`)).toContain("\\text")
    expect(formulaTex("$$P=F/(1+i)^N$$")).toContain("\\frac")
    expect(mathMarkup(String.raw`$$P=\frac{F}{(1+i)^N}$$`, true)).toContain("<mfrac>")
    expect(mathMarkup(String.raw`\(F=P(1+i)^N\)`, true)).toContain("<msup>")
  })
  it("keeps unsupported prose or invalid syntax visibly available", () => {
    expect(formulaTex("F = A when f=i, and B when f!=i")).toBeNull()
    expect(formulaTex("F = P/(")).toBeNull()
    expect(mathMarkup(String.raw`\unsupportedMacro{F}`)).toBeNull()
    const html = renderToStaticMarkup(createElement(MathFormula, { expression: "F = A when f=i" }))
    expect(html).toContain("F = A when f=i")
    expect(html).toContain("请核对来源 PPT")
  })
  it("does not trust source HTML or executable links", () => {
    const html = renderToStaticMarkup(createElement(MathFormula, { expression: '<img src=x onerror="alert(1)">' }))
    expect(html).not.toContain("<img")
    expect(html).toContain("&lt;img")
    expect(mathMarkup(String.raw`\href{javascript:alert(1)}{x}`, true)).not.toContain("href=\"javascript:")
  })
})
