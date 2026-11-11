-- Apply only through an explicitly approved migration, never in a request handler.
CREATE TABLE approval_interactions (
  challenge_id TEXT PRIMARY KEY,
  approval_id TEXT NOT NULL,
  owner_binding TEXT NOT NULL,
  binding_sha256 TEXT NOT NULL,
  request_sha256 TEXT NOT NULL,
  expires_at INTEGER NOT NULL,
  record_json TEXT NOT NULL,
  status TEXT NOT NULL CHECK(status IN ('PENDING','DECIDED','CONSUMED')),
  subject_sha256 TEXT,
  session_sha256 TEXT,
  csrf_sha256 TEXT,
  decision TEXT CHECK(decision IS NULL OR decision IN ('accept','decline')),
  decided_at INTEGER,
  consumed_at INTEGER,
  receipt_sha256 TEXT
);
CREATE INDEX approval_owner_history ON approval_interactions(owner_binding, approval_id);
