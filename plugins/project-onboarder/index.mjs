import { spawnSync } from "node:child_process";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { definePluginEntry } from "openclaw/plugin-sdk/plugin-entry";

const OPENCLAW_HOME = path.join(os.homedir(), ".openclaw");
const ENGINE_SCRIPT = path.join(
  OPENCLAW_HOME,
  "workspace-orchestrator/skills/pipeline/onboard_project.py",
);
const DB_PATH = path.join(OPENCLAW_HOME, "data/editorial.db");
const OPENCLAW_JSON = path.join(OPENCLAW_HOME, "openclaw.json");
const TELEGRAM_ACCOUNT_ID = "news";
const ONBOARD_MENU_ENTRY = { command: "onboard", description: "Add project" };
const MENU_RECOVERY_DELAY_MS = 15_000;
const INBOUND_MEDIA_DIR = path.join(OPENCLAW_HOME, "media/inbound");
const INBOUND_MEDIA_MAX_AGE_MS = 120_000;
const LOGO_MEDIA_INPUT = "__MEDIA__";

const CALLBACK_PREFIXES = ["ob_confirm:", "ob_preset:"];
const MEDIA_PLACEHOLDER_RE = /^<media:(image|document)/i;

let activeChatsCache = { ids: new Set(), fetchedAt: 0 };
let menuRecoveryTimer = null;

function loadActiveOnboardChats() {
  const now = Date.now();
  if (now - activeChatsCache.fetchedAt < 3000) return activeChatsCache.ids;
  const ids = new Set();
  try {
    const result = spawnSync(
      "sqlite3",
      [DB_PATH, "SELECT DISTINCT chat_id FROM onboard_sessions;"],
      { encoding: "utf8", timeout: 5000 },
    );
    if (result.status === 0 && result.stdout) {
      for (const line of result.stdout.split("\n")) {
        const trimmed = line.trim();
        if (trimmed) ids.add(trimmed);
      }
    }
  } catch {
    // Fail open — no active-chat filtering if sqlite3 unavailable.
  }
  activeChatsCache = { ids, fetchedAt: now };
  return ids;
}

function loadOwnerIds() {
  const owners = new Set();
  try {
    const raw = fs.readFileSync(OPENCLAW_JSON, "utf8");
    const cfg = JSON.parse(raw);
    const ownerAllowFrom = cfg?.commands?.ownerAllowFrom ?? [];
    for (const entry of ownerAllowFrom) {
      const s = String(entry);
      owners.add(s.includes(":") ? s.split(":", 2)[1] : s);
    }
    const groupAllowFrom =
      cfg?.channels?.telegram?.accounts?.[TELEGRAM_ACCOUNT_ID]?.groupAllowFrom ?? [];
    for (const entry of groupAllowFrom) owners.add(String(entry));
  } catch {
    // Fail open — if config unreadable, authorization check below rejects everyone.
  }
  return owners;
}

/**
 * @param {string} chatId
 * @param {string} userId
 * @returns {string | null}
 */
function getOnboardSessionStep(chatId, userId) {
  if (!/^-?\d+$/.test(chatId) || !/^\d+$/.test(userId)) return null;
  try {
    const sql = `SELECT step FROM onboard_sessions WHERE chat_id='${chatId}' AND user_id='${userId}' LIMIT 1;`;
    const result = spawnSync("sqlite3", [DB_PATH, sql], {
      encoding: "utf8",
      timeout: 5000,
    });
    if (result.status === 0) {
      const step = result.stdout.trim();
      if (step) return step;
    }
  } catch {
    // Fail open — step lookup unavailable.
  }
  return null;
}

/**
 * @returns {string | null}
 */
function findRecentInboundMedia() {
  if (!fs.existsSync(INBOUND_MEDIA_DIR)) return null;
  const now = Date.now();
  /** @type {string | null} */
  let best = null;
  let bestMtime = 0;
  let entries;
  try {
    entries = fs.readdirSync(INBOUND_MEDIA_DIR);
  } catch {
    return null;
  }
  for (const name of entries) {
    const full = path.join(INBOUND_MEDIA_DIR, name);
    let st;
    try {
      st = fs.statSync(full);
    } catch {
      continue;
    }
    if (!st.isFile()) continue;
    if (now - st.mtimeMs > INBOUND_MEDIA_MAX_AGE_MS) continue;
    if (st.mtimeMs > bestMtime) {
      bestMtime = st.mtimeMs;
      best = full;
    }
  }
  return best;
}

/**
 * @param {string} text
 * @returns {boolean}
 */
function isMediaPlaceholder(text) {
  return MEDIA_PLACEHOLDER_RE.test(text);
}

/**
 * @param {Record<string, unknown>} event
 * @returns {string | null}
 */
function extractText(event) {
  const fields = [event.content, event.body, event.bodyForAgent];
  for (const field of fields) {
    if (typeof field === "string" && field.trim()) return field.trim();
  }
  return null;
}

function baseChatId(conversationId) {
  const raw = String(conversationId ?? "").trim();
  const topicIdx = raw.indexOf(":topic:");
  return topicIdx >= 0 ? raw.slice(0, topicIdx) : raw;
}

