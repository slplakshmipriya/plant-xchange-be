-- 0035: sitter availability semantics flipped — selection now means AVAILABLE.
-- The sitter marks the dates they are free for plant sitting; absence of
-- rows means no marked availability. Replaces 0033's sitter_unavailable_dates
-- (staging-only data; the old opt-out marks have no meaningful inverse).
DROP TABLE IF EXISTS sitter_unavailable_dates;
CREATE TABLE IF NOT EXISTS sitter_available_dates (
    sitter_uid TEXT NOT NULL REFERENCES sitter_profiles(uid) ON DELETE CASCADE,
    day DATE NOT NULL,
    PRIMARY KEY (sitter_uid, day)
);
