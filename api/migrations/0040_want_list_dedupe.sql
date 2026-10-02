-- 0040_want_list_dedupe: one want per (user, normalized variety).
--
-- The want-list previously allowed the same variety twice for one user
-- (case/whitespace variants included). Matching treats those as the same
-- item, so duplicates only double-notify. Drop exact duplicates first
-- (keep the earliest created entry, tie-break on id), then enforce with a
-- unique index on the normalized variety.
DELETE FROM want_list a
USING want_list b
WHERE a.user_uid = b.user_uid
  AND lower(btrim(a.variety)) = lower(btrim(b.variety))
  AND (b.created_at < a.created_at
       OR (b.created_at = a.created_at AND b.id < a.id));

CREATE UNIQUE INDEX IF NOT EXISTS want_list_user_variety_uidx
    ON want_list (user_uid, lower(btrim(variety)));