/**
 * @param {string | undefined} from
 * @param {string | undefined} to
 * @returns {string}
 */
function parseTelegramChatId(from, to) {
  for (const raw of [to, from]) {
    if (!raw) continue;
    const s = String(raw).trim();
    const tailMatch = s.match(/(-100\d+|-\d+)$/);
    if (tailMatch) return tailMatch[1];
    if (s.startsWith("telegram:")) {
      const id = s.slice("telegram:".length).split(":").pop() ?? "";
      if (/^-?\d+$/.test(id)) return id;
    }
  }
  return "";
}

/**
 * @param {Record<string, unknown>} event
 * @param {Record<string, unknown>} ctx
 * @returns {boolean}
 */
function isTelegramGroupMessage(event, ctx) {
  if (ctx.channelId && ctx.channelId !== "telegram") return false;
  if (event.channel && event.channel !== "telegram") return false;
  if (event.isGroup === false) return false;
  return true;
}

function runEngine(args) {
  const result = spawnSync("python3", [ENGINE_SCRIPT, ...args], {
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
    return { ok: false, detail: stderr || stdout || `exit ${result.status ?? "unknown"}` };
  }
  return { ok: true, detail: (result.stdout || "").trim() };
}

/**
 * @param {import("openclaw/plugin-sdk/plugins/types").PluginCommandContext} ctx
 * @param {{ warn?: (msg: string) => void; info?: (msg: string) => void }} logger
 * @param {"start" | "cancel"} subcommand
 */
function runOnboardCommand(ctx, logger, subcommand) {
  if (!fs.existsSync(ENGINE_SCRIPT)) {
    logger.warn?.(`project-onboarder: engine missing at ${ENGINE_SCRIPT}`);
    return { text: "Onboarding engine is not installed." };
  }

  const chatId = parseTelegramChatId(ctx.from, ctx.to);
  const userId = String(ctx.senderId ?? "");
  if (!chatId || !userId) {
    return { text: "Could not resolve chat/user for onboarding." };
  }

  const owners = loadOwnerIds();
  if (owners.size > 0 && !owners.has(userId)) {
    logger.warn?.(`project-onboarder: rejected ${subcommand} from unauthorized user ${userId}`);
    return { text: "You are not authorized to use this command." };
  }

  const outcome = runEngine([subcommand, "--chat-id", chatId, "--user-id", userId]);
  if (!outcome.ok) {
    logger.warn?.(`project-onboarder: ${subcommand} failed: ${outcome.detail}`);
    return { text: `Onboarding failed: ${outcome.detail}` };
  }

  activeChatsCache.fetchedAt = 0;
  logger.info?.(`project-onboarder: ${subcommand} via registerCommand for ${chatId}`);
  // Python sends Telegram messages directly; suppress duplicate bot reply.
  return {};
}

/**
 * @param {Record<string, unknown>} cfg
 * @returns {string | null}
 */
function loadNewsBotToken(cfg) {
  const token = cfg?.channels?.telegram?.accounts?.[TELEGRAM_ACCOUNT_ID]?.botToken;
  return typeof token === "string" && token.trim() ? token.trim() : null;
}

/**
 * @param {string} token
 * @param {string} method
 * @param {Record<string, unknown>} body
 */
async function telegramBotApi(token, method, body) {
  const url = `https://api.telegram.org/bot${token}/${method}`;
  const resp = await fetch(url, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  return resp.json();
}

/**
 * Prepend /onboard to the Telegram bot menu if OpenClaw dropped it at the 5700-char budget cap.
 *
 * @param {Record<string, unknown>} cfg
 * @param {{ warn?: (msg: string) => void; info?: (msg: string) => void }} logger
 */
async function ensureOnboardInTelegramMenu(cfg, logger) {
  const token = loadNewsBotToken(cfg);
  if (!token) {
    logger.warn?.("project-onboarder: no news bot token; skipping menu recovery");
    return;
  }

  const getResp = await telegramBotApi(token, "getMyCommands", {});
  if (!getResp?.ok) {
    logger.warn?.(
      `project-onboarder: getMyCommands failed: ${getResp?.description ?? "unknown error"}`,
    );
    return;
  }

  /** @type {Array<{ command: string; description: string }>} */
  const existing = Array.isArray(getResp.result) ? getResp.result : [];
  if (existing.some((entry) => entry.command === "onboard")) {
    logger.info?.("project-onboarder: /onboard already present in Telegram menu");
    return;
  }

  let merged = [ONBOARD_MENU_ENTRY, ...existing.filter((entry) => entry.command !== "onboard")];
  while (merged.length > 0) {
    const setResp = await telegramBotApi(token, "setMyCommands", { commands: merged });
    if (setResp?.ok) {
      logger.info?.(
        `project-onboarder: prepended /onboard to Telegram menu (${merged.length} commands)`,
      );
      return;
    }
    const desc = String(setResp?.description ?? "");
    if (setResp?.error_code === 400 && /BOT_COMMANDS_TOO_MUCH/i.test(desc)) {
      merged = merged.slice(0, -1);
      continue;
    }
    logger.warn?.(`project-onboarder: setMyCommands failed: ${desc || "unknown error"}`);
    return;
  }
  logger.warn?.("project-onboarder: could not fit /onboard into Telegram menu");
}

/**
 * Step answers and inline-keyboard callbacks — handled via before_dispatch.
 * /onboard and /cancel use api.registerCommand (mention-gate bypass).
 *
 * @param {Record<string, unknown>} event
 * @param {Record<string, unknown>} ctx
 * @param {{ info?: (msg: string) => void; warn?: (msg: string) => void }} logger
 * @returns {{ handled: boolean }}
 */
function handleOnboardEvent(event, ctx, logger) {
  try {
    if (!isTelegramGroupMessage(event, ctx)) return { handled: false };

    const text = extractText(event);
    if (!text) return { handled: false };

    const chatId = baseChatId(
      typeof ctx.conversationId === "string"
        ? ctx.conversationId
        : typeof event.conversationId === "string"
          ? event.conversationId
          : "",
    );
    const senderId =
      (typeof ctx.senderId === "string" && ctx.senderId) ||
      (typeof event.senderId === "string" && event.senderId) ||
      "";
    if (!chatId || !senderId) return { handled: false };

    const owners = loadOwnerIds();
    if (owners.size > 0 && !owners.has(senderId)) {
      return { handled: false };
    }

    const sessionStep = getOnboardSessionStep(chatId, senderId);
    const isMedia = isMediaPlaceholder(text);
    const isCallback = CALLBACK_PREFIXES.some((p) => text.startsWith(p));
    const activeChats = loadActiveOnboardChats();
    const isPlausibleAnswer =
      isCallback ||
      activeChats.has(chatId) ||
      (sessionStep === "logo_watermark" && isMedia);

    if (!isPlausibleAnswer) {
      return { handled: false };
    }

    if (!fs.existsSync(ENGINE_SCRIPT)) {
      logger.warn?.(`project-onboarder: engine missing at ${ENGINE_SCRIPT}; falling through`);
      return { handled: false };
    }

    const messageId =
      (typeof ctx.messageId === "string" && ctx.messageId) ||
      (typeof event.messageId === "string" && event.messageId) ||
      "";

    let inputText = text;
    if (sessionStep === "logo_watermark" && isMedia) {
      inputText = LOGO_MEDIA_INPUT;
    }

    /** @type {string[]} */
    const args = ["step", "--chat-id", chatId, "--user-id", String(senderId), "--input", inputText];
    if (sessionStep === "logo_watermark" && isMedia) {
      const mediaPath = findRecentInboundMedia();
      if (mediaPath) {
        args.push("--media-path", mediaPath);
      } else {
        logger.warn?.(
          `project-onboarder: logo step media for ${chatId} but no recent inbound file in ${INBOUND_MEDIA_DIR}`,
        );
      }
    }
    if (messageId) args.push("--reply-to-message-id", messageId);
    const outcome = runEngine(args);

    if (!outcome.ok) {
      logger.warn?.(`project-onboarder: engine failed: ${outcome.detail}`);
      return { handled: false };
    }

    logger.info?.(`project-onboarder: handled step for ${chatId}`);
    activeChatsCache.fetchedAt = 0;
    return { handled: true };
  } catch (err) {
    logger.warn?.(`project-onboarder: unexpected error: ${String(err)}`);
    return { handled: false };
  }
}

export default definePluginEntry({
  id: "project-onboarder",
  name: "Project Onboarder",
  description:
    "Deterministic /onboard flow that adds a complete news-agent project (WordPress verify, categories, authors, Telegram wiring) without waking the orchestrator LLM.",
  register(api) {
    const logger = api.logger;

    api.registerCommand({
      name: "onboard",
      description: "Add a new news-agent project (WordPress, Telegram, scanner)",
      requireAuth: false,
      handler: async (ctx) => runOnboardCommand(ctx, logger, "start"),
    });

    api.registerCommand({
      name: "cancel",
      description: "Cancel an in-progress project onboarding session",
      requireAuth: false,
      handler: async (ctx) => runOnboardCommand(ctx, logger, "cancel"),
    });

    api.registerService({
      id: "onboard-menu-recovery",
      start: (ctx) => {
        if (menuRecoveryTimer) clearTimeout(menuRecoveryTimer);
        menuRecoveryTimer = setTimeout(() => {
          ensureOnboardInTelegramMenu(ctx.config, logger).catch((err) => {
            logger.warn?.(`project-onboarder: menu recovery error: ${String(err)}`);
          });
        }, MENU_RECOVERY_DELAY_MS);
      },
      stop: () => {
        if (menuRecoveryTimer) {
          clearTimeout(menuRecoveryTimer);
          menuRecoveryTimer = null;
        }
      },
    });

    api.on(
      "before_dispatch",
      async (event, ctx) => handleOnboardEvent(event, ctx, logger),
      { priority: 90 },
    );

    api.on(
      "inbound_claim",
      async (event, ctx) => handleOnboardEvent(event, ctx, logger),
      { priority: 90 },
    );
  },
});
