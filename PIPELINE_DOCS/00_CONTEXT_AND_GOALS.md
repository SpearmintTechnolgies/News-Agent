# Project Context & Goals: Autonomous Crypto News Pipeline

## 🌟 The Vision
The goal of this project is to create a fully autonomous, zero-intervention pipeline that acts as an end-to-end publishing house for cryptocurrency news. 

Instead of relying on a single large Language Model to "do everything," this system embraces a **Multi-Agent Architecture** built on the **OpenClaw** framework. By breaking the pipeline down into highly specialized, isolated sub-agents, we guarantee reliability, prevent hallucination, and enforce strict stylistic formatting.

## 🎯 Key Objectives

1. **Autonomous Research (No Paid APIs)**:
   The pipeline must source its own factual data without relying on paid search APIs (like Google or Bing). It achieves this by pulling raw XML from live RSS feeds (CoinTelegraph, CoinDesk, Decrypt) and extracting text via raw HTTP requests.

2. **Single-Topic Deep Dives**:
   Rather than producing generic "roundup" summaries, the system is designed to cross-reference multiple feeds, identify the *single biggest news event* of the day, and aggregate facts from multiple sources into a highly detailed payload.

3. **Strict SEO-Optimized Formatting**:
   The output must not look like generic "AI writing." It must adhere to a strict, trader-focused journalism template. This includes mandatory H1/H2/H3 hierarchies, specific keyword placements, meta descriptions, and bullet-point summaries.

4. **Automated Visuals**:
   Every article must have a highly professional, cinematic feature image generated autonomously via the Leonardo AI API.

5. **Headless Publishing**:
   The final article and image must be seamlessly merged and published directly to Google Drive as a native Google Doc, fully bypassing any need for human copy-pasting.

## 🛠️ The "Why" Behind the Design
- **Why Pandoc?** Google Drive's API strictly strips images when converting Markdown to Google Docs. We use `pandoc` locally to stitch the image into a `.docx` file first, which Google Drive *does* natively support.
- **Why Session Clearing?** AI agents inherently have "memory." If an agent remembers the news from yesterday, it might hallucinate it into today's article. The Orchestrator forcefully deletes the `sessions.json` memory of every worker agent before spawning them to guarantee a 100% clean, stateless execution every single time.
