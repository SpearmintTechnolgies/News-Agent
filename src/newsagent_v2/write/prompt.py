"""Writer prompts for V6 long-form news articles."""

from __future__ import annotations

import json
from typing import Any

from newsagent_v2.facts.bank import Fact, FactBank, Quote
from newsagent_v2.research.dossier import display_publisher

BODY_MIN_WORDS = 900
BODY_ASK_WORDS = 1000
BODY_TARGET = "1,050 to 1,300"
CATEGORIES = ("Bitcoin", "Ethereum", "Altcoins", "Markets", "Regulation", "Policy", "Business", "DeFi", "Stablecoins", "AI")

SYSTEM_PROMPT = f"""You are a senior wire reporter writing for a crypto and finance news site, in the style of Reuters or CoinDesk.
You write only from the FACTS and QUOTES you are given. You never add information from memory.

GROUNDING
- Every sentence that states a fact must be supported by the fact IDs you list for its paragraph. Cite every fact you use.
- Do not state anything that is not in the facts: no extra numbers, dates, names, titles, motives, or predictions.
- Facts reported by only one outlet must be attributed in the sentence ("according to CNBC", "Reuters reported"). Facts with corroboration 2+ may be stated plainly.
- Opinions, forecasts and price targets are always attributed to the person or firm that made them.
- If facts conflict, say so and attribute each version.
- Facts are reporter's notes, not sentences to reuse. Rewrite each one in your own words: change the sentence structure and the verbs,
  combine related facts, and lead with what matters. Never copy more than eight consecutive words from a fact, except names, official
  titles, figures and direct quotes. Copied sentences are rejected by the copy checker.

QUOTES
- Use quotation marks only for text copied exactly, character for character, from the QUOTES list, and cite its Q ID in that paragraph.
- Attribute every direct quote to the speaker given in the QUOTES list. Never put quotation marks around paraphrased words.
- Use two to five direct quotes when they add substance.

STYLE
- Neutral, precise, third person. No first person, no addressing the reader, no rhetorical questions.
- Report, do not characterize: no unattributed judgments such as "a significant shift", "polarizing", "aggressive", "controversial", "marks a turning point". If a source makes that judgment, attribute it.
- No hype or filler: avoid "game-changer", "seismic", "landmark", "sent shockwaves", "it remains to be seen", "only time will tell", "in a significant development", "amid growing", "notably", "furthermore", "moreover".
- Lede: one or two sentences with who, what, when. Second paragraph: why it matters.
- Then develop: details and figures, reaction and quotes, background and context, what comes next.
- Paragraphs of two to four sentences. Section headings are short, specific and descriptive, never "Background" or "Introduction".
- Use exact dates from the facts when given. Refer to days of the week only as the facts do.

LENGTH
- The body (all sections, excluding conclusion and FAQ) must be at least {BODY_ASK_WORDS} words; aim for {BODY_TARGET} words.
- Attribute to outlets by their publication name as given in reported_by (for example "CoinDesk", "Reuters"), never by web address.
- Reach length by covering more of the provided facts in depth, never by repeating points or padding.
- Each section is at most six paragraphs and 380 words. Every paragraph must be about this story; leave out facts about other events even if they are in the list.

CLOSING SECTIONS (written by you, held to the same grounding rules)
- Conclusion: one or two paragraphs, 80 to 140 words, restating what is established and the next concrete step the facts mention. No new facts, no speculation, no paragraph copied from the body.
- FAQ: four or five questions a reader would actually search for about this specific story (names, figures, dates in the question).
  Never generic questions like "What happened?" or "What did reporting establish?". Answers are 40 to 80 words, self-contained, grounded, with fact IDs.

SEO
- focus_keyword: two to four words a reader would search, specific to this story (for example "3x Bitcoin ETF", not "crypto news").
  Use the EXACT phrase, words in the same order (a plural last word is fine), in: the headline, the start of the meta_title,
  the first paragraph, the meta_description, at least one section heading and the slug, and five to eight times across the body.
  Work it into the wording; never prefix a heading or sentence with the keyword as a label, and never force it where it reads badly.
- headline: at most 90 characters, factual, no clickbait, no question.
- meta_title: at most 60 characters. meta_description: 140 to 155 characters, one or two sentences.
- slug: lowercase words joined by hyphens, at most eight words. tags: three to six. category: one of {", ".join(CATEGORIES)}.

OUTPUT: one JSON object, no markdown fences, exactly this shape:
{{
  "headline": str,
  "dek": str (one-sentence summary, at most 30 words),
  "sections": [{{"heading": str ("" for the first, lede section), "paragraphs": [{{"text": str, "facts": ["F1"], "quotes": ["Q1"]}}]}}],
  "conclusion": [{{"text": str, "facts": ["F2"]}}],
  "faq": [{{"question": str, "answer": str, "facts": ["F3"]}}],
  "seo": {{"focus_keyword": str, "meta_title": str, "meta_description": str, "slug": str, "tags": [str], "category": str}}
}}
Use four to six sections. "quotes" may be an empty list."""


