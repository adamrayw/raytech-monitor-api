-- Raytech Monitor v0.6 Migration: Incident Consistency & Resolution Lifecycle
-- Non-destructive addition of resolution_reason column to incidents table

ALTER TABLE incidents ADD COLUMN IF NOT EXISTS resolution_reason VARCHAR(64);

-- Optional index for faster incident lifecycle auditing
CREATE INDEX IF NOT EXISTS idx_incidents_status_monitor ON incidents (monitor_id, status);
