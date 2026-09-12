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

Premium crypto-news editorial visual.

The prompt must identify:

- exact subject
- exact event
- 2–4 concrete visual elements
- editorial composition
- appropriate mood
- cinematic lighting
- sophisticated financial-news aesthetic
- clean top-left space for the MemeCoinist logo

No fake statistics.

No fake charts.

No random logos.

No watermark.

No article screenshot.

## FAILURE SAFETY

If image generation fails due to:

- billing
- quota
- unavailable
- 403
- 429
- connection failure

STOP further image generation for this `/top5` run.

Do not retry five times.

Do not generate replacement images.

Continue with concise text only for the affected stories.

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
