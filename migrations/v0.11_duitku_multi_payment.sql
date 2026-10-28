-- Migration: v0.11_duitku_multi_payment.sql
-- Description: Adds va_number column to payment_transactions for Duitku Virtual Account integration.
-- Forward-only, non-destructive migration. Preserves all users, subscriptions, monitors, checks, and existing transactions.

ALTER TABLE payment_transactions ADD COLUMN IF NOT EXISTS va_number VARCHAR(64);
