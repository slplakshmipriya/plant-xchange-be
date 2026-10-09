-- 0043_free_listings: allow credit_cost = 0 so a vertical running with
-- economy.credits_enabled=false (free/plain exchange) can store its
-- free listings. The 1-credit floor for credit-enabled marketplaces is
-- enforced in the API (ListingIn/ListingPatch validators), which is the
-- only write path; the CHECK keeps the 0..100 outer bound.
-- Same idempotent drop/re-add pattern as 0039.
DO $$
BEGIN
    ALTER TABLE listings DROP CONSTRAINT IF EXISTS listings_credit_cost_check;
    IF NOT EXISTS (
        SELECT 1
        FROM pg_constraint c
        JOIN pg_class t ON t.oid = c.conrelid
        WHERE c.conname = 'listings_credit_cost_check'
          AND t.relname = 'listings'
    ) THEN
        ALTER TABLE listings
            ADD CONSTRAINT listings_credit_cost_check CHECK (credit_cost BETWEEN 0 AND 100);
    END IF;
END $$;
