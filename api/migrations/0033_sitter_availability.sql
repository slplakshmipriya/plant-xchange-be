-- 0033: sitter availability — dates the sitter marks as unavailable.
-- Absence of rows means open. The client renders a 14-day window from this
-- set and the sitter edits it via PUT /v1/sitters/me/availability.
CREATE TABLE IF NOT EXISTS sitter_unavailable_dates (
    sitter_uid TEXT NOT NULL REFERENCES sitter_profiles(uid) ON DELETE CASCADE,
    day DATE NOT NULL,
    PRIMARY KEY (sitter_uid, day)
);
