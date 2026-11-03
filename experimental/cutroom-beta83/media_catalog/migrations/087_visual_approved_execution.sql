-- 087: bounded server-owned approval consumption; never seed a real grant.
CREATE TABLE visual_execution_approvals (
 approval_id TEXT PRIMARY KEY,
 approval_ref TEXT NOT NULL,
 evidence_sha256 TEXT NOT NULL UNIQUE,
 package_json TEXT NOT NULL,
 package_sha256 TEXT NOT NULL,
 scope_kind TEXT NOT NULL CHECK(scope_kind='original_remaining_business'),
 total_limit INTEGER NOT NULL CHECK(total_limit=5),
 prior_used INTEGER NOT NULL CHECK(prior_used=3),
 prior_use_evidence_sha256 TEXT NOT NULL,
 reserved_requests INTEGER NOT NULL DEFAULT 0 CHECK(reserved_requests BETWEEN 0 AND 2),
 status TEXT NOT NULL CHECK(status IN ('approved','reserved','cancelled')),
 expires_at REAL NOT NULL
);
CREATE TABLE visual_execution_operations (
 operation_id TEXT PRIMARY KEY,
 approval_id TEXT NOT NULL UNIQUE REFERENCES visual_execution_approvals(approval_id),
 job_id TEXT NOT NULL UNIQUE REFERENCES jobs(job_id),
 worker_id TEXT NOT NULL,
 claim_token_sha256 TEXT NOT NULL,
 package_sha256 TEXT NOT NULL,
 status TEXT NOT NULL CHECK(status IN ('claimed','running','succeeded','failed','unknown','cancelled','stale')),
 reserved_requests INTEGER NOT NULL CHECK(reserved_requests=2),
 error_code TEXT
);
CREATE TABLE visual_execution_attempts (
 operation_id TEXT NOT NULL REFERENCES visual_execution_operations(operation_id),
 request_index INTEGER NOT NULL CHECK(request_index IN (0,1)),
 request_sha256 TEXT NOT NULL,
 status TEXT NOT NULL CHECK(status IN ('dispatch_intent','succeeded','failed','unknown','cancelled','stale')),
 error_code TEXT,
 PRIMARY KEY(operation_id,request_index)
);

-- Approval writer is server-only. These guards preserve its reviewed identity;
-- they are not a permission check or evidence validation by themselves.
CREATE TRIGGER visual_approval_identity_immutable
BEFORE UPDATE ON visual_execution_approvals
WHEN NEW.approval_id IS NOT OLD.approval_id OR NEW.approval_ref IS NOT OLD.approval_ref OR NEW.evidence_sha256 IS NOT OLD.evidence_sha256 OR NEW.package_json IS NOT OLD.package_json OR NEW.package_sha256 IS NOT OLD.package_sha256 OR NEW.scope_kind IS NOT OLD.scope_kind OR NEW.total_limit IS NOT OLD.total_limit OR NEW.prior_used IS NOT OLD.prior_used OR NEW.prior_use_evidence_sha256 IS NOT OLD.prior_use_evidence_sha256 OR NEW.expires_at IS NOT OLD.expires_at
BEGIN SELECT RAISE(ABORT,'VISUAL_APPROVAL_IDENTITY_IMMUTABLE'); END;
CREATE TRIGGER visual_approval_transition_guard
BEFORE UPDATE ON visual_execution_approvals
WHEN NOT (
 (NEW.status=OLD.status AND NEW.reserved_requests=OLD.reserved_requests)
 OR (OLD.status='approved' AND NEW.status='reserved' AND OLD.reserved_requests=0 AND NEW.reserved_requests=2)
 OR (OLD.status IN ('approved','reserved') AND NEW.status='cancelled' AND NEW.reserved_requests=OLD.reserved_requests)
)
BEGIN SELECT RAISE(ABORT,'VISUAL_APPROVAL_TRANSITION_INVALID'); END;
CREATE TRIGGER visual_approval_no_delete
BEFORE DELETE ON visual_execution_approvals
BEGIN SELECT RAISE(ABORT,'VISUAL_APPROVAL_HISTORY_REQUIRED'); END;
CREATE TRIGGER visual_operation_identity_immutable
BEFORE UPDATE ON visual_execution_operations
WHEN NEW.operation_id IS NOT OLD.operation_id OR NEW.approval_id IS NOT OLD.approval_id
 OR NEW.job_id IS NOT OLD.job_id OR NEW.worker_id IS NOT OLD.worker_id
 OR NEW.claim_token_sha256 IS NOT OLD.claim_token_sha256 OR NEW.package_sha256 IS NOT OLD.package_sha256
 OR NEW.reserved_requests IS NOT OLD.reserved_requests
BEGIN SELECT RAISE(ABORT,'VISUAL_OPERATION_IDENTITY_IMMUTABLE'); END;
CREATE TRIGGER visual_operation_terminal_guard
BEFORE UPDATE OF status ON visual_execution_operations
WHEN OLD.status NOT IN ('claimed','running') AND NEW.status IS NOT OLD.status
BEGIN SELECT RAISE(ABORT,'VISUAL_OPERATION_CLOSED'); END;
CREATE TRIGGER visual_attempt_identity_immutable
BEFORE UPDATE ON visual_execution_attempts
WHEN NEW.operation_id IS NOT OLD.operation_id OR NEW.request_index IS NOT OLD.request_index
 OR NEW.request_sha256 IS NOT OLD.request_sha256
BEGIN SELECT RAISE(ABORT,'VISUAL_ATTEMPT_IDENTITY_IMMUTABLE'); END;
CREATE TRIGGER visual_attempt_terminal_guard
BEFORE UPDATE OF status ON visual_execution_attempts
WHEN OLD.status!='dispatch_intent' AND NEW.status IS NOT OLD.status
BEGIN SELECT RAISE(ABORT,'VISUAL_ATTEMPT_CLOSED'); END;
