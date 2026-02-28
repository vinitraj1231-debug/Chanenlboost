-- ╔══════════════════════════════════════════════════════╗
-- ║   ChannelBoost Bot - Database Init Script           ║
-- ║   Auto-executed by Docker on first run              ║
-- ╚══════════════════════════════════════════════════════╝

-- Create extensions
CREATE EXTENSION IF NOT EXISTS pg_trgm;

-- Grant permissions (Docker sets up the user/db, this ensures all rights)
GRANT ALL PRIVILEGES ON DATABASE channelboost_db TO channelboost;

-- All tables are auto-created by SQLAlchemy on startup (Base.metadata.create_all)
-- This file is just for any manual seeds or extensions needed

-- Optional: Create indexes for better query performance
-- (Run these AFTER the bot creates the tables on first startup)
-- CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_users_telegram_id ON users(telegram_id);
-- CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_channels_channel_id ON channels(channel_id);
-- CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_promotions_status ON promotions(status);
-- CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_promotion_posts_promotion_id ON promotion_posts(promotion_id);
