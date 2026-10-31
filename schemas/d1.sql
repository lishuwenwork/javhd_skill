CREATE TABLE IF NOT EXISTS video_uploads (
    id TEXT PRIMARY KEY NOT NULL,
    video_id TEXT NOT NULL UNIQUE,
    title_zh TEXT NOT NULL DEFAULT '',
    title_en TEXT NOT NULL DEFAULT '',
    detail_url TEXT NOT NULL DEFAULT '',
    cover_url TEXT NOT NULL DEFAULT '',
    preview_url TEXT NOT NULL DEFAULT '',
    telegram_status INTEGER NOT NULL DEFAULT 0 CHECK (telegram_status IN (0, 1)),
    quark_status INTEGER NOT NULL DEFAULT 0 CHECK (quark_status IN (0, 1)),
    telegram_message_ids TEXT NOT NULL DEFAULT '',
    quark_file_id TEXT NOT NULL DEFAULT ''
) STRICT;
