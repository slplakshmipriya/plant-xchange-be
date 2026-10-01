# GCS image uploads — GardenSwap API

Listing photos are stored in Google Cloud Storage when
`STORAGE_BACKEND=gcs` (local dev keeps the `./var/uploads` stub).
Client flow is unchanged from the local backend: `POST /v1/uploads/sign`
→ `PUT` the bytes to `upload_url` → `POST /v1/uploads/finalize`.

## Env vars

| Env var | Default | Notes |
|---|---|---|
| `STORAGE_BACKEND` | `local` | Set to `gcs` for object storage. Refused with `local` in prod (`validate_storage_config`). |
| `GCS_BUCKET` | — (required) | Bucket for listing photos. |
| `GCS_MAX_BYTES` | `5368709120` (5 GiB) | Bucket quota cap = max free allowance of the Firebase Spark (unpaid) plan. Configurable; a finalize that would exceed it is rejected with **413 `bucket_quota_exceeded`** and its temp object is deleted. |

The `google-cloud-storage` package is imported lazily — dev/CI without
credentials keep working. In prod the runtime service account needs
`storage.objects.get/create/delete` on the bucket (delete is required:
dedupe drops duplicate temp bytes, and refcount-zero release deletes the
object). Credentials via ADC / `GOOGLE_APPLICATION_CREDENTIALS`.

## How it works (`api/app/images.py`, migration `0037`)

- **Sign** mints a V4 signed PUT URL (15-min TTL). The URL carries **no
  Content-Type constraint**, so existing clients need no change; the key is
  `u/{uid}/{uuid}.{ext}` and ownership is enforced at finalize.
- **Finalize** downloads the temp bytes → validates the image → strips EXIF
  GPS (same pipeline as the local backend) → hashes the clean bytes with
  **dHash** (64-bit perceptual hash, hex) → checks `stored_images`:
  - **Dedupe hit**: the temp object is deleted, the existing row's refcount
    is incremented, and the listing reuses the stored GCS object — no second
    upload. (`ON CONFLICT (phash) DO NOTHING` arbitrates concurrent
    finalizes of the same image.)
  - **Miss**: quota check (`SUM(size_bytes)` + incoming > `GCS_MAX_BYTES`
    → 413), then the clean bytes are stored and a row is inserted with the
    uploader's `home_zip` as `zipcode` (informational — the listing doesn't
    exist yet at upload time) and `refcount = 1`.
- **Release** is refcounted because dedupe shares one object across
  listings. Decrementing to 0 deletes the GCS object + row; otherwise the
  object survives. Hooked into:
  - swap completion (`POST /v1/exchange/confirm`, claimed → completed),
  - harvest depletion (`POST /v1/harvest-events`, live → completed),
  - account deletion (`DELETE /v1/users/me`, before the row cascade),
  - the retention sweep (`POST /v1/internal/sweep`, as a backstop for rows
    completed before the hooks existed).
  - No-op unless `STORAGE_BACKEND=gcs`.

## Ops notes

- Listing photo URLs are public
  `https://storage.googleapis.com/{bucket}/{key}` — the bucket needs
  public-read (or a CDN in front) for photos to load in clients.
- `stored_images.total_bytes` is the live quota meter; alert on it
  approaching `GCS_MAX_BYTES` rather than waiting for 413s.
- dHash is perceptual, not cryptographic: visually identical photos dedupe
  (intended); adversarial near-collisions are out of scope.
- `GET /v1/uploads/public/{key}` stays local-stub-only; on GCS, clients use
  the `public_url` from finalize directly.
