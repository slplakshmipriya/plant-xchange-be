-- 0041_no_show_reports: one no-show strike per (claim, reporter, target).
--
-- Until now the no-show endpoint incremented an aggregate counter on every
-- POST, so one counterparty could report the same claim twice in a row and
-- suspend the other side for 30 days. Strikes now come from recorded
-- reports: each (claim, reporter, target) tuple counts once, so reaching
-- the 2-strike suspension threshold requires two distinct claims.
CREATE TABLE IF NOT EXISTS claim_no_show_reports (
    id UUID PRIMARY KEY,
    claim_id UUID NOT NULL REFERENCES claims(id) ON DELETE CASCADE,
    reporter_uid TEXT NOT NULL REFERENCES users(uid) ON DELETE CASCADE,
    target_uid TEXT NOT NULL REFERENCES users(uid) ON DELETE CASCADE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (claim_id, reporter_uid, target_uid)
);
CREATE INDEX IF NOT EXISTS claim_no_show_reports_target_idx
    ON claim_no_show_reports (target_uid);
