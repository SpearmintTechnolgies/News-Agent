import { spawn, spawnSync } from "node:child_process";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { definePluginEntry } from "openclaw/plugin-sdk/plugin-entry";

const OPENCLAW_HOME = path.join(os.homedir(), ".openclaw");
const HANDLER_SCRIPT = path.join(
  OPENCLAW_HOME,
  "workspace-orchestrator/skills/pipeline/handle_card_feedback.py",
);
const FEED_SCRIPT = path.join(
  OPENCLAW_HOME,
  "workspace-orchestrator/skills/pipeline/send_feed_card.py",
);
const QUIET_HOURS_SCRIPT = path.join(
  OPENCLAW_HOME,
  "workspace-orchestrator/skills/pipeline/quiet_hours.py",
);
const PROJECTS_DIR = path.join(OPENCLAW_HOME, "projects");
const SLASH_NEW = /^\/(?:new|reset)(?:@[A-Za-z0-9_]+)?(?:\s|$)/i;

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
const TAP_IN_TEXT = /\b(oc_(?:go|feed_refresh|publish|pub_a|pub_y|pub_n|draft_yes|draft_no|draft|ri_menu|ri|r|edit_apply|edit_cancel|edit|noop):[^\s<>]+)/i;

function extractFeedTapPayload(event) {
  const fields = [
    event.callbackData,
    event.callback_data,
    event.content,
    event.body,
    event.bodyForAgent,
  ];
  for (const field of fields) {
    if (typeof field !== "string") continue;
    const trimmed = field.trim();
    if (!trimmed) continue;
    if (CARD_TAP_PREFIXES.some((prefix) => trimmed.startsWith(prefix))) {
      return trimmed.split(/\s/)[0];
    }
    const labeled = trimmed.match(/callback_data:\s*(\S+)/i);
    if (labeled && CARD_TAP_PREFIXES.some((prefix) => labeled[1].startsWith(prefix))) {
      return labeled[1];
    }
    const embedded = trimmed.match(TAP_IN_TEXT);
    if (embedded) return embedded[1];
  }
  return null;
}

/**
 * Native OpenClaw /new is a session reset that still compiles a huge prompt.
 * Telegram /new@BotName often skips that handler and wakes the LLM, which
 * then reports "context overflow". Treat /new and /reset as feed-card fetch.
 *
 * @param {Record<string, unknown>} event
 * @returns {boolean}
 */
function isSlashNewCommand(event) {
  const fields = [event.content, event.body, event.bodyForAgent];
  for (const field of fields) {
    if (typeof field !== "string") continue;
    const trimmed = field.trim();
    if (trimmed && SLASH_NEW.test(trimmed)) return true;
  }
  return false;
}

/**
 * @param {string} chatId
 * @returns {string}
 */
function resolveProjectForChat(chatId) {
  try {
    for (const name of fs.readdirSync(PROJECTS_DIR)) {
      if (!name.endsWith(".json") || name.startsWith("_")) continue;
      const raw = fs.readFileSync(path.join(PROJECTS_DIR, name), "utf8");
      const cfg = JSON.parse(raw);
      const groupId = cfg?.telegram?.group_id;
      const gid =
        typeof groupId === "number" ? String(groupId) : String(groupId || "").trim();
      if (gid && gid === chatId) return path.basename(name, ".json");
    }
  } catch {
    // Fall through to coinnetwork.
  }
  return "coinnetwork";
}

/**
 * @param {string | undefined} conversationId
 * @returns {string}
 */
function baseChatId(conversationId) {
  let raw = String(conversationId ?? "").trim();
  if (raw.startsWith("telegram:")) raw = raw.slice("telegram:".length);
  const topicIdx = raw.indexOf(":topic:");
  return topicIdx >= 0 ? raw.slice(0, topicIdx) : raw;
}

