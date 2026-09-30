-- 0032_sitter_rate: optional daily rate on sitter profiles.
-- The sitter chooses the unit: 'credits' (whole credits/day, matching the
-- integer credit ledger) or 'usd' (dollars/day, 2-decimal precision for the
-- Stripe sitting-intent flow). Both columns are nullable: NULL means the
-- sitter hasn't set a rate ("rate on request"). The pair CHECK keeps them
-- in sync — a rate is either fully set or fully absent.
ALTER TABLE sitter_profiles
    ADD COLUMN rate_amount NUMERIC(10, 2),
    ADD COLUMN rate_unit TEXT,
    ADD CONSTRAINT sitter_profiles_rate_unit_check
        CHECK (rate_unit IS NULL OR rate_unit IN ('credits', 'usd')),
    ADD CONSTRAINT sitter_profiles_rate_pair_check
        CHECK ((rate_amount IS NULL) = (rate_unit IS NULL));
