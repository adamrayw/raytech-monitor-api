-- Use QRISGate live defaults for newly created payment transactions.
-- Existing provider and mode values are preserved as historical records.

ALTER TABLE payment_transactions
    ALTER COLUMN provider SET DEFAULT 'qrisgate';

ALTER TABLE payment_transactions
    ALTER COLUMN provider_mode SET DEFAULT 'live';
