# Skill: write-article

Use on every **initial write** spawn (not REVISION MODE).

## Steps

1. Read `$RUN_DIR/research/validated.json`.

2. Resolve and read the project template:
   ```bash
   TEMPLATE_PATH=$(python3 ~/.openclaw/workspace-orchestrator/skills/pipeline/project_config.py \
     --path "$PROJECT_CONFIG" --field writer.template_path)
   cat "$HOME/.openclaw/$TEMPLATE_PATH"
   ```
   All editorial rules (META limits, structure borders, tone, links) are in that template — follow it exactly.

3. **Pre-writing plan** — complete all four steps in `<thinking>` before writing any markdown:

   **Step 1 — H2 body sections (2–4 required)**  
   Name every `##` body section (not `###`). Confirm total is 2–4.

   **Step 2 — H3 sub-sections (3–6 required)**  
   For each `###`, state which H2 parent it sits under. Confirm total is 3–6.

   **Step 3 — FAQ questions (3–6 required)**  
   Write each as `**N. Question?**` (not `###`). Confirm total is 3–6.

   **Step 4 — Per-section word budget**
   ```
   words_for_H2_sections = 900 - (N_FAQ × 70)
   max_words_per_H2      = round(words_for_H2_sections / N_H2)
   ```
   Budget: Hook 100 max + (N_H2 × max_words_per_H2) + Conclusion 100 max + (N_FAQ × 70) ≈ 1100.  
   If `max_words_per_H2 < 100`, reduce N_H2 or N_FAQ and recompute.

   Do not write markdown until all counts are within range.

4. Write the full article to `$RUN_DIR/article/raw.md`.

5. Run the self-check loop in [`../article/SKILL.md`](../article/SKILL.md) (max 3 iterations).

6. Yield `SUCCESS` or validator output per SOUL output contract.
