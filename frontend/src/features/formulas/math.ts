// Presentation-only arithmetic parser. Unsupported prose is preserved as text.
// It never evaluates, simplifies, or adds mathematical facts.
export function unwrapMath(expression: string): string {
  const text = expression.trim()
  for (const [open, close] of [["$$", "$$"], ["$", "$"], ["\\[", "\\]"], ["\\(", "\\)"]]) {
    if (text.startsWith(open) && text.endsWith(close) && text.length >= open.length + close.length) {
      return text.slice(open.length, -close.length).trim()
    }
  }
  return text
}

export function formulaTex(expression: string): string | null {
  const text = unwrapMath(expression)
  if (!text) return null
  if (text.includes("\\")) return text
  if (/\b(when|and|where|if)\b/i.test(text)) return null
  const tokens = text.match(/\.\.\.|[A-Za-z]+|\d+(?:\.\d+)?|[+\-*/^_=(),\[\]{}<>≠≤≥%]/g) ?? []
  if (tokens.join("") !== text.replace(/\s/g, "") || tokens.some((token) => /^[A-Za-z]{4,}$/.test(token))) return null
  let position = 0
  const peek = () => tokens[position]
  const take = () => tokens[position++]
  const atom = (): string => {
    const token = take()
    if (token === undefined) throw new Error("Missing operand")
    if (token === "-" || token === "+") return token + atom()
    let result: string
    if (["(", "[", "{"].includes(token)) {
      const close = { "(": ")", "[": "]", "{": "}" }[token]!
      const inside = sum()
      if (take() !== close) throw new Error("Unbalanced expression")
      result = token === "{" ? `{${inside}}` : `\\left${token}${inside}\\right${close}`
    } else if (/^[A-Za-z0-9]/.test(token) || token === "...") {
      result = token === "..." ? "\\dots" : token
    } else throw new Error("Unsupported token")
    while (peek() === "_" || peek() === "^") {
      const operator = take()
      // Each exponent/subscript consumes one grouped or single atom, not a
      // following exponent. This preserves TeX's original grouping.
      const next = take()
      if (!next) throw new Error("Missing exponent")
      let value = next
      if (["(", "[", "{"].includes(next)) {
        const close = { "(": ")", "[": "]", "{": "}" }[next]!
        value = sum()
        if (take() !== close) throw new Error("Unbalanced exponent")
      } else if (next === "-" || next === "+") value += take() ?? ""
      else if (!/^[A-Za-z0-9]/.test(next)) throw new Error("Invalid exponent")
      result += `${operator}{${value}}`
    }
    return result
  }
  const product = (): string => {
    let result = atom()
    while (peek() && !["+", "-", "=", ",", ")", "]", "}", "<", ">", "≠", "≤", "≥"].includes(peek())) {
      if (peek() === "/") { take(); result = `\\frac{${result}}{${atom()}}` }
      else if (peek() === "*") { take(); result += ` \\cdot ${atom()}` }
      else if (peek() === "%") { take(); result += "\\%" }
      else result += ` ${atom()}`
    }
    return result
  }
  const sum = (): string => {
    let result = product()
    while (["+", "-", "=", ",", "<", ">", "≠", "≤", "≥"].includes(peek())) {
      const operator = take()
      result += `${operator}${product()}`
    }
    return result
  }
  try {
    const result = sum()
    return position === tokens.length ? result : null
  } catch { return null }
}
