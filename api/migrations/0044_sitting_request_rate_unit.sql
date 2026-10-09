-- 0044_sitting_request_rate_unit: snapshot the sitter's pricing
-- denomination onto each sitting request at creation time.
--
-- Why: the payment-intent USD gate used to read the sitter's CURRENT
-- profile rate_unit, so a sitter re-pricing (or a vertical flipping
-- usd_services_enabled) changed the denomination of bookings made
-- earlier. The snapshot freezes the denomination the booking was made
-- under; the intent path now gates on the snapshot.
--
-- Nullable with no backfill: rows created before this migration keep
-- NULL ("denomination unknown"). The intent path fails closed on a
-- NULL snapshot when the booking carries a positive amount, and treats
-- a NULL snapshot with no amount like the historic unit-less quote.
-- No CHECK: the only writers copy sitter_profiles.rate_unit verbatim
-- (itself CHECK-bound to 'credits'/'usd'), and a CHECK here would make
-- this migration fail on any legacy junk instead of staying safe.
ALTER TABLE sitting_requests
    ADD COLUMN IF NOT EXISTS rate_unit TEXT;
