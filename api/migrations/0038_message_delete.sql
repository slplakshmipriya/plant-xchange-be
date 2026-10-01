-- 0038_message_delete: soft-delete for chat messages.
-- Deleted messages become tombstones (body/photo hidden from participants,
-- pagination offsets stay stable). The ciphertext body is kept so support
-- moderation can still review reported-then-deleted messages (audit-logged).
ALTER TABLE messages ADD COLUMN IF NOT EXISTS deleted_at TIMESTAMPTZ NULL DEFAULT NULL;
