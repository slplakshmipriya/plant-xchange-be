-- 0045_slot_claim_visit: record the outcome of a pick-your-own visit on
-- the claim itself. The claimer confirms after their visit how much they
-- actually picked (POST /v1/trees/{id}/slots/{slot_id}/confirm-visit);
-- lbs_picked is what they report, visited_at is when the confirmation
-- landed (epoch ms, matching the dayMs/startMs/endMs convention of the
-- slots table). Both nullable: claims made before this migration (and
-- claims nobody has confirmed yet) carry NULLs, which the serializer
-- renders as visit_confirmed=false with no lbs_picked.
-- No credits move at confirm time (credits settled at claim time), so
-- this is pure annotation — no CHECK beyond the column types.
ALTER TABLE slot_claims
    ADD COLUMN IF NOT EXISTS lbs_picked DOUBLE PRECISION;
ALTER TABLE slot_claims
    ADD COLUMN IF NOT EXISTS visited_at BIGINT;
