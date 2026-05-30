-- backlink_agent.db — single source of truth for backlink workflows (v3)

CREATE TABLE IF NOT EXISTS niches (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT NOT NULL,
  slug TEXT NOT NULL UNIQUE,
  status TEXT NOT NULL DEFAULT 'active',
  search_queries_json TEXT,
  config_json TEXT,
  created_at TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS projects (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  niche_id INTEGER NOT NULL REFERENCES niches(id),
  name TEXT NOT NULL,
  target_domain TEXT NOT NULL,
  target_url TEXT,
  brand_name TEXT,
  status TEXT NOT NULL DEFAULT 'active',
  config_json TEXT,
  created_at TEXT DEFAULT (datetime('now')),
  UNIQUE(niche_id, target_domain)
);
CREATE INDEX IF NOT EXISTS idx_projects_niche ON projects(niche_id);

CREATE TABLE IF NOT EXISTS campaigns (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT NOT NULL,
  target_domain TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'active',
  created_at TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS audits (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  workflow_id TEXT NOT NULL REFERENCES workflows(workflow_id),
  opportunity_id INTEGER REFERENCES opportunities(id),
  audit_json TEXT NOT NULL,
  placement_type TEXT,
  image_required INTEGER NOT NULL DEFAULT 0,
  audit_score REAL,
  pass INTEGER NOT NULL DEFAULT 0,
  created_at TEXT DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_audits_workflow ON audits(workflow_id);

CREATE TABLE IF NOT EXISTS content_assets (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  workflow_id TEXT NOT NULL REFERENCES workflows(workflow_id),
  version INTEGER NOT NULL DEFAULT 1,
  content_text TEXT NOT NULL,
  target_link TEXT,
  image_url TEXT,
  image_local_path TEXT,
  image_prompt TEXT,
  content_type TEXT,
  confidence REAL,
  approved INTEGER NOT NULL DEFAULT 0,
  created_at TEXT DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_content_assets_workflow ON content_assets(workflow_id, version);

CREATE TABLE IF NOT EXISTS feedback_events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  workflow_id TEXT NOT NULL REFERENCES workflows(workflow_id),
  action TEXT NOT NULL,
  user_id TEXT,
  edit_prompt TEXT,
  content_version_before INTEGER,
  content_version_after INTEGER,
  raw_payload_json TEXT,
  created_at TEXT DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_feedback_workflow ON feedback_events(workflow_id);

CREATE TABLE IF NOT EXISTS opportunities (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  campaign_id INTEGER REFERENCES campaigns(id),
  project_id INTEGER REFERENCES projects(id),
  url TEXT NOT NULL,
  normalized_url TEXT NOT NULL,
  url_hash TEXT NOT NULL,
  domain TEXT,
  title TEXT,
  snippet TEXT,
  placement_type TEXT,
  placement_allowed INTEGER,
  context_json TEXT,
  relevance_score REAL,
  spam_risk_score REAL,
  final_score REAL,
  status TEXT NOT NULL DEFAULT 'open',
  created_at TEXT DEFAULT (datetime('now')),
  updated_at TEXT DEFAULT (datetime('now')),
  UNIQUE(campaign_id, url_hash)
);
CREATE INDEX IF NOT EXISTS idx_opportunities_campaign ON opportunities(campaign_id);
CREATE INDEX IF NOT EXISTS idx_opportunities_url_hash ON opportunities(url_hash);
CREATE INDEX IF NOT EXISTS idx_opportunities_domain ON opportunities(domain);

CREATE TABLE IF NOT EXISTS workflows (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  workflow_id TEXT NOT NULL UNIQUE,
  campaign_id INTEGER REFERENCES campaigns(id),
  project_id INTEGER REFERENCES projects(id),
  opportunity_id INTEGER REFERENCES opportunities(id),
  state TEXT NOT NULL DEFAULT 'NEW',
  current_agent TEXT,
  last_error TEXT,
  retry_count INTEGER NOT NULL DEFAULT 0,
  pending_approval_at TEXT,
  approval_expires_at TEXT,
  next_verify_at TEXT,
  created_at TEXT DEFAULT (datetime('now')),
  updated_at TEXT DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_workflows_state ON workflows(state);
CREATE INDEX IF NOT EXISTS idx_workflows_opportunity ON workflows(opportunity_id);

CREATE TABLE IF NOT EXISTS drafts (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  workflow_id TEXT NOT NULL REFERENCES workflows(workflow_id),
  version INTEGER NOT NULL DEFAULT 1,
  draft_text TEXT NOT NULL,
  tone TEXT,
  confidence REAL,
  approved INTEGER NOT NULL DEFAULT 0,
  created_at TEXT DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_drafts_workflow ON drafts(workflow_id, version);

CREATE TABLE IF NOT EXISTS approvals (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  workflow_id TEXT NOT NULL REFERENCES workflows(workflow_id),
  draft_id INTEGER REFERENCES drafts(id),
  telegram_chat_id TEXT,
  telegram_message_id INTEGER,
  decision TEXT,
  callback_data TEXT,
  user_id TEXT,
  decided_at TEXT,
  created_at TEXT DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_approvals_workflow ON approvals(workflow_id);
CREATE INDEX IF NOT EXISTS idx_approvals_tg ON approvals(telegram_chat_id, telegram_message_id);

CREATE TABLE IF NOT EXISTS edit_sessions (
  workflow_id TEXT PRIMARY KEY REFERENCES workflows(workflow_id),
  state TEXT NOT NULL,
  edit_prompt TEXT,
  user_id TEXT,
  updated_at TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS backlinks (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  workflow_id TEXT NOT NULL UNIQUE REFERENCES workflows(workflow_id),
  opportunity_id INTEGER REFERENCES opportunities(id),
  published_url TEXT,
  status TEXT,
  verified INTEGER NOT NULL DEFAULT 0,
  verified_at TEXT,
  screenshot_path TEXT,
  created_at TEXT DEFAULT (datetime('now')),
  updated_at TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS blacklist (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  domain TEXT,
  url TEXT,
  reason TEXT NOT NULL,
  created_at TEXT DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_blacklist_domain ON blacklist(domain);

CREATE TABLE IF NOT EXISTS learning (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  workflow_id TEXT,
  domain TEXT,
  placement_type TEXT,
  final_score REAL,
  approved INTEGER,
  published INTEGER,
  verified INTEGER,
  failure_reason TEXT,
  created_at TEXT DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_learning_domain ON learning(domain);

CREATE TABLE IF NOT EXISTS logs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  workflow_id TEXT NOT NULL,
  step TEXT NOT NULL,
  level TEXT NOT NULL DEFAULT 'info',
  message TEXT NOT NULL,
  detail_json TEXT,
  created_at TEXT DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_logs_workflow ON logs(workflow_id, created_at);

CREATE TABLE IF NOT EXISTS search_cache (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  cache_key TEXT NOT NULL UNIQUE,
  query TEXT NOT NULL,
  provider TEXT NOT NULL,
  results_json TEXT NOT NULL,
  created_at TEXT DEFAULT (datetime('now')),
  expires_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_search_cache_expires ON search_cache(expires_at);

CREATE TABLE IF NOT EXISTS parsed_pages (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  url TEXT NOT NULL,
  normalized_url TEXT NOT NULL,
  url_hash TEXT NOT NULL UNIQUE,
  title TEXT,
  content_hash TEXT,
  signals_json TEXT,
  parsed_at TEXT DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_parsed_pages_hash ON parsed_pages(url_hash);

CREATE TABLE IF NOT EXISTS page_cache (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  url_hash TEXT NOT NULL UNIQUE,
  url TEXT NOT NULL,
  html TEXT NOT NULL,
  fetched_at TEXT DEFAULT (datetime('now')),
  expires_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_page_cache_expires ON page_cache(expires_at);
