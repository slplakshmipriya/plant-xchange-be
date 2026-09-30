-- 0036_sitting_discrete_dates: discrete request dates + per-request services.
--
-- Replaces the inclusive start_date..end_date range on sitting_requests with
-- an explicit list of days (DATE[]). Sitter availability is opt-in since 0035
-- (a sitter marks the days they are free), so a booking is only valid on days
-- the sitter marked available — a contiguous range can't express that.
-- services TEXT[] records which of the sitter's advertised services the owner
-- is requesting for this booking.
ALTER TABLE sitting_requests
    ADD COLUMN dates DATE[] NOT NULL DEFAULT '{}',
    ADD COLUMN services TEXT[] NOT NULL DEFAULT '{}';

-- Backfill: expand each existing range into its inclusive day list, ordered.
-- (Existing rows all satisfy the old CHECK (end_date >= start_date), so the
-- series is never empty; LEFT JOIN LATERAL keeps the guard for safety.)
UPDATE sitting_requests AS sr
SET dates = COALESCE(agg.days, '{}')
FROM (
    SELECT s.id, array_agg(g.day::date ORDER BY g.day) AS days
    FROM sitting_requests s
    LEFT JOIN LATERAL
        generate_series(s.start_date, s.end_date, '1 day'::interval) AS g(day)
        ON true
    GROUP BY s.id
) AS agg
WHERE sr.id = agg.id;

-- Dropping the columns also drops the now-obsolete CHECK (end_date >= start_date).
ALTER TABLE sitting_requests DROP COLUMN start_date;
ALTER TABLE sitting_requests DROP COLUMN end_date;
