import { createElement } from "react"
import { renderToStaticMarkup } from "react-dom/server"
import { expect, test } from "vitest"
import { parseScaffold } from "../knowledge-scaffold/scaffold"
import { PreparationKnowledgePreview } from "./PreparationKnowledgePreview"

const base = { node_id: "KN_0001", title: "Present value", parent_id: null, children: [], order: 0, aliases: [], source_refs: [{ type: "slide", page: 3 }] }

test("preparation preview renders committed v2 formula content without legacy prelecture fields", () => {
  const scaffold = parseScaffold({ schema_version: "2.0", title: "Economics", root_nodes: [base.node_id], relations: [], nodes: {
    [base.node_id]: { ...base, node_type: "formula", scope: "Discount a future payment", evidence: [], content: {
      expression: "P = F / (1 + i)^n", variables: [{ symbol: "i", meaning: "interest rate" }], interpretation: "Find the equivalent value today.", assumptions: ["Constant interest rate"],
    } },
  } })
  const html = renderToStaticMarkup(createElement(PreparationKnowledgePreview, { scaffold, materialUrl: "/slides.pdf" }))
  expect(html).toContain("P = F / (1 + i)^n")
  expect(html).toContain("interest rate")
  expect(html).toContain("Constant interest rate")
  expect(html).toContain("Slide 3")
})

test("preparation preview still renders legacy course packages through the shared parser", () => {
  const scaffold = parseScaffold({ schema_version: 1, title: "Economics", root_nodes: [base.node_id], nodes: {
    [base.node_id]: { ...base, node_type: "concept", relations: {}, prelecture: {
      core_idea: "Money has a time value", definition: "The current value of a future payment.", formula: null,
      key_terms: ["discounting"], variables: {}, steps: [], comparison: null, slide_examples: ["A payment due next year."],
    } },
  } })
  const html = renderToStaticMarkup(createElement(PreparationKnowledgePreview, { scaffold }))
  expect(html).toContain("The current value of a future payment.")
  expect(html).toContain("Money has a time value")
  expect(html).toContain("A payment due next year.")
})
