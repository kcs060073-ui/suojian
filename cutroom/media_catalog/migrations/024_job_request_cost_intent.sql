-- 024: keep the requested cost intent immutable for provider idempotency.

ALTER TABLE job_payloads ADD COLUMN requested_cost_status TEXT NOT NULL
  DEFAULT 'unknown'
  CHECK(requested_cost_status IN (
    'unknown', 'estimated', 'approved', 'incurred', 'waived'
  ));

UPDATE job_payloads
SET requested_cost_status=CASE
  WHEN cost_status='incurred' THEN 'approved'
  ELSE cost_status
END;
