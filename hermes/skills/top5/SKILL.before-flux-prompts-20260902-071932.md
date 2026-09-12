---
name: top5
description: Get 5 important current crypto news stories, each with one branded image.
version: 5.0.0
author: Spearmint
platforms: [linux]
metadata:
  hermes:
    tags: [crypto, blockchain, news, top5, images]
    category: news
---

# /top5

Dedicated Spearmint crypto-news workflow.

## COST SAFETY

Use Gemini 2.5 Flash only.

Low reasoning.

One compact research pass.

One writing pass.

No subagents.

No browser automation.

No WordPress.

No duplicate searches.

No full article dumps.

## NEWS

Find exactly 5 distinct current crypto/blockchain stories.

Use one broad search.

Use search-result snippets first.

Open only the strongest sources when verification is necessary.

Keep useful evidence compact.

Target:
3000–5000 characters total.

## WRITING

Prepare all five stories in ONE Gemini Flash pass.

Each story must contain:

- strong headline
- 2 short sentences
- category
- asset when known
- 3–5 tags
- source URL
- short visual prompt

Visual prompt:
20–30 words.

Do not write long articles.

## IMAGE GENERATION

Normally:

5 stories = 5 image_generate calls.

Exactly one image per story.

Image:
512×512
1:1
FAL FLUX.2 Klein 9B
no upscale
no references
no editing
no regeneration

The exact MemeCoinist logo is added locally.

## CRITICAL IMAGE FAILURE CIRCUIT BREAKER

This rule has highest priority.

If ANY image_generate call returns an error containing ANY of:

- Exhausted balance
- User is locked
- 403
- insufficient balance
- billing
- quota
- unavailable

THEN:

1. DO NOT call image_generate again.
2. DO NOT attempt image 2, 3, 4 or 5.
3. STOP IMAGE GENERATION COMPLETELY for this /top5 run.
4. Continue with the news text only.
5. Do not explain the internal failure in detail.
6. Never repeatedly retry a failed image provider.

One image-generation provider failure means ZERO additional image calls for that run.

This rule exists to protect the image budget.

## TELEGRAM

For each successful image:

[ACTUAL IMAGE]

**<Headline>**

<2 short sentences>

**Category:** <category>
**Asset:** <asset> | **Tags:** <tags>

Source: https://...

Never paste the FAL image URL.

If the image provider is unavailable, send the stories without images rather than repeatedly attempting generation.

## OUTPUT

Only provide the five stories.

No internal process.

No token discussion.

No provider discussion.

No "Here is what I found."

No unnecessary introduction.

## FINAL

ONE search
ONE writing pass
UP TO five image calls

BUT:

IF FIRST IMAGE FAILS FOR BILLING/QUOTA/403:
STOP ALL IMAGE CALLS IMMEDIATELY.
