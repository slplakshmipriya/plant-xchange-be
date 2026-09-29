-- 0031_reports_details_nullable: reports may omit details text.
-- The frontend sends no "details" key when the reporter leaves it empty.
-- The existing length CHECK passes on NULL in Postgres (NULL is not FALSE),
-- so only the NOT NULL constraint needs dropping.
ALTER TABLE reports ALTER COLUMN details DROP NOT NULL;
