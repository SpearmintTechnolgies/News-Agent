# COINOGRAPHY EDITORIAL RULES

Follow these rules for every article. Interior structure (how many sections, headings, hook style) is your editorial decision within the borders below.

<constraints>
  <!-- Hard schema. Validate every number against this BEFORE writing. The article
       itself is plain Markdown — NEVER emit these XML tags in your output. -->
  <h1 count="1" max_words="15"/>
  <h2_body min="2" max="4"/>            <!-- excludes ## Conclusion and ## FAQs -->
  <h3 min="3" max="6"/>                 <!-- ### only, nested inside an H2 -->
  <faqs min="3" max="6" format="**N. Question?**"/>
  <body_words aim="1100" min="950" max="1250"/>  <!-- hook+H2s+Conclusion+FAQs -->
  <anchor_links count="2" placement="hook_or_first_H2" no_tweets="true" distinct="true"
                source="research.source_urls"/>
  <meta seo_title_max="55" meta_description_max="155" url_slug_max="50"
        keyword_in_description="verbatim"/>
  <order>META -> H1(hook) -> H2 body -> ## Conclusion -> ## FAQs -> **Sources:** -> [Word Count: N]</order>
  <style no_em_dash="true" no_banned_phrases="true"/>
  <output>Clean Markdown only. Do NOT print XML tags, schema, or your plan in the article file.</output>
</constraints>

---

## Hard rules (never break)

**Content quality**
- **NO EM-DASHES:** Never use `—`. Use a comma, period, or restructure.
- **NATURAL WRITING:** Never use: "it's worth noting", "it is important to note", "delve into", "in conclusion", "furthermore", "moreover", "in summary", "the crypto landscape", "the world of crypto", "a testament to", "shed light on".
- **TONE:** Direct. Trader-focused. Analytical. No hype or fluff ("amazing", "shocking").
- **PARAGRAPHS:** 1–4 sentences max. One idea per paragraph.
- **BULLET LISTS:** 3–4 items only, inside H3 sections if you use them. Never in Conclusion or opening hook.

**SEO and links**
- **ANCHOR LINKS:** Exactly **2** markdown links to URLs from `RESEARCH_JSON.source_urls` (distinct URLs, no repeats). Both in the **opening hook or first H2 only** — woven into sentences, never a standalone `Publication | Publication` line. No x.com or twitter.com links in the body.
- **HEADINGS:** One clean `#` / `##` / `###` marker per line — never put `#` characters inside heading text.
- **KEYWORDS:** Primary keyword in H1 (first 5 words); in the **first sentence of the hook** (the very first line of body text under H1); in at least one H2 and in Conclusion. Use secondary keywords in H2/H3 where natural.

**Length**
- **Word count:** Aim for **~1100** body words. Accepted band is **950–1250** (validator). Count body only; META, Sources, and `[Word Count:]` line are not counted. Body includes hook, H2 sections, Conclusion, and FAQs.
- **Verify before footer:** Run the combined validator:
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

**Heading hierarchy — MANDATORY, never deviate:**

| Level | Markdown | Role |
|-------|----------|------|
| H1 `#` | `# Article Title` | Article title — exactly one, at the very top |
| H2 `##` | `## Section Name` | Main body sections (2–4) + `## Conclusion` + `## FAQs` |
| H3 `###` | `### Subsection` | Sub-sections nested inside an H2 only |

**NEVER use `###` for a main body section — that must be `##`.**
**NEVER use `###` for FAQ questions — use `**1. Question?**` (see FAQs section below).**

Article skeleton — fill in your own heading text, do not copy it literally:

```
META
...

---

# H1 Title Here

[Hook paragraphs — 2–4 sentences under H1, no heading]

## First Main Section       ← H2
### Sub-section (optional)  ← H3 nested under the H2

## Second Main Section      ← H2
### Sub-section (optional)  ← H3 nested under the H2

[0–2 more ## body sections, each optionally with ### sub-sections]

## Conclusion               ← H2

## FAQs                     ← H2
**1. Question one?**

Answer paragraph.

**2. Question two?**

Answer paragraph.

[up to 6 FAQ items]

**Sources:**
- Publication: URL

[Word Count: 1100]
```

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
- URL Slug: [lowercase hyphens; 3–5 words; must include Primary Keyword tokens; ≤50 chars]
- Categories: [e.g. Regulation, Latest News]
- Primary Keyword: [...]
- Secondary Keywords: [...]
```

---

## H1 and body

After META and `---`, write **one** H1:

`# [Primary keyword in first 5 words; include a price or number when relevant]`

Then your hook and body. Use `##` for main sections and `###` only for sub-sections nested inside an `##`. Heading names and section count are your choice within the borders above.

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
**Never use `### ` heading syntax for FAQ questions.** Each item must open with `**N. Your question?**` on its own line — no exceptions.

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
- [ ] URL Slug includes Primary Keyword tokens and is ≤50 chars
- [ ] First body sentence (under H1) contains Primary Keyword
- [ ] 2 source URLs planned for hook/first H2
- [ ] Structure choices within H2/H3/FAQ borders
- [ ] All main body sections use `##`; sub-sections use `###`
- [ ] FAQ items use `**N. Question?**`, not `###`
- [ ] Body word budget ~1100 (accepted 950–1250)
- [ ] Order: Conclusion then FAQs then Sources

## Post-output check

- [ ] 1 H1; H2/H3/FAQ counts within borders; main sections use `##`, sub-sections use `###`
- [ ] Conclusion before FAQs; FAQs before Sources
- [ ] ~1100 body words (accepted 950–1250)
- [ ] Exactly 2 source links in hook/first H2
- [ ] No em-dashes or banned phrases
- [ ] Meta Description contains Primary Keyword
- [ ] First sentence under H1 contains Primary Keyword
