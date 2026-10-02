-- 0039_credit_cost_cap: widen the listing credit-cost cap from 3 to 100.
-- The inline CHECK from 0004_listings auto-names the constraint
-- listings_credit_cost_check. The DO block makes the drop/re-add idempotent:
-- with two migration runners racing (H9), the pg_constraint guard skips the
-- ADD when it already exists (run_migrations also serializes runners with
-- an advisory lock; this is belt-and-braces).
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
            ADD CONSTRAINT listings_credit_cost_check CHECK (credit_cost BETWEEN 1 AND 100);
    END IF;
END $$;
