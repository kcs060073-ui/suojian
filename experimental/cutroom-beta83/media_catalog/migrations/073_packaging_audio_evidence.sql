-- Derived objective measurements, not a second asset catalog.
CREATE TABLE IF NOT EXISTS packaging_audio_evidence (
 source_sha256 TEXT NOT NULL,
 method_version TEXT NOT NULL,
 evidence_json TEXT NOT NULL,
 analyzed_at TEXT NOT NULL,
 PRIMARY KEY (source_sha256, method_version)
);
