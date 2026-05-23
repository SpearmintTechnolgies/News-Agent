# WordPress Publisher Agent Implementation

This plan outlines the steps required to create a brand new, dedicated WordPress Publisher Agent (`wp-publisher`), while keeping the original Google Drive Publisher intact. The Orchestrator will act as an interactive manager to sequence them based on your approval.

## The Simplified Workflow

1. **Phase 1: The Draft & Preview**
   The Orchestrator runs its normal pipeline. It uses the existing `publisher` agent to upload the DOCX to Google Drive and delivers the link to you. It then waits.
2. **Phase 2: The Decision**
   You review the Google Doc. You can ignore it, or you can send a new message to the Orchestrator like: *"Publish it"* or *"Draft it to WordPress"*.
3. **Phase 3: The WP Publisher**
   The Orchestrator reads your command and explicitly triggers the new `wp-publisher` agent. This agent takes the existing `/tmp/crypto-article.md` and `/tmp/crypto-feature.jpg` and pushes them to WordPress.

## Proposed Changes

### 1. Create the New WP Publisher Agent (workspace-wp-publisher/SOUL.md)
We will create a completely new agent folder (`workspace-wp-publisher`) dedicated purely to WordPress.
- **Thinking Required:** The agent will be forced to use a `<thinking>` block to map out the exact HTML conversion and API payload before running its publishing commands.
- **Authentication:** Use a secure WordPress Application Password stored in the environment variables (`$WP_APP_PASSWORD`).
- **Workflow:**
  1. POST `/tmp/crypto-feature.jpg` to `https://<YOUR-SITE>/wp-json/wp/v2/media` and extract the image ID using `jq`.
  2. Run `pandoc /tmp/crypto-article.md -t html -o /tmp/article.html` to convert the markdown into HTML.
  3. POST to `https://<YOUR-SITE>/wp-json/wp/v2/posts` with the HTML content and extracted image ID. It will set the status to `draft` or `publish` based on what the Orchestrator tells it.

### 2. Update the Orchestrator (workspace-orchestrator/SOUL.md)
We will simply add a rule to the bottom of the Orchestrator's SOUL:
- *"If the user says to 'publish' or 'draft' an article to WordPress, do not run the full pipeline. Instead, trigger the `wp-publisher` agent to upload the current article in the `/tmp` folder."*

## User Review Required

> [!IMPORTANT]
> This is the isolated blueprint for the WordPress Publisher integration.
> **No code changes will be made yet.** Once you receive the true template from your team and are ready to wire up the website, we will execute this plan.
