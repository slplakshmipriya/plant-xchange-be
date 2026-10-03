-- 0042_device_token_owner: one owner per FCM device token (L4).
--
-- device_tokens was keyed (user_uid, token), so the same device token
-- could be registered under several accounts and every one of them would
-- receive that device's pushes. A token names one physical device: keep
-- only the most recent registration per token, then enforce a UNIQUE
-- constraint on token alone; register_token now upserts by token and
-- reassigns ownership.
DELETE FROM device_tokens a
USING device_tokens b
WHERE a.token = b.token
  AND (b.last_seen_at > a.last_seen_at
       OR (b.last_seen_at = a.last_seen_at AND b.user_uid > a.user_uid));

ALTER TABLE device_tokens DROP CONSTRAINT IF EXISTS device_tokens_pkey;
ALTER TABLE device_tokens
    ADD CONSTRAINT device_tokens_pkey PRIMARY KEY (token);
