# ╔══════════════════════════════════════════════════════╗
# ║         ChannelBoost Bot - Dockerfile               ║
# ╚══════════════════════════════════════════════════════╝

FROM python:3.11-slim

# Labels
LABEL maintainer="ChannelBoost Team"
LABEL description="ChannelBoost Cross Promotion Credit Exchange Bot"
LABEL version="1.0.0"

# Environment
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app \
    TZ=UTC

# System dependencies
RUN apt-get update && apt-get install -y \
    gcc \
    libpq-dev \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Working directory
WORKDIR /app

# Install Python dependencies first (layer caching)
COPY requirements.txt .
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir -r requirements.txt

# Copy source code
COPY channelboost_bot.py .
COPY .env* ./

# Create log directory
RUN mkdir -p /app/logs

# Health check
HEALTHCHECK --interval=30s --timeout=10s --start-period=10s --retries=3 \
    CMD curl -f http://localhost:${WEBAPP_PORT:-8080}/health || exit 1

# Non-root user for security
RUN useradd --no-create-home --shell /bin/false botuser && \
    chown -R botuser:botuser /app
USER botuser

# Run the bot
CMD ["python", "channelboost_bot.py"]
