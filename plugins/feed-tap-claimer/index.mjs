import { spawnSync } from "node:child_process";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { definePluginEntry } from "openclaw/plugin-sdk/plugin-entry";

const OPENCLAW_HOME = path.join(os.homedir(), ".openclaw");
const HANDLER_SCRIPT = path.join(
  OPENCLAW_HOME,
  "workspace-orchestrator/skills/pipeline/handle_card_feedback.py",
);
const PROJECTS_DIR = path.join(OPENCLAW_HOME, "projects");

const CARD_TAP_PREFIXES = [
  "oc_go:",
  "oc_feed_refresh:",
  "oc_publish:",
  "oc_pub_a:",
  "oc_pub_y:",
  "oc_pub_n:",
  "oc_draft:",
  "oc_draft_yes:",
  "oc_draft_no:",
  "oc_r:",
  "oc_ri:",
  "oc_ri_menu:",
  "oc_edit:",
  "oc_edit_apply:",
  "oc_edit_cancel:",
  "oc_noop:",
];

/** @type {Set<string> | null} */
let allowedGroupIds = null;

function loadAllowedGroupIds() {
  if (allowedGroupIds) return allowedGroupIds;
  /** @type {Set<string>} */
  const ids = new Set();
  try {
    for (const name of fs.readdirSync(PROJECTS_DIR)) {
      if (!name.endsWith(".json")) continue;
      const raw = fs.readFileSync(path.join(PROJECTS_DIR, name), "utf8");
      const cfg = JSON.parse(raw);
      const groupId = cfg?.telegram?.group_id;
      if (typeof groupId === "string" && groupId.trim()) {
        ids.add(groupId.trim());
      } else if (typeof groupId === "number") {
        ids.add(String(groupId));
      }
    }
  } catch {
    // Fail open — group filter skipped if projects dir unreadable.
  }
  allowedGroupIds = ids;
  return ids;
}

/**
 * Telegram callback taps arrive as synthetic inbound messages; callback data is
 * the message text (see telegram bot callback_query → processMessage).
 * before_dispatch exposes it on content + body; inbound_claim also sets
 * bodyForAgent when available.
 *
 * @param {Record<string, unknown>} event
 * @returns {string | null}
 */
function extractFeedTapPayload(event) {
  const fields = [event.content, event.body, event.bodyForAgent];
  for (const field of fields) {
    if (typeof field !== "string") continue;
    const trimmed = field.trim();
    if (CARD_TAP_PREFIXES.some((prefix) => trimmed.startsWith(prefix))) {
      return trimmed;
    }
  }
  return null;
}

/**
 * @param {string | undefined} conversationId
 * @returns {string}
 */
function baseChatId(conversationId) {
  const raw = String(conversationId ?? "").trim();
  const topicIdx = raw.indexOf(":topic:");
  return topicIdx >= 0 ? raw.slice(0, topicIdx) : raw;
}

/**
 * @param {Record<string, unknown>} event
 * @param {Record<string, unknown>} ctx
 * @returns {boolean}
 */
function shouldActOnTap(event, ctx) {
  if (ctx.channelId && ctx.channelId !== "telegram") return false;
  if (event.channel && event.channel !== "telegram") return false;
  if (event.isGroup === false) return false;

  const groups = loadAllowedGroupIds();
  if (groups.size === 0) return true;

  const chatId = baseChatId(
    typeof ctx.conversationId === "string"
      ? ctx.conversationId
      : typeof event.conversationId === "string"
        ? event.conversationId
        : "",
  );
  return groups.has(chatId);
}

/**
 * @param {string} payload
 * @param {Record<string, unknown>} event
 * @param {Record<string, unknown>} ctx
 * @returns {{ ok: boolean; detail?: string }}
 */
function runHandleCardFeedback(payload, event, ctx) {
  const chatId =
    (typeof ctx.conversationId === "string" && ctx.conversationId) ||
    (typeof event.conversationId === "string" && event.conversationId) ||
    "";
  const userId =
    (typeof ctx.senderId === "string" && ctx.senderId) ||
    (typeof event.senderId === "string" && event.senderId) ||
    "";
  const username =
    typeof event.senderUsername === "string" ? event.senderUsername : "";
  const messageId =
    (typeof ctx.messageId === "string" && ctx.messageId) ||
    (typeof event.messageId === "string" && event.messageId) ||
    "";

  /** @type {string[]} */
  const args = [
    HANDLER_SCRIPT,
    "--payload",
    payload,
    "--chat-id",
    chatId,
    "--user-id",
    userId,
    "--username",
    username,
  ];
  if (messageId) {
    args.push("--reply-to-message-id", messageId);
  }

  const result = spawnSync("python3", args, {
    encoding: "utf8",
    timeout: 120_000,
    env: process.env,
  });

  if (result.error) {
    return { ok: false, detail: String(result.error) };
  }
  if (result.status !== 0) {
    const stderr = (result.stderr || "").trim();
    const stdout = (result.stdout || "").trim();
    return {
      ok: false,
      detail: stderr || stdout || `exit ${result.status ?? "unknown"}`,
    };
  }
  return { ok: true };
}

/**
 * Shared handler for before_dispatch (orchestrator-bound taps) and
 * inbound_claim (plugin-bound conversations).
 *
 * @param {Record<string, unknown>} event
 * @param {Record<string, unknown>} ctx
 * @param {{ info?: (msg: string) => void; warn?: (msg: string) => void }} logger
 * @returns {{ handled: boolean }}
 */
function handleFeedTap(event, ctx, logger) {
  try {
    const payload = extractFeedTapPayload(event);
    if (!payload) return { handled: false };
    if (!shouldActOnTap(event, ctx)) return { handled: false };

    if (!fs.existsSync(HANDLER_SCRIPT)) {
      logger.warn?.(
        `feed-tap-claimer: handler missing at ${HANDLER_SCRIPT}; falling through`,
      );
      return { handled: false };
    }

    const outcome = runHandleCardFeedback(payload, event, ctx);
    if (!outcome.ok) {
      logger.warn?.(
        `feed-tap-claimer: handle_card_feedback failed (${payload}): ${outcome.detail}`,
      );
      return { handled: false };
    }

    logger.info?.(`feed-tap-claimer: claimed ${payload.split(":")[0]} tap`);
    return { handled: true };
  } catch (err) {
    logger.warn?.(`feed-tap-claimer: unexpected error: ${String(err)}`);
    return { handled: false };
  }
}

export default definePluginEntry({
  id: "feed-tap-claimer",
  name: "Card Tap Claimer",
  description:
    "Handles Telegram news-card and feed-card callback taps without waking the orchestrator.",
  register(api) {
    const logger = api.logger;

    // Primary path: fires for orchestrator-bound Telegram groups before LLM dispatch.
    api.on(
      "before_dispatch",
      async (event, ctx) => handleFeedTap(event, ctx, logger),
      { priority: 100 },
    );

    // Secondary path: plugin-owned conversation bindings (inbound_claim is targeted there).
    api.on(
      "inbound_claim",
      async (event, ctx) => handleFeedTap(event, ctx, logger),
      { priority: 100 },
    );
  },
});
