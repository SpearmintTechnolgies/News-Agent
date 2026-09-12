---
name: top5
description: Get 5 important current crypto news stories, each with one branded AI image.
version: 6.0.0
author: Spearmint
platforms: [linux]
metadata:
  hermes:
    tags: [crypto, blockchain, news, top5, images]
    category: news
---

# /top5

Production Spearmint crypto-news command.

## COST

Use Gemini 2.5 Flash only.

Low reasoning.

One compact web research pass.

One compact writing pass.

No subagents.

No browser automation.

No WordPress.

No repeated searches.

No full article dumps.

## NEWS

Find exactly five distinct important current crypto/blockchain stories.

Use one broad search.

Use search-result snippets first.

Open only the strongest sources when necessary.

Keep evidence compact:
3000–5000 characters total.

## WRITING

Prepare ALL FIVE stories in one concise Gemini Flash response.

For each story produce:

- headline
- 2 short sentences
- category
- asset when known
- 3–5 tags
- source URL
- one 40–60 word visual prompt

The visual prompt must describe the actual event.

Do not make generic crypto artwork.

## IMAGES

EVERY STORY MUST HAVE ONE IMAGE.

Exactly:

5 stories
5 image generations
1 image per story

Use the active `local-flux` image provider.

Model:
FLUX.2 Klein 9B

Generation:
512×512
1:1
n=1
num_inference_steps=4
guidance_scale=1.0

No references.

No upscaling.

No alternatives.

No regeneration unless the request itself fails.

## LOGO

The exact MemeCoinist logo is applied LOCALLY after generation.

The logo file is:

/home/Rupesh/.hermes/branding/logo.png

Never send the logo to the image model.

Never recreate the logo with AI.

Every final image must contain the exact transparent MemeCoinist logo at the top-left.

## IMAGE DELIVERY

The final branded image is returned as an absolute local file path.

The gateway must deliver that file as native Telegram media.

Do not paste the image URL into the visible response.

Do not describe the local file path to the user.

## IMAGE PROMPT STYLE

Create a premium crypto-news editorial graphic that clearly communicates the exact news event.

The image prompt must describe:

- exact subject
- exact event
- relevant asset/company/protocol/person
- 2–4 concrete visual elements
- clear focal point
- professional editorial composition
- appropriate mood
- cinematic lighting
- sophisticated financial-news aesthetic
- mobile-friendly framing
- clean negative space in the top-left for the exact MemeCoinist logo

### TEXT AND TYPOGRAPHY — HARD REQUIREMENT

Any text rendered inside the image must be:

- perfectly readable
- correctly spelled
- grammatically correct
- intentional
- professionally typeset
- sharp and high contrast
- properly aligned
- properly spaced
- positioned deliberately

NEVER generate:

- gibberish
- random letters
- scrambled words
- misspellings
- duplicated letters
- broken words
- distorted typography
- fake symbols
- unreadable text
- random text placed around the image
- unnecessary text
- paragraphs

Use only the exact requested headline.

Prefer ONE main headline of 3–7 words.

If supporting text is needed, use only ONE very short supporting line.

The headline must be large, clean, sharp, correctly spelled, and immediately readable on a mobile screen.

Do NOT invent extra wording.

Do NOT alter the supplied headline.

Do NOT add fake statistics or claims.

### TEXT POSITIONING

Keep the headline in one clear visual area.

Use a strong hierarchy:

1. small optional category/label
2. large main headline
3. optional tiny supporting line

Never scatter text around the composition.

Keep important text away from edges.

Maintain clean margins and consistent alignment.

### IMAGE QUALITY

Use:

- premium crypto-news editorial visual
- cinematic lighting
- realistic or sophisticated visual treatment
- strong central subject
- clean composition
- high contrast
- professional financial journalism aesthetic
- mobile-first readability

Do NOT create:

- article screenshots
- website screenshots
- fake social-media posts
- fake charts
- fake numbers
- random brand logos
- watermarks
- clutter

### BRANDING

Leave clean negative space in the top-left for the exact MemeCoinist logo.

The MemeCoinist logo is added locally after generation.

Never recreate the logo with AI.

Never send the logo as an image reference to FLUX.

## TELEGRAM

Send each story with its own actual photo.

For each story:

[ACTUAL BRANDED IMAGE]

**<Headline>**

<2 short sentences>

**Category:** <category>
**Asset:** <asset> | **Tags:** <tags>

Source: https://...

Then continue to the next story.

Do not combine all five stories into one giant text response.

Do not send image URLs.

## FINAL RULE

ONE search
ONE writing pass
FIVE stories
FIVE image generations
FIVE locally branded images
FIVE Telegram photo deliveries
