-- 0037_stored_images: content-addressed image library for GCS uploads.
--
-- Every finalized GCS upload inserts one row keyed by perceptual hash
-- (dHash of the EXIF-stripped bytes, hex). A finalize whose phash already
-- exists reuses the stored object instead of uploading a second copy, so
-- one GCS object can back photos on many listings — hence the refcount:
-- completion/deletion/GC hooks decrement, and the GCS object + row are
-- removed only when refcount reaches 0.
-- size_bytes feeds the GCS_MAX_BYTES bucket-quota check in finalize.
-- zipcode is the uploader's home_zip at upload time (informational only).
CREATE TABLE IF NOT EXISTS stored_images (
    id UUID PRIMARY KEY,
    phash TEXT NOT NULL,
    gcs_key TEXT NOT NULL,
    size_bytes BIGINT NOT NULL CHECK (size_bytes > 0),
    zipcode TEXT,
    refcount INT NOT NULL DEFAULT 1 CHECK (refcount >= 0),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX IF NOT EXISTS stored_images_phash_uidx ON stored_images (phash);
CREATE INDEX IF NOT EXISTS stored_images_gcs_key_idx ON stored_images (gcs_key);