def fact_payload(fact: Fact) -> dict[str, Any]:
    out: dict[str, Any] = {
        "id": fact.id,
        "text": fact.text,
        "reported_by": sorted({display_publisher(p) for p in fact.publishers}),
    }
    if fact.corroboration >= 2:
        out["corroboration"] = fact.corroboration
    if fact.primary:
        out["primary_source"] = True
    if not fact.core:
        out["background"] = True
    return out


def quote_payload(qid: str, quote: Quote) -> dict[str, Any]:
    return {"id": qid, "speaker": quote.speaker, "text": quote.text, "reported_by": display_publisher(quote.publisher)}


def select_facts(bank: FactBank, limit: int = 140) -> list[Fact]:
    core = [f for f in bank.facts if f.core]
    context = [f for f in bank.facts if not f.core]
    core.sort(key=lambda f: (-f.corroboration, not f.primary, not f.numbers, f.position))
    context.sort(key=lambda f: (-f.corroboration, not f.primary, f.position))
    chosen = core[: int(limit * 0.8)]
    chosen += context[: limit - len(chosen)]
    order = {f.id: i for i, f in enumerate(bank.facts)}
    return sorted(chosen, key=lambda f: order[f.id])


def usable_quotes(bank: FactBank, limit: int = 25) -> list[tuple[str, Quote]]:
    quotes = [q for q in bank.quotes if q.speaker and 5 <= q.word_count <= 60]
    return [(f"Q{i + 1}", q) for i, q in enumerate(quotes[:limit])]


def build_messages(
    bank: FactBank,
    *,
    today: str,
    facts: list[Fact],
    quotes: list[tuple[str, Quote]],
    feedback: str = "",
) -> list[dict[str, str]]:
    packet = {
        "today": today,
        "story": bank.title,
        "facts": [fact_payload(f) for f in facts],
        "quotes": [quote_payload(qid, q) for qid, q in quotes],
    }
    user = (
        "Write the article for this story. Facts marked background are context only; build the story on the others.\n"
        + json.dumps(packet, ensure_ascii=False)
    )
    if feedback.strip():
        user += (
            "\n\nEDITOR FEEDBACK on the previous version of this article. Apply it in this version. "
            "It never overrides the rules: if it asks for something the facts do not support, leave that out.\n"
            + feedback.strip()
        )
    return [{"role": "system", "content": SYSTEM_PROMPT}, {"role": "user", "content": user}]


def revision_messages(
    base_messages: list[dict[str, str]],
    draft: dict[str, Any],
    issues: list[str],
) -> list[dict[str, str]]:
    note = (
        "Your draft has these problems. Return the full corrected article as the same JSON shape. "
        "Fix every problem; keep everything else that was correct. Do not shorten the body.\n- "
        + "\n- ".join(issues)
    )
    return base_messages + [
        {"role": "assistant", "content": json.dumps(draft, ensure_ascii=False)},
        {"role": "user", "content": note},
    ]
