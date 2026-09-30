-- 0034: services a sitter offers, from a fixed taxonomy validated in app
-- code (SITTER_SERVICES in api/app/sitter.py). Empty array = "on request".
ALTER TABLE sitter_profiles
    ADD COLUMN IF NOT EXISTS services TEXT[] NOT NULL DEFAULT '{}';
