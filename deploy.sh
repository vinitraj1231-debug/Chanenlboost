#!/bin/bash
# ╔══════════════════════════════════════════════════════╗
# ║   ChannelBoost Bot - Quick Deploy Script            ║
# ║   Usage: chmod +x deploy.sh && ./deploy.sh          ║
# ╚══════════════════════════════════════════════════════╝

set -e

echo "╔══════════════════════════════════════════════╗"
echo "║     ChannelBoost Bot - Deployment Script    ║"
echo "╚══════════════════════════════════════════════╝"

# Check .env exists
if [ ! -f ".env" ]; then
    echo "⚠️  .env file not found!"
    echo "    Copying .env.example to .env..."
    cp .env.example .env
    echo "✏️  Please edit .env with your actual values and run again."
    exit 1
fi

# Check BOT_TOKEN is set
if grep -q "your_bot_token_here" .env; then
    echo "❌ BOT_TOKEN is not set in .env!"
    echo "   Edit .env and set your actual bot token from @BotFather"
    exit 1
fi

echo ""
echo "🐳 Starting with Docker Compose..."
echo ""

# Pull latest images
docker-compose pull postgres redis 2>/dev/null || true

# Build the bot image
echo "🔨 Building bot image..."
docker-compose build bot

# Start all services
echo "🚀 Starting all services..."
docker-compose up -d

# Wait for DB to be ready
echo "⏳ Waiting for PostgreSQL to be ready..."
sleep 10

# Check status
echo ""
echo "📊 Service Status:"
docker-compose ps

echo ""
echo "✅ Deployment complete!"
echo ""
echo "📋 Useful commands:"
echo "   View logs:    docker-compose logs -f bot"
echo "   Stop:         docker-compose down"
echo "   Restart bot:  docker-compose restart bot"
echo "   Update:       git pull && ./deploy.sh"
echo ""
echo "🎉 ChannelBoost Bot is now running!"
