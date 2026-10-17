-- RAYTECH MONITOR v0.5 — Public Status Pages Forward Migration
-- Safe, non-destructive migration script for PostgreSQL

-- 1. Create status_pages table
CREATE TABLE IF NOT EXISTS status_pages (
    id SERIAL PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    name VARCHAR(255) NOT NULL,
    slug VARCHAR(64) NOT NULL,
    description TEXT,
    accent_color VARCHAR(32) NOT NULL DEFAULT '#00f0ff',
    logo_url VARCHAR(1024),
    is_published BOOLEAN NOT NULL DEFAULT FALSE,
    show_incident_history BOOLEAN NOT NULL DEFAULT TRUE,
    created_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW(),
    CONSTRAINT uq_status_pages_slug UNIQUE (slug)
);

CREATE INDEX IF NOT EXISTS idx_status_pages_user_id ON status_pages(user_id);
CREATE INDEX IF NOT EXISTS idx_status_pages_slug_published ON status_pages(slug, is_published);

-- 2. Create status_page_monitors table
CREATE TABLE IF NOT EXISTS status_page_monitors (
    id SERIAL PRIMARY KEY,
    status_page_id INTEGER NOT NULL REFERENCES status_pages(id) ON DELETE CASCADE,
    monitor_id INTEGER NOT NULL REFERENCES monitors(id) ON DELETE CASCADE,
    display_name VARCHAR(255),
    display_order INTEGER NOT NULL DEFAULT 0,
    created_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW(),
    CONSTRAINT uq_status_page_monitors_page_monitor UNIQUE (status_page_id, monitor_id)
);

CREATE INDEX IF NOT EXISTS idx_status_page_monitors_order ON status_page_monitors(status_page_id, display_order);
CREATE INDEX IF NOT EXISTS idx_status_page_monitors_monitor_id ON status_page_monitors(monitor_id);
