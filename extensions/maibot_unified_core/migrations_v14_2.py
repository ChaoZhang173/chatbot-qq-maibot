"""v14.2 additive, idempotent schema. Only CREATE ... IF NOT EXISTS; no existing table is altered or dropped.

Rollback = stop the new executors and restore the previous code/config; the tables keep their rows.
"""
SCHEMA_VERSION = '14.2.0'

STATEMENTS = '''
-- Short-lived persona fields keep the ORIGINAL evidence time; reads, cache rebuilds and restatements never renew them.
CREATE TABLE IF NOT EXISTS persona_temp(
    field TEXT PRIMARY KEY, value TEXT NOT NULL, scope TEXT NOT NULL, source_event_id TEXT NOT NULL,
    evidence_at REAL NOT NULL, expires_at REAL NOT NULL, set_at REAL NOT NULL);
-- Relationship preferences are bound to platform + bot account + verified actor + session.
CREATE TABLE IF NOT EXISTS relationship_prefs(
    pref_id TEXT PRIMARY KEY, platform TEXT NOT NULL, bot_account_id TEXT NOT NULL, actor_id TEXT NOT NULL,
    session_id TEXT NOT NULL, kind TEXT NOT NULL, text TEXT NOT NULL, quote TEXT NOT NULL,
    source_event_id TEXT NOT NULL UNIQUE, memory_status TEXT NOT NULL DEFAULT 'pending',
    created_at REAL NOT NULL, active INTEGER NOT NULL DEFAULT 1);
CREATE INDEX IF NOT EXISTS relationship_scope ON relationship_prefs(platform,bot_account_id,actor_id,session_id,active);
-- One row per source event: idempotent routing and audit of explicit feedback.
CREATE TABLE IF NOT EXISTS feedback_log(
    event_id TEXT PRIMARY KEY, route TEXT NOT NULL, status TEXT NOT NULL, detail TEXT NOT NULL DEFAULT '{}',
    persona_version INTEGER, actor_id TEXT NOT NULL DEFAULT '', created_at REAL NOT NULL);
CREATE INDEX IF NOT EXISTS feedback_route ON feedback_log(route,actor_id,created_at);
CREATE TABLE IF NOT EXISTS owner_global_pending(
    actor_id TEXT PRIMARY KEY, event_id TEXT NOT NULL, changes TEXT NOT NULL, quote TEXT NOT NULL, created_at REAL NOT NULL);
-- Daily learning: one row per (day, scope, batch) with lease, saved output and a durable commit list.
CREATE TABLE IF NOT EXISTS daily_batches(
    batch_key TEXT PRIMARY KEY, day TEXT NOT NULL, scope TEXT NOT NULL, kind TEXT NOT NULL,
    status TEXT NOT NULL, lease_until REAL NOT NULL DEFAULT 0, attempts INTEGER NOT NULL DEFAULT 0,
    next_at REAL NOT NULL DEFAULT 0, manifest TEXT NOT NULL DEFAULT '[]', output TEXT NOT NULL DEFAULT '',
    committed TEXT NOT NULL DEFAULT '[]', detail TEXT NOT NULL DEFAULT '', input_chars INTEGER NOT NULL DEFAULT 0,
    output_chars INTEGER NOT NULL DEFAULT 0, tokens_in INTEGER, tokens_out INTEGER,
    created_at REAL NOT NULL, updated_at REAL NOT NULL);
CREATE INDEX IF NOT EXISTS daily_batches_day ON daily_batches(day,status);
-- Local impression ledger (zero model tokens): one contact = one row, weight is the maximum action.
CREATE TABLE IF NOT EXISTS impressions(
    impression_key TEXT PRIMARY KEY, platform TEXT NOT NULL, opportunity_id TEXT NOT NULL,
    content_id TEXT NOT NULL, target_type TEXT NOT NULL, target_ref TEXT NOT NULL, topic TEXT NOT NULL,
    direction TEXT NOT NULL, mode TEXT NOT NULL, execution TEXT NOT NULL, action TEXT NOT NULL,
    weight INTEGER NOT NULL, scope TEXT NOT NULL, occurred_at REAL NOT NULL, evidence_ids TEXT NOT NULL,
    created_at REAL NOT NULL, updated_at REAL NOT NULL);
CREATE INDEX IF NOT EXISTS impressions_topic ON impressions(target_type,topic,occurred_at);
CREATE TABLE IF NOT EXISTS interest_candidates(
    candidate_key TEXT PRIMARY KEY, field TEXT NOT NULL DEFAULT '', label TEXT NOT NULL, status TEXT NOT NULL,
    fingerprint TEXT NOT NULL, verdict TEXT NOT NULL DEFAULT '', reviewed_at REAL NOT NULL DEFAULT 0,
    reason TEXT NOT NULL DEFAULT '', contact_keys TEXT NOT NULL DEFAULT '[]', updated_at REAL NOT NULL);
-- Curiosity exploration queue and its per-opportunity budget.
CREATE TABLE IF NOT EXISTS exploration_tasks(
    task_key TEXT PRIMARY KEY, platform TEXT NOT NULL, purpose TEXT NOT NULL, query TEXT NOT NULL,
    purpose_norm TEXT NOT NULL, status TEXT NOT NULL, origin_event_id TEXT NOT NULL DEFAULT '',
    origin_kind TEXT NOT NULL DEFAULT '', created_at REAL NOT NULL, started_at REAL NOT NULL DEFAULT 0,
    finished_at REAL NOT NULL DEFAULT 0, searches INTEGER NOT NULL DEFAULT 0, reads INTEGER NOT NULL DEFAULT 0,
    detail TEXT NOT NULL DEFAULT '{}');
CREATE INDEX IF NOT EXISTS exploration_day ON exploration_tasks(status,created_at);
'''


def apply(db):
    db.executescript(STATEMENTS)
    db.execute("INSERT INTO meta VALUES('schema_v14_2',?) ON CONFLICT(k) DO NOTHING", ('"%s"' % SCHEMA_VERSION,))
