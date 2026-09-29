# Topic Split Reliability — Logic Flow

```text
Preprocess paragraphs
  -> explicit Markdown headings >= 2?
       yes -> heading sections
       no  -> LLM planner (1,000-token budget)
                -> malformed JSON? one compact repair
                -> valid JSON? validate exact paragraph coverage
                     valid -> LLM topic sections
                     invalid -> conservative paragraph chunks
                -> one/unified plan -> one note
```

No fallback section is allowed to omit or duplicate source paragraphs. A fallback title is blank unless supplied by an explicit heading; the later extraction model supplies a draft title from the section content.