function resolvePython() {
  const candidates = [
    process.env.OPENCLAW_PYTHON,
    path.join(os.homedir(), "AppData/Local/Programs/Python/Python311/python.exe"),
    path.join(os.homedir(), "AppData/Local/Programs/Python/Python312/python.exe"),
    "python",
    "python3",
  ].filter(Boolean);
  for (const bin of candidates) {
    try {
      const probe = spawnSync(bin, ["-c", "print(1)"], {
        encoding: "utf8",
        timeout: 8000,
        windowsHide: true,
      });
      if (probe.status === 0) return bin;
    } catch {
      // try next
    }
  }
  return process.platform === "win32" ? "python" : "python3";
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
  const chatId = baseChatId(
    (typeof ctx.conversationId === "string" && ctx.conversationId) ||
      (typeof event.conversationId === "string" && event.conversationId) ||
      "",
  );
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

  const result = spawnSync(resolvePython(), args, {
    encoding: "utf8",
    timeout: 120_000,
    env: {
      ...process.env,
      HOME: os.homedir(),
      PYTHONIOENCODING: "utf-8",
    },
    windowsHide: true,
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
 * Detach send_feed_card so before_dispatch returns immediately (no LLM).
 *
 * @param {string} project
 * @returns {{ ok: boolean; detail?: string }}
 */
function runSendFeedCard(project) {
  if (!fs.existsSync(FEED_SCRIPT)) {
    return { ok: false, detail: `missing ${FEED_SCRIPT}` };
  }
  const logPath = path.join(os.tmpdir(), `feed-new-${project}.log`);
  let logFd;
  try {
    logFd = fs.openSync(logPath, "a");
    const child = spawn(
      resolvePython(),
      [FEED_SCRIPT, "--project", project, "--ensure-pool"],
      {
        detached: true,
        stdio: ["ignore", logFd, logFd],
        windowsHide: true,
        env: {
          ...process.env,
          HOME: os.homedir(),
          PYTHONIOENCODING: "utf-8",
        },
      },
    );
    child.unref();
    return { ok: true };
  } catch (err) {
    return { ok: false, detail: String(err) };
  } finally {
    if (logFd !== undefined) {
      try {
        fs.closeSync(logFd);
      } catch {
        // inherited by child
      }
    }
  }
}

/**
 * @param {Record<string, unknown>} event
 * @param {Record<string, unknown>} ctx
 * @returns {boolean}
 */
function isQuietHours() {
  try {
    const result = spawnSync(resolvePython(), [QUIET_HOURS_SCRIPT, "--check"], {
      encoding: "utf8",
      timeout: 5000,
      env: { ...process.env, HOME: os.homedir(), PYTHONIOENCODING: "utf-8" },
      windowsHide: true,
    });
    if (result.status !== 0) return false;
    return (result.stdout || "").trim() === "true";
  } catch {
    return false;
  }
}

/**
 * @param {string} chatId
 * @param {string} messageId
 * @param {{ warn?: (msg: string) => void }} logger
 */
function sendSnoozeReply(chatId, messageId, logger) {
  /** @type {string[]} */
  const args = [QUIET_HOURS_SCRIPT, "--send-snooze", "--chat-id", chatId];
  if (messageId) args.push("--reply-to-message-id", messageId);
  const result = spawnSync(resolvePython(), args, {
    encoding: "utf8",
    timeout: 15000,
    env: { ...process.env, HOME: os.homedir(), PYTHONIOENCODING: "utf-8" },
    windowsHide: true,
  });
  if (result.status !== 0) {
    logger.warn?.(
      `feed-tap-claimer: snooze reply failed: ${(result.stderr || result.stdout || "").trim()}`,
    );
  }
}

/**
 * Block all project-group Telegram inbound during quiet hours (no LLM dispatch).
 *
 * @param {Record<string, unknown>} event
 * @param {Record<string, unknown>} ctx
 * @param {{ info?: (msg: string) => void; warn?: (msg: string) => void }} logger
 * @returns {{ handled: boolean }}
 */
function handleQuietHoursBlock(event, ctx, logger) {
  if (!shouldActOnTap(event, ctx)) return { handled: false };
  if (!isQuietHours()) return { handled: false };

  const chatId =
    (typeof ctx.conversationId === "string" && ctx.conversationId) ||
    (typeof event.conversationId === "string" && event.conversationId) ||
    "";
  const messageId =
    (typeof ctx.messageId === "string" && ctx.messageId) ||
    (typeof event.messageId === "string" && event.messageId) ||
    "";

  if (chatId) {
    sendSnoozeReply(baseChatId(chatId), messageId, logger);
  }

  logger.info?.("feed-tap-claimer: blocked inbound during quiet hours");
  return { handled: true };
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
    if (!shouldActOnTap(event, ctx)) return { handled: false };

    if (isSlashNewCommand(event)) {
      const chatId = baseChatId(
        (typeof ctx.conversationId === "string" && ctx.conversationId) ||
          (typeof event.conversationId === "string" && event.conversationId) ||
          "",
      );
      const project = resolveProjectForChat(chatId);
      if (!fs.existsSync(FEED_SCRIPT)) {
        logger.warn?.(
          `feed-tap-claimer: send_feed_card missing at ${FEED_SCRIPT}; falling through`,
        );
        return { handled: false };
      }
      const outcome = runSendFeedCard(project);
      if (!outcome.ok) {
        logger.warn?.(
          `feed-tap-claimer: /new feed post failed (${project}): ${outcome.detail}`,
        );
        return { handled: false };
      }
      logger.info?.(`feed-tap-claimer: claimed /new for ${project}`);
      return { handled: true };
    }

    const payload = extractFeedTapPayload(event);
    if (!payload) return { handled: false };

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
    "Handles Telegram /new, news-card, and feed-card taps without waking the orchestrator.",
  register(api) {
    const logger = api.logger;

    // Quiet hours: block all project-group inbound before taps or orchestrator dispatch.
    api.on(
      "before_dispatch",
      async (event, ctx) => handleQuietHoursBlock(event, ctx, logger),
      { priority: 200 },
    );
    api.on(
      "inbound_claim",
      async (event, ctx) => handleQuietHoursBlock(event, ctx, logger),
      { priority: 200 },
    );

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
