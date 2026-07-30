# MEMECOIN EDITORIAL RULES

Follow these rules for every article. Interior structure (how many sections, headings, hook style) is your editorial decision within the borders below.

---

## Hard rules (never break)

**Content quality**
- **NO EM-DASHES:** Never use `—`. Use a comma, period, or restructure.
- **NATURAL WRITING:** Never use: "it's worth noting", "it is important to note", "delve into", "in conclusion", "furthermore", "moreover", "in summary", "the crypto landscape", "the world of crypto", "a testament to", "shed light on".
- **TONE:** Direct. Trader-focused. Analytical. No hype or fluff ("amazing", "shocking").
- **PARAGRAPHS:** 1–4 sentences max. One idea per paragraph.
- **BULLET LISTS:** 3–4 items only, inside H3 sections if you use them. Never in Conclusion or opening hook.

**SEO and links**
- **ANCHOR LINKS:** Exactly **2** markdown links to URLs from `RESEARCH_JSON.source_urls` (distinct URLs, no repeats). Both in the **opening hook or first H2 only**. No x.com or twitter.com links in the body.
- **KEYWORDS:** Primary keyword in H1 (first 5 words); in the **first sentence of the hook** (the very first line of body text under H1); in at least one H2 and in Conclusion. Use secondary keywords in H2/H3 where natural.

**Length**
- **Word count:** Body **1000–1200** words (aim **1100**). Count body only; META, Sources, and `[Word Count:]` line are not counted. Body includes hook, H2 sections, Conclusion, and FAQs.
- **Verify before footer:** Run the combined validator (writer self-check):
  ```bash
  python3 ~/.openclaw/workspace-writer/skills/article/check_article.py \
    --article <raw.md> \
    --research <validated.json>
  ```
  The `[Word Count: N]` line must match the `word_count_footer` PASS line from the checker — never guess.

**Fixed section order**
```
META → H1 (hook) → H2 body sections → ## Conclusion → ## FAQs → **Sources:** → [Word Count: NNNN]
```
- **## Conclusion** must appear immediately before **## FAQs**.
- **## FAQs** must be the **last** section in the content body (immediately before **Sources:**).
- Do not put FAQs before Conclusion or interleave FAQs in the body.

---

## Structure borders (you decide inside these limits)

| Element | Minimum | Maximum |
|---------|---------|---------|
| H2 body sections (excluding Conclusion, FAQs) | 2 | 4 |
| H3 subsections (`###`) total | 3 | 6 |
| FAQ items under `## FAQs` | 3 | 6 |

Within these borders, you choose:
- How many H2s, H3s, and FAQs to use
- Heading text and angle for each section
- How to open the article (2–4 paragraphs under H1)
- Section depth, bullet lists, and flow

There is **no** prescribed mapping from fact count to section count. Decide what structure best serves **this** story.

Each H3 should draw on distinct material from research; do not pad with empty sections.

---

## META block (required first)

The first word of the file must be **META**.

Count characters in `<thinking>` before writing:

| Field | Your max | Rank Math cap |
|-------|----------|---------------|
| SEO Title | **55** | 60 |
| Meta Description | **155** | 160 |
| URL Slug | **50** | 75 |

```
META
- SEO Title: [Primary Keyword first (within first 3 words); include one number/figure when factual; ≤55 chars]
- Meta Description: [Must contain the Primary Keyword verbatim; hook + detail; ≤155 chars]
- URL Slug: [lowercase hyphens; 3–5 words; ≤50 chars]
- Categories: [e.g. Regulation, Latest News]
- Primary Keyword: [...]
- Secondary Keywords: [...]
```

---

## H1 and body

After META and `---`, write **one** H1:

`# [Primary keyword in first 5 words; include a price or number when relevant]`

Then your hook and H2/H3 body sections. Heading names and section count are your choice within the borders above.

---

## Conclusion (required)

`## Conclusion`

Restate the event with primary keyword and actionable close for traders. Length is your call within the total word band.

---

## FAQs (required, last before Sources)

`## FAQs`

Write questions that fit **this** story (no fixed question bank). **3–6** items within the FAQ border.

Format each item:
```
**1. Your question here?**

Answer paragraph, 3–4 sentences.
```

Renumber 1..N. Do not use markdown list syntax for questions.

---

## Sources footer (required)

Do not use `## Sources` or `## Word Count` as headings.

```
**Sources:**
- [Publication]: [URL]
- [Publication]: [URL]

[Word Count: 1100]
```

Final line: `[Word Count: NNNN]` with digits only.

---

## Pre-flight (in `<thinking>`)

- [ ] META char counts within limits
- [ ] Meta Description contains Primary Keyword verbatim
- [ ] SEO Title starts with Primary Keyword (within first 3 words) and has a number/figure
- [ ] URL Slug ≤50 chars
- [ ] First body sentence (under H1) contains Primary Keyword
- [ ] 2 source URLs planned for hook/first H2
- [ ] Structure choices within H2/H3/FAQ borders
- [ ] Body word budget 1000–1200
- [ ] Order: Conclusion then FAQs then Sources

## Post-output check

- [ ] 1 H1; H2/H3/FAQ counts within borders
- [ ] Conclusion before FAQs; FAQs before Sources
- [ ] 1000–1200 body words
- [ ] Exactly 2 source links in hook/first H2
- [ ] No em-dashes or banned phrases
- [ ] Meta Description contains Primary Keyword
- [ ] First sentence under H1 contains Primary Keyword
