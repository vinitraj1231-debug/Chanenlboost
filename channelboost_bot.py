#!/usr/bin/env python3
"""
╔══════════════════════════════════════════════════════════════════╗
║          ChannelBoost Exchange Bot - Production Ready           ║
║          Cross Promotion Credit Exchange System                 ║
║          Built with aiogram 3.x | PostgreSQL | Redis           ║
╚══════════════════════════════════════════════════════════════════╝
"""

# ─────────────────────────────────────────────────────────────────
#  IMPORTS
# ─────────────────────────────────────────────────────────────────
import asyncio
import logging
import os
import sys
from datetime import datetime, timedelta
from typing import Optional, List, Dict, Any, Callable, Awaitable
from contextlib import asynccontextmanager

from dotenv import load_dotenv
load_dotenv()

from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode, ChatMemberStatus
from aiogram.filters import Command, CommandStart, CommandObject
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.redis import RedisStorage
from aiogram.types import (
    Message, CallbackQuery, ChatMemberUpdated,
    InlineKeyboardMarkup, InlineKeyboardButton,
    ReplyKeyboardMarkup, KeyboardButton, ReplyKeyboardRemove,
    BotCommand, BotCommandScopeDefault
)
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError, TelegramAPIError
from aiogram.webhook.aiohttp_server import SimpleRequestHandler, setup_application
from aiogram.filters.chat_member_updated import ChatMemberUpdatedFilter, MEMBER, LEFT
from aiohttp import web

from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession, async_sessionmaker
from sqlalchemy.orm import DeclarativeBase, relationship
from sqlalchemy import (
    Column, Integer, BigInteger, String, DateTime, Float,
    Boolean, ForeignKey, Text, select, update, func, and_, or_, desc, asc
)

from apscheduler.schedulers.asyncio import AsyncIOScheduler
import redis.asyncio as aioredis

# ─────────────────────────────────────────────────────────────────
#  LOGGING
# ─────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler("channelboost.log", encoding="utf-8"),
    ],
)
logger = logging.getLogger("ChannelBoost")

# ─────────────────────────────────────────────────────────────────
#  CONFIG
# ─────────────────────────────────────────────────────────────────
class Config:
    BOT_TOKEN: str = os.getenv("BOT_TOKEN", "")
    DATABASE_URL: str = os.getenv("DATABASE_URL", "postgresql+asyncpg://user:pass@localhost/channelboost")
    REDIS_URL: str = os.getenv("REDIS_URL", "redis://localhost:6379/0")
    ADMIN_IDS: List[int] = [int(x) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip()]
    WEBHOOK_URL: str = os.getenv("WEBHOOK_URL", "")
    WEBHOOK_PATH: str = os.getenv("WEBHOOK_PATH", "/webhook")
    WEBAPP_HOST: str = os.getenv("WEBAPP_HOST", "0.0.0.0")
    WEBAPP_PORT: int = int(os.getenv("WEBAPP_PORT", "8080"))

    # Credit rules
    CREDITS_PER_100_VIEWS: int = 2
    CREDITS_PER_JOIN: int = 5
    CREDITS_PER_PUBLISH: int = 3
    INITIAL_CREDITS: int = 0
    REFERRAL_BONUS: int = 10

    # Scheduler intervals
    PROMOTION_ENGINE_INTERVAL: int = 10   # minutes
    VIEW_TRACKING_INTERVAL: int = 15      # minutes

cfg = Config()

# ─────────────────────────────────────────────────────────────────
#  DATABASE MODELS
# ─────────────────────────────────────────────────────────────────
class Base(DeclarativeBase):
    pass

class User(Base):
    __tablename__ = "users"
    id              = Column(Integer, primary_key=True, autoincrement=True)
    telegram_id     = Column(BigInteger, unique=True, nullable=False, index=True)
    username        = Column(String(64), nullable=True)
    full_name       = Column(String(128), nullable=True)
    credits         = Column(Integer, default=0, nullable=False)
    status          = Column(String(16), default="active")   # active | banned
    referral_code   = Column(String(16), unique=True, nullable=True)
    referred_by     = Column(BigInteger, nullable=True)
    joined_at       = Column(DateTime, default=datetime.utcnow)

    channels        = relationship("Channel", back_populates="owner", lazy="select")
    promotions      = relationship("Promotion", back_populates="creator", lazy="select")

class Channel(Base):
    __tablename__ = "channels"
    id              = Column(Integer, primary_key=True, autoincrement=True)
    channel_id      = Column(BigInteger, unique=True, nullable=False, index=True)
    channel_username= Column(String(64), nullable=True)
    title           = Column(String(128), nullable=True)
    owner_id        = Column(Integer, ForeignKey("users.id"), nullable=False)
    avg_views       = Column(Float, default=0.0)
    total_joins     = Column(Integer, default=0)
    score           = Column(Float, default=0.0)
    status          = Column(String(16), default="active")  # active | inactive | removed
    invite_link     = Column(String(256), nullable=True)
    connected_at    = Column(DateTime, default=datetime.utcnow)

    owner           = relationship("User", back_populates="channels")
    promo_posts     = relationship("PromotionPost", back_populates="channel", lazy="select")

class Promotion(Base):
    __tablename__ = "promotions"
    id                   = Column(Integer, primary_key=True, autoincrement=True)
    creator_id           = Column(Integer, ForeignKey("users.id"), nullable=False)
    source_chat_id       = Column(BigInteger, nullable=True)
    source_message_id    = Column(Integer, nullable=True)
    caption              = Column(Text, nullable=True)
    credit_budget        = Column(Integer, nullable=False)
    credits_spent        = Column(Integer, default=0)
    duration_hours       = Column(Integer, default=24)
    max_channels         = Column(Integer, default=10)
    channels_used        = Column(Integer, default=0)
    status               = Column(String(16), default="pending")  # pending|approved|rejected|running|completed|paused
    total_views          = Column(BigInteger, default=0)
    total_joins          = Column(Integer, default=0)
    admin_note           = Column(Text, nullable=True)
    created_at           = Column(DateTime, default=datetime.utcnow)
    approved_at          = Column(DateTime, nullable=True)
    expires_at           = Column(DateTime, nullable=True)

    creator              = relationship("User", back_populates="promotions")
    posts                = relationship("PromotionPost", back_populates="promotion", lazy="select")

class PromotionPost(Base):
    __tablename__ = "promotion_posts"
    id              = Column(Integer, primary_key=True, autoincrement=True)
    promotion_id    = Column(Integer, ForeignKey("promotions.id"), nullable=False)
    channel_id      = Column(Integer, ForeignKey("channels.id"), nullable=False)
    message_id      = Column(Integer, nullable=True)
    views_at_post   = Column(Integer, default=0)
    current_views   = Column(Integer, default=0)
    delta_views     = Column(Integer, default=0)
    joins           = Column(Integer, default=0)
    credits_earned  = Column(Integer, default=0)
    credits_tracked = Column(Integer, default=0)  # already credited
    posted_at       = Column(DateTime, default=datetime.utcnow)
    last_checked    = Column(DateTime, nullable=True)

    promotion       = relationship("Promotion", back_populates="posts")
    channel         = relationship("Channel", back_populates="promo_posts")

class JoinEvent(Base):
    __tablename__ = "join_events"
    id              = Column(Integer, primary_key=True, autoincrement=True)
    user_telegram_id= Column(BigInteger)
    channel_id      = Column(Integer, ForeignKey("channels.id"))
    promotion_id    = Column(Integer, ForeignKey("promotions.id"), nullable=True)
    credited        = Column(Boolean, default=False)
    occurred_at     = Column(DateTime, default=datetime.utcnow)

# ─────────────────────────────────────────────────────────────────
#  DATABASE SERVICE
# ─────────────────────────────────────────────────────────────────
engine = create_async_engine(cfg.DATABASE_URL, echo=False, pool_pre_ping=True)
AsyncSessionFactory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

@asynccontextmanager
async def db_session():
    async with AsyncSessionFactory() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise

async def init_db():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    logger.info("✅ Database tables created/verified")

# ─────────────────────────────────────────────────────────────────
#  REDIS SERVICE
# ─────────────────────────────────────────────────────────────────
redis_client: Optional[aioredis.Redis] = None

async def get_redis() -> aioredis.Redis:
    global redis_client
    if redis_client is None:
        redis_client = await aioredis.from_url(cfg.REDIS_URL, decode_responses=True)
    return redis_client

async def redis_set(key: str, value: str, ttl: int = 3600):
    r = await get_redis()
    await r.setex(key, ttl, value)

async def redis_get(key: str) -> Optional[str]:
    r = await get_redis()
    return await r.get(key)

async def redis_delete(key: str):
    r = await get_redis()
    await r.delete(key)

# ─────────────────────────────────────────────────────────────────
#  CREDIT SERVICE
# ─────────────────────────────────────────────────────────────────
async def get_user_credits(telegram_id: int) -> int:
    async with db_session() as session:
        result = await session.execute(select(User).where(User.telegram_id == telegram_id))
        user = result.scalar_one_or_none()
        return user.credits if user else 0

async def add_credits(telegram_id: int, amount: int, reason: str = "") -> int:
    async with db_session() as session:
        result = await session.execute(select(User).where(User.telegram_id == telegram_id))
        user = result.scalar_one_or_none()
        if user:
            user.credits += amount
            await session.flush()
            logger.info(f"💰 +{amount} credits → user {telegram_id} | reason: {reason}")
            return user.credits
        return 0

async def deduct_credits(telegram_id: int, amount: int) -> bool:
    async with db_session() as session:
        result = await session.execute(select(User).where(User.telegram_id == telegram_id))
        user = result.scalar_one_or_none()
        if user and user.credits >= amount:
            user.credits -= amount
            await session.flush()
            return True
        return False

# ─────────────────────────────────────────────────────────────────
#  USER SERVICE
# ─────────────────────────────────────────────────────────────────
import random
import string

def generate_referral_code(length: int = 8) -> str:
    return "".join(random.choices(string.ascii_uppercase + string.digits, k=length))

async def get_or_create_user(tg_user) -> User:
    async with db_session() as session:
        result = await session.execute(select(User).where(User.telegram_id == tg_user.id))
        user = result.scalar_one_or_none()
        if not user:
            code = generate_referral_code()
            user = User(
                telegram_id=tg_user.id,
                username=tg_user.username,
                full_name=tg_user.full_name,
                credits=cfg.INITIAL_CREDITS,
                referral_code=code,
            )
            session.add(user)
            await session.flush()
            await session.refresh(user)
            logger.info(f"👤 New user registered: {tg_user.id} (@{tg_user.username})")
        else:
            user.username = tg_user.username
            user.full_name = tg_user.full_name
        return user

async def get_user_by_telegram_id(telegram_id: int) -> Optional[User]:
    async with db_session() as session:
        result = await session.execute(select(User).where(User.telegram_id == telegram_id))
        return result.scalar_one_or_none()

async def ban_user(telegram_id: int) -> bool:
    async with db_session() as session:
        result = await session.execute(select(User).where(User.telegram_id == telegram_id))
        user = result.scalar_one_or_none()
        if user:
            user.status = "banned"
            return True
        return False

async def get_user_stats() -> Dict[str, int]:
    async with db_session() as session:
        total = (await session.execute(func.count(User.id))).scalar() or 0
        active_q = await session.execute(select(func.count(User.id)).where(User.status == "active"))
        active = active_q.scalar() or 0
        banned_q = await session.execute(select(func.count(User.id)).where(User.status == "banned"))
        banned = banned_q.scalar() or 0
        return {"total": total, "active": active, "banned": banned}

# ─────────────────────────────────────────────────────────────────
#  CHANNEL SERVICE
# ─────────────────────────────────────────────────────────────────
async def verify_bot_admin(bot: Bot, channel_username: str) -> Optional[Dict]:
    """Verify bot is admin in the channel with required permissions."""
    try:
        chat = await bot.get_chat(channel_username)
        bot_member = await bot.get_chat_member(chat.id, (await bot.get_me()).id)
        if bot_member.status not in ["administrator", "creator"]:
            return None
        can_post = getattr(bot_member, "can_post_messages", False)
        can_invite = getattr(bot_member, "can_invite_users", False)
        if not can_post or not can_invite:
            return None
        return {
            "chat_id": chat.id,
            "title": chat.title,
            "username": chat.username,
            "member_count": getattr(chat, "member_count", 0),
        }
    except (TelegramBadRequest, TelegramForbiddenError, TelegramAPIError):
        return None

async def connect_channel(owner_telegram_id: int, channel_info: Dict, bot: Bot) -> Optional[Channel]:
    async with db_session() as session:
        # Find owner user
        result = await session.execute(select(User).where(User.telegram_id == owner_telegram_id))
        owner = result.scalar_one_or_none()
        if not owner:
            return None

        # Check if channel already connected
        existing = await session.execute(
            select(Channel).where(Channel.channel_id == channel_info["chat_id"])
        )
        if existing.scalar_one_or_none():
            return None

        # Create invite link
        try:
            link = await bot.create_chat_invite_link(channel_info["chat_id"], creates_join_request=False)
            invite_link = link.invite_link
        except Exception:
            invite_link = f"https://t.me/{channel_info.get('username', '')}"

        channel = Channel(
            channel_id=channel_info["chat_id"],
            channel_username=channel_info.get("username"),
            title=channel_info.get("title", "Unknown"),
            owner_id=owner.id,
            invite_link=invite_link,
        )
        session.add(channel)
        await session.flush()
        await session.refresh(channel)
        return channel

async def get_user_channels(telegram_id: int) -> List[Channel]:
    async with db_session() as session:
        result = await session.execute(select(User).where(User.telegram_id == telegram_id))
        user = result.scalar_one_or_none()
        if not user:
            return []
        ch_result = await session.execute(
            select(Channel).where(and_(Channel.owner_id == user.id, Channel.status == "active"))
        )
        return ch_result.scalars().all()

async def get_best_channels_for_promotion(promo: Promotion, limit: int = 5) -> List[Channel]:
    """Get highest score active channels, excluding creator's own channels and already used ones."""
    async with db_session() as session:
        # Get already used channel IDs for this promo
        used_result = await session.execute(
            select(PromotionPost.channel_id).where(PromotionPost.promotion_id == promo.id)
        )
        used_ids = [r for r in used_result.scalars().all()]

        # Get owner channels to exclude
        result = await session.execute(select(User).where(User.id == promo.creator_id))
        creator = result.scalar_one_or_none()
        if not creator:
            return []

        own_ch_result = await session.execute(
            select(Channel.id).where(Channel.owner_id == creator.id)
        )
        own_ids = [r for r in own_ch_result.scalars().all()]

        exclude_ids = set(used_ids + own_ids)

        query = select(Channel).where(
            and_(
                Channel.status == "active",
                Channel.id.not_in(exclude_ids) if exclude_ids else True,
            )
        ).order_by(desc(Channel.score)).limit(limit)

        ch_result = await session.execute(query)
        return ch_result.scalars().all()

async def update_channel_score(channel_db_id: int):
    async with db_session() as session:
        result = await session.execute(select(Channel).where(Channel.id == channel_db_id))
        channel = result.scalar_one_or_none()
        if channel:
            channel.score = channel.avg_views + (channel.total_joins * 2)

# ─────────────────────────────────────────────────────────────────
#  PROMOTION SERVICE
# ─────────────────────────────────────────────────────────────────
async def create_promotion(
    creator_telegram_id: int,
    source_chat_id: int,
    source_message_id: int,
    credit_budget: int,
    duration_hours: int,
    max_channels: int,
) -> Optional[Promotion]:
    async with db_session() as session:
        result = await session.execute(select(User).where(User.telegram_id == creator_telegram_id))
        user = result.scalar_one_or_none()
        if not user:
            return None
        if user.credits < credit_budget:
            return None

        promo = Promotion(
            creator_id=user.id,
            source_chat_id=source_chat_id,
            source_message_id=source_message_id,
            credit_budget=credit_budget,
            duration_hours=duration_hours,
            max_channels=max_channels,
            status="pending",
        )
        session.add(promo)
        await session.flush()
        await session.refresh(promo)
        return promo

async def approve_promotion(promo_id: int, admin_note: str = "") -> Optional[Promotion]:
    async with db_session() as session:
        result = await session.execute(select(Promotion).where(Promotion.id == promo_id))
        promo = result.scalar_one_or_none()
        if not promo:
            return None
        promo.status = "approved"
        promo.admin_note = admin_note
        promo.approved_at = datetime.utcnow()
        promo.expires_at = datetime.utcnow() + timedelta(hours=promo.duration_hours)
        return promo

async def reject_promotion(promo_id: int, reason: str = "") -> Optional[Promotion]:
    async with db_session() as session:
        result = await session.execute(select(Promotion).where(Promotion.id == promo_id))
        promo = result.scalar_one_or_none()
        if not promo:
            return None
        promo.status = "rejected"
        promo.admin_note = reason
        return promo

async def get_pending_promotions() -> List[Promotion]:
    async with db_session() as session:
        result = await session.execute(
            select(Promotion).where(Promotion.status == "pending").order_by(asc(Promotion.created_at))
        )
        return result.scalars().all()

async def get_running_promotions() -> List[Promotion]:
    async with db_session() as session:
        result = await session.execute(
            select(Promotion).where(
                or_(Promotion.status == "approved", Promotion.status == "running")
            )
        )
        return result.scalars().all()

async def get_user_promotions(telegram_id: int) -> List[Promotion]:
    async with db_session() as session:
        result = await session.execute(select(User).where(User.telegram_id == telegram_id))
        user = result.scalar_one_or_none()
        if not user:
            return []
        p_result = await session.execute(
            select(Promotion).where(Promotion.creator_id == user.id).order_by(desc(Promotion.created_at))
        )
        return p_result.scalars().all()

# ─────────────────────────────────────────────────────────────────
#  LEADERBOARD SERVICE
# ─────────────────────────────────────────────────────────────────
async def get_leaderboard(limit: int = 10) -> List[User]:
    async with db_session() as session:
        result = await session.execute(
            select(User).where(User.status == "active").order_by(desc(User.credits)).limit(limit)
        )
        return result.scalars().all()

# ─────────────────────────────────────────────────────────────────
#  KEYBOARDS
# ─────────────────────────────────────────────────────────────────
def main_menu_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="💰 My Credits", callback_data="mycredits"),
            InlineKeyboardButton(text="📡 My Channels", callback_data="mychannels"),
        ],
        [
            InlineKeyboardButton(text="📢 My Promotions", callback_data="mypromotions"),
            InlineKeyboardButton(text="🏆 Leaderboard", callback_data="leaderboard"),
        ],
        [
            InlineKeyboardButton(text="➕ Connect Channel", callback_data="connect_channel"),
            InlineKeyboardButton(text="🚀 Create Promo", callback_data="create_promo"),
        ],
        [
            InlineKeyboardButton(text="ℹ️ Help", callback_data="help"),
            InlineKeyboardButton(text="👤 My Profile", callback_data="profile"),
        ],
    ])

def back_to_menu_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🏠 Main Menu", callback_data="main_menu")]
    ])

def promo_detail_keyboard(promo_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="⏸ Pause", callback_data=f"pause_promo:{promo_id}"),
            InlineKeyboardButton(text="🗑 Cancel", callback_data=f"cancel_promo:{promo_id}"),
        ],
        [InlineKeyboardButton(text="🔙 Back", callback_data="mypromotions")],
    ])

def admin_promo_keyboard(promo_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="✅ Approve", callback_data=f"admin_approve:{promo_id}"),
            InlineKeyboardButton(text="❌ Reject", callback_data=f"admin_reject:{promo_id}"),
        ],
        [InlineKeyboardButton(text="👁 View Pending", callback_data="admin_pending")],
    ])

def admin_main_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="📋 Pending Promos", callback_data="admin_pending"),
            InlineKeyboardButton(text="📊 Stats", callback_data="admin_stats"),
        ],
        [
            InlineKeyboardButton(text="👥 Users", callback_data="admin_users"),
            InlineKeyboardButton(text="📡 Channels", callback_data="admin_channels"),
        ],
        [InlineKeyboardButton(text="🏠 Main Menu", callback_data="main_menu")],
    ])

def confirm_keyboard(action: str, data: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="✅ Confirm", callback_data=f"confirm_{action}:{data}"),
            InlineKeyboardButton(text="❌ Cancel", callback_data="main_menu"),
        ]
    ])

def channel_list_keyboard(channels: List[Channel]) -> InlineKeyboardMarkup:
    rows = []
    for ch in channels:
        name = ch.title or ch.channel_username or str(ch.channel_id)
        rows.append([InlineKeyboardButton(
            text=f"📡 {name[:30]}",
            callback_data=f"channel_detail:{ch.id}"
        )])
    rows.append([InlineKeyboardButton(text="🏠 Main Menu", callback_data="main_menu")])
    return InlineKeyboardMarkup(inline_keyboard=rows)

def channel_detail_keyboard(channel_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="📊 Stats", callback_data=f"channel_stats:{channel_id}"),
            InlineKeyboardButton(text="🗑 Remove", callback_data=f"remove_channel:{channel_id}"),
        ],
        [InlineKeyboardButton(text="🔙 My Channels", callback_data="mychannels")],
    ])

# ─────────────────────────────────────────────────────────────────
#  FSM STATES
# ─────────────────────────────────────────────────────────────────
class ConnectChannelState(StatesGroup):
    waiting_username = State()

class CreatePromoState(StatesGroup):
    waiting_post     = State()
    waiting_budget   = State()
    waiting_duration = State()
    waiting_channels = State()

class AdminState(StatesGroup):
    waiting_reject_reason   = State()
    waiting_ban_id          = State()
    waiting_addcredits_id   = State()
    waiting_addcredits_amt  = State()

# ─────────────────────────────────────────────────────────────────
#  MIDDLEWARES
# ─────────────────────────────────────────────────────────────────
from aiogram import BaseMiddleware
from typing import Union

class UserMiddleware(BaseMiddleware):
    """Ensure user exists in DB for every update."""
    async def __call__(self, handler: Callable, event: Any, data: Dict) -> Any:
        if hasattr(event, "from_user") and event.from_user:
            tg_user = event.from_user
            user = await get_or_create_user(tg_user)
            data["db_user"] = user
            if user.status == "banned":
                if isinstance(event, Message):
                    await event.answer("🚫 You are banned from using this bot.")
                elif isinstance(event, CallbackQuery):
                    await event.answer("🚫 You are banned.", show_alert=True)
                return
        return await handler(event, data)

class AdminMiddleware(BaseMiddleware):
    """Inject is_admin flag."""
    async def __call__(self, handler: Callable, event: Any, data: Dict) -> Any:
        uid = None
        if hasattr(event, "from_user") and event.from_user:
            uid = event.from_user.id
        data["is_admin"] = uid in cfg.ADMIN_IDS
        return await handler(event, data)

# ─────────────────────────────────────────────────────────────────
#  HELPERS
# ─────────────────────────────────────────────────────────────────
def status_emoji(status: str) -> str:
    return {
        "pending":   "⏳",
        "approved":  "✅",
        "rejected":  "❌",
        "running":   "🚀",
        "completed": "🏁",
        "paused":    "⏸",
        "active":    "🟢",
        "inactive":  "🔴",
        "banned":    "🚫",
    }.get(status, "❓")

def format_promotion(p: Promotion, idx: int = 0) -> str:
    emoji = status_emoji(p.status)
    expires = p.expires_at.strftime("%Y-%m-%d %H:%M") if p.expires_at else "Not started"
    return (
        f"{emoji} <b>Promo #{p.id}</b>\n"
        f"├ Status: <code>{p.status.upper()}</code>\n"
        f"├ Budget: <code>{p.credit_budget}</code> credits\n"
        f"├ Spent: <code>{p.credits_spent}</code> credits\n"
        f"├ Channels: <code>{p.channels_used}/{p.max_channels}</code>\n"
        f"├ Views: <code>{p.total_views:,}</code>\n"
        f"├ Joins: <code>{p.total_joins}</code>\n"
        f"└ Expires: <code>{expires}</code>"
    )

def format_channel(ch: Channel) -> str:
    emoji = status_emoji(ch.status)
    name = ch.title or ch.channel_username or str(ch.channel_id)
    return (
        f"{emoji} <b>{name}</b>\n"
        f"├ Score: <code>{ch.score:.1f}</code>\n"
        f"├ Avg Views: <code>{ch.avg_views:.0f}</code>\n"
        f"└ Joins: <code>{ch.total_joins}</code>"
    )

# ─────────────────────────────────────────────────────────────────
#  ROUTER SETUP
# ─────────────────────────────────────────────────────────────────
router = Router()

# ─────────────────────────────────────────────────────────────────
#  /start HANDLER
# ─────────────────────────────────────────────────────────────────
@router.message(CommandStart())
async def cmd_start(message: Message, command: CommandObject, db_user: User):
    # Handle referral
    if command.args:
        ref_code = command.args.strip()
        async with db_session() as session:
            ref_result = await session.execute(
                select(User).where(
                    and_(User.referral_code == ref_code, User.telegram_id != message.from_user.id)
                )
            )
            referrer = ref_result.scalar_one_or_none()
            if referrer and not db_user.referred_by:
                await add_credits(referrer.telegram_id, cfg.REFERRAL_BONUS, "referral bonus")
                await add_credits(message.from_user.id, cfg.REFERRAL_BONUS // 2, "referral welcome")
                async with db_session() as s2:
                    u = (await s2.execute(select(User).where(User.telegram_id == message.from_user.id))).scalar_one_or_none()
                    if u:
                        u.referred_by = referrer.telegram_id

    welcome_text = (
        f"🚀 <b>Welcome to ChannelBoost Exchange Bot!</b>\n\n"
        f"Hello, <b>{message.from_user.full_name}</b>! 👋\n\n"
        f"<b>How it works:</b>\n"
        f"📡 Connect your Telegram channels\n"
        f"💰 Earn credits when promotions get views & joins\n"
        f"🚀 Spend credits to promote your own posts\n"
        f"✅ All promotions require admin approval\n\n"
        f"<b>Credit System:</b>\n"
        f"├ Every 100 views = +{cfg.CREDITS_PER_100_VIEWS} credits\n"
        f"├ Every join = +{cfg.CREDITS_PER_JOIN} credits\n"
        f"└ Publishing to a channel = -{cfg.CREDITS_PER_PUBLISH} credits\n\n"
        f"<b>Your Balance:</b> <code>{db_user.credits}</code> credits\n"
        f"<b>Referral Code:</b> <code>{db_user.referral_code}</code>\n\n"
        f"Use the menu below to get started!"
    )
    await message.answer(welcome_text, reply_markup=main_menu_keyboard(), parse_mode="HTML")

# ─────────────────────────────────────────────────────────────────
#  MAIN MENU CALLBACK
# ─────────────────────────────────────────────────────────────────
@router.callback_query(F.data == "main_menu")
async def cb_main_menu(call: CallbackQuery, db_user: User):
    text = (
        f"🏠 <b>Main Menu</b>\n\n"
        f"👤 <b>{call.from_user.full_name}</b>\n"
        f"💰 Credits: <code>{db_user.credits}</code>\n"
        f"🔗 Ref Code: <code>{db_user.referral_code}</code>"
    )
    await call.message.edit_text(text, reply_markup=main_menu_keyboard(), parse_mode="HTML")
    await call.answer()

# ─────────────────────────────────────────────────────────────────
#  HELP CALLBACK
# ─────────────────────────────────────────────────────────────────
@router.callback_query(F.data == "help")
async def cb_help(call: CallbackQuery):
    text = (
        "📖 <b>ChannelBoost Help</b>\n\n"
        "<b>Commands:</b>\n"
        "/start - Main menu\n"
        "/connect - Connect a channel\n"
        "/create_promo - Create a promotion\n"
        "/mycredits - Check balance\n"
        "/mychannels - Manage channels\n"
        "/mypromotions - View promotions\n"
        "/leaderboard - Top users\n\n"
        "<b>Credit Rules:</b>\n"
        f"📊 100 views → +{cfg.CREDITS_PER_100_VIEWS} credits\n"
        f"👤 1 join → +{cfg.CREDITS_PER_JOIN} credits\n"
        f"📤 1 channel publish → -{cfg.CREDITS_PER_PUBLISH} credits\n\n"
        "<b>Promotion Flow:</b>\n"
        "1️⃣ Create promo → status: pending\n"
        "2️⃣ Admin reviews & approves\n"
        "3️⃣ Bot distributes to channels\n"
        "4️⃣ You earn credits from engagement\n\n"
        "<b>Referral:</b>\n"
        f"Share your code and earn {cfg.REFERRAL_BONUS} credits per referral!"
    )
    await call.message.edit_text(text, reply_markup=back_to_menu_keyboard(), parse_mode="HTML")
    await call.answer()

# ─────────────────────────────────────────────────────────────────
#  PROFILE CALLBACK
# ─────────────────────────────────────────────────────────────────
@router.callback_query(F.data == "profile")
async def cb_profile(call: CallbackQuery, db_user: User):
    channels = await get_user_channels(call.from_user.id)
    promos = await get_user_promotions(call.from_user.id)
    text = (
        f"👤 <b>Your Profile</b>\n\n"
        f"🆔 ID: <code>{call.from_user.id}</code>\n"
        f"👤 Name: <b>{call.from_user.full_name}</b>\n"
        f"📛 Username: @{call.from_user.username or 'N/A'}\n"
        f"💰 Credits: <code>{db_user.credits}</code>\n"
        f"📡 Channels: <code>{len(channels)}</code>\n"
        f"📢 Promotions: <code>{len(promos)}</code>\n"
        f"🔗 Referral Code: <code>{db_user.referral_code}</code>\n"
        f"🔗 Referral Link: <code>https://t.me/{(await call.bot.get_me()).username}?start={db_user.referral_code}</code>\n"
        f"📅 Joined: <code>{db_user.joined_at.strftime('%Y-%m-%d')}</code>"
    )
    await call.message.edit_text(text, reply_markup=back_to_menu_keyboard(), parse_mode="HTML")
    await call.answer()

# ─────────────────────────────────────────────────────────────────
#  MY CREDITS
# ─────────────────────────────────────────────────────────────────
@router.message(Command("mycredits"))
@router.callback_query(F.data == "mycredits")
async def cmd_mycredits(event: Union[Message, CallbackQuery], db_user: User):
    text = (
        f"💰 <b>Your Credit Balance</b>\n\n"
        f"Current Balance: <code>{db_user.credits}</code> credits\n\n"
        f"<b>How to earn more:</b>\n"
        f"📡 Connect channels to receive promotions\n"
        f"👤 Invite users with your referral link\n"
        f"📊 Get views on promotional posts\n\n"
        f"<b>Spending:</b>\n"
        f"📤 Publishing costs <code>{cfg.CREDITS_PER_PUBLISH}</code> credits per channel"
    )
    kb = back_to_menu_keyboard()
    if isinstance(event, Message):
        await event.answer(text, reply_markup=kb, parse_mode="HTML")
    else:
        await event.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
        await event.answer()

# ─────────────────────────────────────────────────────────────────
#  CONNECT CHANNEL
# ─────────────────────────────────────────────────────────────────
@router.message(Command("connect"))
@router.callback_query(F.data == "connect_channel")
async def cmd_connect(event: Union[Message, CallbackQuery], state: FSMContext):
    text = (
        "📡 <b>Connect Your Channel</b>\n\n"
        "Please send your channel username or ID.\n\n"
        "<b>Requirements:</b>\n"
        "✅ Bot must be admin in the channel\n"
        "✅ Bot needs <code>Post Messages</code> permission\n"
        "✅ Bot needs <code>Invite Users</code> permission\n\n"
        "<b>Example:</b> <code>@mychannel</code> or <code>-1001234567890</code>"
    )
    await state.set_state(ConnectChannelState.waiting_username)
    if isinstance(event, Message):
        await event.answer(text, reply_markup=ReplyKeyboardRemove(), parse_mode="HTML")
    else:
        await event.message.edit_text(text, reply_markup=back_to_menu_keyboard(), parse_mode="HTML")
        await event.answer()

@router.message(ConnectChannelState.waiting_username)
async def process_channel_username(message: Message, state: FSMContext, bot: Bot):
    await state.clear()
    username = message.text.strip()
    if not username.startswith("@") and not username.startswith("-"):
        username = "@" + username

    status_msg = await message.answer("🔍 Verifying channel... Please wait.")

    channel_info = await verify_bot_admin(bot, username)
    if not channel_info:
        await status_msg.edit_text(
            "❌ <b>Verification Failed!</b>\n\n"
            "Possible reasons:\n"
            "• Bot is not an admin in this channel\n"
            "• Bot lacks 'Post Messages' or 'Invite Users' permissions\n"
            "• Channel doesn't exist or is private\n\n"
            "Please add the bot as admin and try again.",
            reply_markup=back_to_menu_keyboard(),
            parse_mode="HTML"
        )
        return

    channel = await connect_channel(message.from_user.id, channel_info, bot)
    if not channel:
        await status_msg.edit_text(
            "⚠️ <b>Already Connected!</b>\n\n"
            "This channel is already connected to the bot.",
            reply_markup=back_to_menu_keyboard(),
            parse_mode="HTML"
        )
        return

    await status_msg.edit_text(
        f"✅ <b>Channel Connected Successfully!</b>\n\n"
        f"📡 <b>{channel.title}</b>\n"
        f"🆔 ID: <code>{channel.channel_id}</code>\n"
        f"🔗 @{channel.channel_username or 'N/A'}\n\n"
        f"Your channel will now receive promotional posts.\n"
        f"You'll earn credits for every 100 views and every join!",
        reply_markup=back_to_menu_keyboard(),
        parse_mode="HTML"
    )

# ─────────────────────────────────────────────────────────────────
#  MY CHANNELS
# ─────────────────────────────────────────────────────────────────
@router.message(Command("mychannels"))
@router.callback_query(F.data == "mychannels")
async def cmd_mychannels(event: Union[Message, CallbackQuery]):
    uid = event.from_user.id
    channels = await get_user_channels(uid)

    if not channels:
        text = (
            "📡 <b>My Channels</b>\n\n"
            "You haven't connected any channels yet.\n\n"
            "Connect a channel to start earning credits!"
        )
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="➕ Connect Channel", callback_data="connect_channel")],
            [InlineKeyboardButton(text="🏠 Main Menu", callback_data="main_menu")],
        ])
    else:
        text = f"📡 <b>My Channels ({len(channels)})</b>\n\n"
        for ch in channels:
            text += format_channel(ch) + "\n\n"
        kb = channel_list_keyboard(channels)

    if isinstance(event, Message):
        await event.answer(text, reply_markup=kb, parse_mode="HTML")
    else:
        await event.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
        await event.answer()

@router.callback_query(F.data.startswith("channel_detail:"))
async def cb_channel_detail(call: CallbackQuery):
    ch_id = int(call.data.split(":")[1])
    async with db_session() as session:
        result = await session.execute(select(Channel).where(Channel.id == ch_id))
        ch = result.scalar_one_or_none()
    if not ch:
        await call.answer("Channel not found.", show_alert=True)
        return
    text = (
        f"📡 <b>Channel Details</b>\n\n"
        f"📌 Name: <b>{ch.title}</b>\n"
        f"🆔 ID: <code>{ch.channel_id}</code>\n"
        f"🔗 Username: @{ch.channel_username or 'N/A'}\n"
        f"📊 Score: <code>{ch.score:.2f}</code>\n"
        f"👁 Avg Views: <code>{ch.avg_views:.0f}</code>\n"
        f"👤 Total Joins: <code>{ch.total_joins}</code>\n"
        f"📅 Connected: <code>{ch.connected_at.strftime('%Y-%m-%d')}</code>\n"
        f"🟢 Status: <code>{ch.status.upper()}</code>"
    )
    await call.message.edit_text(text, reply_markup=channel_detail_keyboard(ch_id), parse_mode="HTML")
    await call.answer()

@router.callback_query(F.data.startswith("remove_channel:"))
async def cb_remove_channel(call: CallbackQuery):
    ch_id = int(call.data.split(":")[1])
    async with db_session() as session:
        result = await session.execute(select(Channel).where(Channel.id == ch_id))
        ch = result.scalar_one_or_none()
        if ch:
            ch.status = "removed"
    await call.message.edit_text(
        "🗑 <b>Channel Removed</b>\n\nThe channel has been disconnected from the bot.",
        reply_markup=back_to_menu_keyboard(),
        parse_mode="HTML"
    )
    await call.answer("✅ Channel removed")

# ─────────────────────────────────────────────────────────────────
#  CREATE PROMOTION
# ─────────────────────────────────────────────────────────────────
@router.message(Command("create_promo"))
@router.callback_query(F.data == "create_promo")
async def cmd_create_promo(event: Union[Message, CallbackQuery], state: FSMContext, db_user: User):
    text = (
        "🚀 <b>Create Promotion</b>\n\n"
        "Step 1/4: <b>Forward a post</b> from your channel that you want to promote.\n\n"
        "⚠️ The post must be from a public channel.\n"
        "📋 All promotions require admin approval before going live."
    )
    await state.set_state(CreatePromoState.waiting_post)
    if isinstance(event, Message):
        await event.answer(text, reply_markup=ReplyKeyboardRemove(), parse_mode="HTML")
    else:
        await event.message.edit_text(text, reply_markup=back_to_menu_keyboard(), parse_mode="HTML")
        await event.answer()

@router.message(CreatePromoState.waiting_post, F.forward_origin)
async def process_promo_post(message: Message, state: FSMContext, db_user: User):
    # Extract source info
    origin = message.forward_origin
    source_chat_id = None
    source_message_id = None

    if hasattr(origin, "chat"):
        source_chat_id = origin.chat.id
        source_message_id = origin.message_id
    elif hasattr(origin, "sender_chat"):
        source_chat_id = origin.sender_chat.id

    if not source_chat_id:
        await message.answer(
            "❌ Could not extract post source. Please forward a post from a public channel.",
            reply_markup=back_to_menu_keyboard()
        )
        await state.clear()
        return

    await state.update_data(
        source_chat_id=source_chat_id,
        source_message_id=source_message_id,
    )
    await state.set_state(CreatePromoState.waiting_budget)

    text = (
        f"✅ <b>Post received!</b>\n\n"
        f"Step 2/4: Set your <b>credit budget</b>.\n\n"
        f"💰 Your balance: <code>{db_user.credits}</code> credits\n"
        f"📤 Each channel publish costs <code>{cfg.CREDITS_PER_PUBLISH}</code> credits\n\n"
        f"Enter the number of credits to allocate to this promotion:\n"
        f"<i>(Minimum: {cfg.CREDITS_PER_PUBLISH}, Maximum: {db_user.credits})</i>"
    )
    await message.answer(text, parse_mode="HTML")

@router.message(CreatePromoState.waiting_budget)
async def process_promo_budget(message: Message, state: FSMContext, db_user: User):
    try:
        budget = int(message.text.strip())
        if budget < cfg.CREDITS_PER_PUBLISH:
            await message.answer(f"❌ Minimum budget is {cfg.CREDITS_PER_PUBLISH} credits.")
            return
        if budget > db_user.credits:
            await message.answer(f"❌ Insufficient credits. Balance: {db_user.credits}")
            return
    except ValueError:
        await message.answer("❌ Please enter a valid number.")
        return

    await state.update_data(credit_budget=budget)
    await state.set_state(CreatePromoState.waiting_duration)
    await message.answer(
        "Step 3/4: Set <b>duration</b> in hours.\n\n"
        "How long should this promotion run?\n"
        "<i>(e.g. 24 for 1 day, 72 for 3 days)</i>",
        parse_mode="HTML"
    )

@router.message(CreatePromoState.waiting_duration)
async def process_promo_duration(message: Message, state: FSMContext):
    try:
        hours = int(message.text.strip())
        if hours < 1 or hours > 168:
            await message.answer("❌ Duration must be between 1 and 168 hours (7 days).")
            return
    except ValueError:
        await message.answer("❌ Please enter a valid number.")
        return

    await state.update_data(duration_hours=hours)
    await state.set_state(CreatePromoState.waiting_channels)
    await message.answer(
        "Step 4/4: Set <b>max channels</b>.\n\n"
        "How many channels can this promotion be distributed to?\n"
        "<i>(e.g. 5, 10, 20)</i>",
        parse_mode="HTML"
    )

@router.message(CreatePromoState.waiting_channels)
async def process_promo_channels(message: Message, state: FSMContext, db_user: User):
    try:
        max_ch = int(message.text.strip())
        if max_ch < 1 or max_ch > 100:
            await message.answer("❌ Must be between 1 and 100.")
            return
    except ValueError:
        await message.answer("❌ Please enter a valid number.")
        return

    data = await state.get_data()
    await state.clear()

    budget = data["credit_budget"]
    hours = data["duration_hours"]
    source_chat_id = data["source_chat_id"]
    source_message_id = data.get("source_message_id")

    promo = await create_promotion(
        creator_telegram_id=message.from_user.id,
        source_chat_id=source_chat_id,
        source_message_id=source_message_id,
        credit_budget=budget,
        duration_hours=hours,
        max_channels=max_ch,
    )

    if not promo:
        await message.answer(
            "❌ Failed to create promotion. Please check your credit balance.",
            reply_markup=back_to_menu_keyboard()
        )
        return

    # Notify admins
    for admin_id in cfg.ADMIN_IDS:
        try:
            admin_text = (
                f"📬 <b>New Promotion Pending Review</b>\n\n"
                f"🆔 Promo ID: <code>{promo.id}</code>\n"
                f"👤 Creator ID: <code>{message.from_user.id}</code>\n"
                f"👤 Creator: @{message.from_user.username or 'N/A'}\n"
                f"💰 Budget: <code>{budget}</code> credits\n"
                f"⏱ Duration: <code>{hours}</code> hours\n"
                f"📡 Max Channels: <code>{max_ch}</code>\n\n"
                f"Use the buttons below to approve or reject."
            )
            await message.bot.send_message(
                admin_id, admin_text,
                reply_markup=admin_promo_keyboard(promo.id),
                parse_mode="HTML"
            )
            # Forward the actual post
            if source_chat_id and source_message_id:
                await message.bot.forward_message(admin_id, source_chat_id, source_message_id)
        except Exception as e:
            logger.error(f"Failed to notify admin {admin_id}: {e}")

    await message.answer(
        f"✅ <b>Promotion Created!</b>\n\n"
        f"🆔 Promo ID: <code>{promo.id}</code>\n"
        f"💰 Budget: <code>{budget}</code> credits\n"
        f"⏱ Duration: <code>{hours}</code> hours\n"
        f"📡 Max Channels: <code>{max_ch}</code>\n\n"
        f"⏳ Status: <b>Pending Admin Approval</b>\n\n"
        f"You'll be notified once it's approved and running.",
        reply_markup=back_to_menu_keyboard(),
        parse_mode="HTML"
    )

# ─────────────────────────────────────────────────────────────────
#  MY PROMOTIONS
# ─────────────────────────────────────────────────────────────────
@router.message(Command("mypromotions"))
@router.callback_query(F.data == "mypromotions")
async def cmd_mypromotions(event: Union[Message, CallbackQuery]):
    uid = event.from_user.id
    promos = await get_user_promotions(uid)

    if not promos:
        text = (
            "📢 <b>My Promotions</b>\n\n"
            "You haven't created any promotions yet.\n\n"
            "Create a promotion to start distributing your posts!"
        )
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🚀 Create Promo", callback_data="create_promo")],
            [InlineKeyboardButton(text="🏠 Main Menu", callback_data="main_menu")],
        ])
    else:
        text = f"📢 <b>My Promotions ({len(promos)})</b>\n\n"
        rows = []
        for p in promos[:10]:
            emoji = status_emoji(p.status)
            rows.append([InlineKeyboardButton(
                text=f"{emoji} Promo #{p.id} | {p.status.upper()}",
                callback_data=f"promo_detail:{p.id}"
            )])
        rows.append([InlineKeyboardButton(text="🏠 Main Menu", callback_data="main_menu")])
        kb = InlineKeyboardMarkup(inline_keyboard=rows)

    if isinstance(event, Message):
        await event.answer(text, reply_markup=kb, parse_mode="HTML")
    else:
        await event.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
        await event.answer()

@router.callback_query(F.data.startswith("promo_detail:"))
async def cb_promo_detail(call: CallbackQuery):
    promo_id = int(call.data.split(":")[1])
    async with db_session() as session:
        result = await session.execute(select(Promotion).where(Promotion.id == promo_id))
        p = result.scalar_one_or_none()
    if not p:
        await call.answer("Promotion not found.", show_alert=True)
        return
    text = format_promotion(p)
    if p.admin_note:
        text += f"\n\n📝 Admin Note: <i>{p.admin_note}</i>"

    kb = promo_detail_keyboard(promo_id) if p.status in ["running", "approved"] else back_to_menu_keyboard()
    await call.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
    await call.answer()

@router.callback_query(F.data.startswith("pause_promo:"))
async def cb_pause_promo(call: CallbackQuery):
    promo_id = int(call.data.split(":")[1])
    async with db_session() as session:
        result = await session.execute(select(Promotion).where(
            and_(Promotion.id == promo_id, Promotion.status.in_(["running", "approved"]))
        ))
        p = result.scalar_one_or_none()
        if p:
            p.status = "paused"
    await call.message.edit_text(
        f"⏸ <b>Promotion #{promo_id} Paused</b>\n\nYou can resume it from admin panel.",
        reply_markup=back_to_menu_keyboard(), parse_mode="HTML"
    )
    await call.answer("✅ Paused")

@router.callback_query(F.data.startswith("cancel_promo:"))
async def cb_cancel_promo(call: CallbackQuery):
    promo_id = int(call.data.split(":")[1])
    async with db_session() as session:
        result = await session.execute(select(Promotion).where(Promotion.id == promo_id))
        p = result.scalar_one_or_none()
        if p:
            p.status = "completed"
    await call.message.edit_text(
        f"🗑 <b>Promotion #{promo_id} Cancelled</b>",
        reply_markup=back_to_menu_keyboard(), parse_mode="HTML"
    )
    await call.answer("✅ Cancelled")

# ─────────────────────────────────────────────────────────────────
#  LEADERBOARD
# ─────────────────────────────────────────────────────────────────
@router.message(Command("leaderboard"))
@router.callback_query(F.data == "leaderboard")
async def cmd_leaderboard(event: Union[Message, CallbackQuery]):
    users = await get_leaderboard(10)
    text = "🏆 <b>Credit Leaderboard</b>\n\n"
    medals = ["🥇", "🥈", "🥉"] + ["🏅"] * 7
    for i, u in enumerate(users):
        name = u.full_name or u.username or str(u.telegram_id)
        text += f"{medals[i]} <b>{name[:20]}</b> — <code>{u.credits}</code> credits\n"

    if not users:
        text += "No users yet!"

    if isinstance(event, Message):
        await event.answer(text, reply_markup=back_to_menu_keyboard(), parse_mode="HTML")
    else:
        await event.message.edit_text(text, reply_markup=back_to_menu_keyboard(), parse_mode="HTML")
        await event.answer()

# ─────────────────────────────────────────────────────────────────
#  ADMIN PANEL
# ─────────────────────────────────────────────────────────────────
def admin_only(func):
    """Decorator to restrict handlers to admins only."""
    import functools
    @functools.wraps(func)
    async def wrapper(event: Any, *args, is_admin: bool = False, **kwargs):
        if not is_admin:
            if isinstance(event, Message):
                await event.answer("🚫 Admin access required.")
            elif isinstance(event, CallbackQuery):
                await event.answer("🚫 Admin access required.", show_alert=True)
            return
        return await func(event, *args, is_admin=is_admin, **kwargs)
    return wrapper

@router.message(Command("admin"))
@admin_only
async def cmd_admin(message: Message, is_admin: bool):
    pending = await get_pending_promotions()
    text = (
        f"🔧 <b>Admin Panel</b>\n\n"
        f"⏳ Pending Promotions: <code>{len(pending)}</code>\n\n"
        f"<b>Commands:</b>\n"
        f"/approve <code>promo_id</code>\n"
        f"/reject <code>promo_id reason</code>\n"
        f"/ban <code>user_id</code>\n"
        f"/addcredits <code>user_id amount</code>\n"
        f"/stats"
    )
    await message.answer(text, reply_markup=admin_main_keyboard(), parse_mode="HTML")

@router.callback_query(F.data == "admin_pending")
@admin_only
async def cb_admin_pending(call: CallbackQuery, is_admin: bool):
    promos = await get_pending_promotions()
    if not promos:
        await call.message.edit_text(
            "✅ <b>No pending promotions!</b>\n\nAll caught up.",
            reply_markup=admin_main_keyboard(), parse_mode="HTML"
        )
        await call.answer()
        return

    text = f"⏳ <b>Pending Promotions ({len(promos)})</b>\n\n"
    rows = []
    for p in promos:
        text += (
            f"🆔 <b>#{p.id}</b> | Creator: <code>{p.creator_id}</code> | "
            f"Budget: <code>{p.credit_budget}</code>cr | "
            f"Duration: <code>{p.duration_hours}h</code>\n"
        )
        rows.append([
            InlineKeyboardButton(text=f"✅ #{p.id}", callback_data=f"admin_approve:{p.id}"),
            InlineKeyboardButton(text=f"❌ #{p.id}", callback_data=f"admin_reject:{p.id}"),
        ])
    rows.append([InlineKeyboardButton(text="🔙 Admin Menu", callback_data="admin_menu")])
    await call.message.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=rows), parse_mode="HTML")
    await call.answer()

@router.callback_query(F.data.startswith("admin_approve:"))
@admin_only
async def cb_admin_approve(call: CallbackQuery, bot: Bot, is_admin: bool):
    promo_id = int(call.data.split(":")[1])
    promo = await approve_promotion(promo_id)
    if not promo:
        await call.answer("❌ Promotion not found.", show_alert=True)
        return

    await call.message.edit_text(
        f"✅ <b>Promotion #{promo_id} Approved!</b>\n\n"
        f"It will start distributing in the next scheduler cycle (every {cfg.PROMOTION_ENGINE_INTERVAL} min).",
        reply_markup=admin_main_keyboard(), parse_mode="HTML"
    )
    await call.answer("✅ Approved!")

    # Notify creator
    async with db_session() as session:
        result = await session.execute(select(User).where(User.id == promo.creator_id))
        creator = result.scalar_one_or_none()
    if creator:
        try:
            await bot.send_message(
                creator.telegram_id,
                f"🎉 <b>Promotion Approved!</b>\n\n"
                f"Your promotion <b>#{promo_id}</b> has been approved by an admin.\n"
                f"It will start distributing to channels shortly!\n\n"
                f"⏱ Duration: <code>{promo.duration_hours}</code> hours",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text="📢 View Promo", callback_data=f"promo_detail:{promo_id}")]
                ]),
                parse_mode="HTML"
            )
        except Exception:
            pass

@router.callback_query(F.data.startswith("admin_reject:"))
@admin_only
async def cb_admin_reject(call: CallbackQuery, state: FSMContext, is_admin: bool):
    promo_id = int(call.data.split(":")[1])
    await state.set_state(AdminState.waiting_reject_reason)
    await state.update_data(reject_promo_id=promo_id, admin_msg_id=call.message.message_id)
    await call.message.edit_text(
        f"❌ <b>Reject Promotion #{promo_id}</b>\n\n"
        f"Please enter a reason for rejection:",
        parse_mode="HTML"
    )
    await call.answer()

@router.message(AdminState.waiting_reject_reason)
@admin_only
async def process_reject_reason(message: Message, state: FSMContext, bot: Bot, is_admin: bool):
    data = await state.get_data()
    promo_id = data["reject_promo_id"]
    reason = message.text.strip()
    await state.clear()

    promo = await reject_promotion(promo_id, reason)
    if not promo:
        await message.answer("❌ Promotion not found.")
        return

    await message.answer(
        f"✅ <b>Promotion #{promo_id} Rejected</b>\n\nReason: {reason}",
        reply_markup=admin_main_keyboard(), parse_mode="HTML"
    )

    # Notify creator
    async with db_session() as session:
        result = await session.execute(select(User).where(User.id == promo.creator_id))
        creator = result.scalar_one_or_none()
    if creator:
        try:
            await bot.send_message(
                creator.telegram_id,
                f"❌ <b>Promotion Rejected</b>\n\n"
                f"Promotion <b>#{promo_id}</b> was rejected by admin.\n\n"
                f"📝 Reason: <i>{reason}</i>",
                parse_mode="HTML"
            )
        except Exception:
            pass

@router.message(Command("approve"))
@admin_only
async def cmd_approve(message: Message, command: CommandObject, bot: Bot, is_admin: bool):
    if not command.args:
        await message.answer("Usage: /approve <promo_id>")
        return
    try:
        promo_id = int(command.args.strip())
    except ValueError:
        await message.answer("Invalid promo ID.")
        return

    promo = await approve_promotion(promo_id)
    if not promo:
        await message.answer(f"❌ Promotion #{promo_id} not found or already processed.")
        return

    await message.answer(
        f"✅ <b>Promotion #{promo_id} Approved!</b>",
        reply_markup=admin_main_keyboard(), parse_mode="HTML"
    )

    async with db_session() as session:
        result = await session.execute(select(User).where(User.id == promo.creator_id))
        creator = result.scalar_one_or_none()
    if creator:
        try:
            await bot.send_message(
                creator.telegram_id,
                f"🎉 Promotion #{promo_id} has been approved and will start distributing soon!",
                parse_mode="HTML"
            )
        except Exception:
            pass

@router.message(Command("reject"))
@admin_only
async def cmd_reject(message: Message, command: CommandObject, bot: Bot, is_admin: bool):
    if not command.args:
        await message.answer("Usage: /reject <promo_id> [reason]")
        return
    parts = command.args.strip().split(maxsplit=1)
    try:
        promo_id = int(parts[0])
        reason = parts[1] if len(parts) > 1 else "No reason provided"
    except (ValueError, IndexError):
        await message.answer("Invalid format. Usage: /reject <promo_id> [reason]")
        return

    promo = await reject_promotion(promo_id, reason)
    if not promo:
        await message.answer(f"❌ Promotion #{promo_id} not found.")
        return
    await message.answer(f"✅ Promotion #{promo_id} rejected. Reason: {reason}")

@router.message(Command("ban"))
@admin_only
async def cmd_ban(message: Message, command: CommandObject, is_admin: bool):
    if not command.args:
        await message.answer("Usage: /ban <user_id>")
        return
    try:
        user_id = int(command.args.strip())
    except ValueError:
        await message.answer("Invalid user ID.")
        return

    success = await ban_user(user_id)
    if success:
        await message.answer(f"🚫 User {user_id} has been banned.")
    else:
        await message.answer(f"❌ User {user_id} not found.")

@router.message(Command("addcredits"))
@admin_only
async def cmd_addcredits(message: Message, command: CommandObject, is_admin: bool):
    if not command.args:
        await message.answer("Usage: /addcredits <user_id> <amount>")
        return
    parts = command.args.strip().split()
    if len(parts) < 2:
        await message.answer("Usage: /addcredits <user_id> <amount>")
        return
    try:
        user_id = int(parts[0])
        amount = int(parts[1])
    except ValueError:
        await message.answer("Invalid format.")
        return

    new_balance = await add_credits(user_id, amount, "admin grant")
    await message.answer(
        f"✅ Added <code>{amount}</code> credits to user <code>{user_id}</code>.\n"
        f"New balance: <code>{new_balance}</code>",
        parse_mode="HTML"
    )

@router.message(Command("stats"))
@router.callback_query(F.data == "admin_stats")
@admin_only
async def cmd_stats(event: Union[Message, CallbackQuery], is_admin: bool):
    user_stats = await get_user_stats()

    async with db_session() as session:
        ch_count = (await session.execute(select(func.count(Channel.id)).where(Channel.status == "active"))).scalar() or 0
        promo_pending = (await session.execute(select(func.count(Promotion.id)).where(Promotion.status == "pending"))).scalar() or 0
        promo_running = (await session.execute(select(func.count(Promotion.id)).where(Promotion.status.in_(["running", "approved"])))).scalar() or 0
        total_credits = (await session.execute(select(func.sum(User.credits)))).scalar() or 0

    text = (
        f"📊 <b>Bot Statistics</b>\n\n"
        f"👥 <b>Users:</b>\n"
        f"├ Total: <code>{user_stats['total']}</code>\n"
        f"├ Active: <code>{user_stats['active']}</code>\n"
        f"└ Banned: <code>{user_stats['banned']}</code>\n\n"
        f"📡 <b>Channels:</b>\n"
        f"└ Active: <code>{ch_count}</code>\n\n"
        f"📢 <b>Promotions:</b>\n"
        f"├ Pending: <code>{promo_pending}</code>\n"
        f"└ Running: <code>{promo_running}</code>\n\n"
        f"💰 <b>Total Credits in System:</b> <code>{total_credits:,}</code>"
    )

    kb = admin_main_keyboard()
    if isinstance(event, Message):
        await event.answer(text, reply_markup=kb, parse_mode="HTML")
    else:
        await event.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
        await event.answer()

@router.callback_query(F.data == "admin_menu")
@admin_only
async def cb_admin_menu(call: CallbackQuery, is_admin: bool):
    await call.message.edit_text(
        "🔧 <b>Admin Panel</b>",
        reply_markup=admin_main_keyboard(), parse_mode="HTML"
    )
    await call.answer()

# ─────────────────────────────────────────────────────────────────
#  JOIN TRACKING (ChatMemberUpdated)
# ─────────────────────────────────────────────────────────────────
@router.chat_member(ChatMemberUpdatedFilter(MEMBER))
async def on_user_joined(event: ChatMemberUpdated, bot: Bot):
    """Award credits to channel owner when users join."""
    try:
        joined_user_id = event.new_chat_member.user.id
        chat_id = event.chat.id

        async with db_session() as session:
            # Find the channel in our DB
            result = await session.execute(select(Channel).where(Channel.channel_id == chat_id))
            channel = result.scalar_one_or_none()
            if not channel or channel.status != "active":
                return

            # Update join count and score
            channel.total_joins += 1
            channel.score = channel.avg_views + (channel.total_joins * 2)

            # Find channel owner
            owner_result = await session.execute(select(User).where(User.id == channel.owner_id))
            owner = owner_result.scalar_one_or_none()
            if not owner:
                return

            # Log join event
            join_event = JoinEvent(
                user_telegram_id=joined_user_id,
                channel_id=channel.id,
                credited=True,
            )
            session.add(join_event)

        # Credit the channel owner
        new_balance = await add_credits(owner.telegram_id, cfg.CREDITS_PER_JOIN, f"join in channel {chat_id}")

        logger.info(f"👤 Join credit: +{cfg.CREDITS_PER_JOIN} to user {owner.telegram_id} for channel {chat_id}")

    except Exception as e:
        logger.error(f"Error in join tracking: {e}")

# ─────────────────────────────────────────────────────────────────
#  SCHEDULER - PROMOTION ENGINE
# ─────────────────────────────────────────────────────────────────
async def promotion_engine(bot: Bot):
    """Main promotion engine - runs every 10 minutes."""
    logger.info("⚙️ Promotion Engine: Starting cycle...")
    try:
        promos = await get_running_promotions()
        for promo in promos:
            await process_promotion(bot, promo)
    except Exception as e:
        logger.error(f"Promotion engine error: {e}")

async def process_promotion(bot: Bot, promo: Promotion):
    """Process a single approved/running promotion."""
    try:
        async with db_session() as session:
            result = await session.execute(select(Promotion).where(Promotion.id == promo.id))
            promo_fresh = result.scalar_one_or_none()
            if not promo_fresh:
                return

            # Check expiry
            if promo_fresh.expires_at and datetime.utcnow() > promo_fresh.expires_at:
                promo_fresh.status = "completed"
                logger.info(f"🏁 Promotion #{promo.id} expired and marked completed")
                return

            # Check budget remaining
            remaining_budget = promo_fresh.credit_budget - promo_fresh.credits_spent
            if remaining_budget < cfg.CREDITS_PER_PUBLISH:
                promo_fresh.status = "completed"
                logger.info(f"💰 Promotion #{promo.id} ran out of budget")
                return

            # Check max channels
            if promo_fresh.channels_used >= promo_fresh.max_channels:
                promo_fresh.status = "completed"
                logger.info(f"📡 Promotion #{promo.id} reached max channels")
                return

            # Check creator credits
            user_result = await session.execute(select(User).where(User.id == promo_fresh.creator_id))
            creator = user_result.scalar_one_or_none()
            if not creator or creator.credits < cfg.CREDITS_PER_PUBLISH:
                promo_fresh.status = "paused"
                logger.info(f"💸 Promotion #{promo.id} paused - insufficient credits")
                return

            # Mark as running if approved
            if promo_fresh.status == "approved":
                promo_fresh.status = "running"

        # Get best channel for this promotion
        channels = await get_best_channels_for_promotion(promo, limit=1)
        if not channels:
            logger.info(f"📡 No eligible channels for promotion #{promo.id}")
            return

        channel = channels[0]

        # Try to post the promotion
        try:
            if promo.source_chat_id and promo.source_message_id:
                sent = await bot.forward_message(
                    chat_id=channel.channel_id,
                    from_chat_id=promo.source_chat_id,
                    message_id=promo.source_message_id,
                )
                message_id = sent.message_id
                views = getattr(sent, "views", 0) or 0
            else:
                logger.warning(f"Promotion #{promo.id} has no source message to forward")
                return
        except (TelegramForbiddenError, TelegramBadRequest) as e:
            logger.error(f"Failed to post promo #{promo.id} to channel {channel.channel_id}: {e}")
            # Mark channel as inactive if bot was removed
            async with db_session() as session:
                result = await session.execute(select(Channel).where(Channel.id == channel.id))
                ch = result.scalar_one_or_none()
                if ch:
                    ch.status = "inactive"
            return

        # Record the post and deduct credits
        async with db_session() as session:
            post = PromotionPost(
                promotion_id=promo.id,
                channel_id=channel.id,
                message_id=message_id,
                views_at_post=views,
                current_views=views,
                posted_at=datetime.utcnow(),
            )
            session.add(post)

            # Update promotion stats
            promo_result = await session.execute(select(Promotion).where(Promotion.id == promo.id))
            promo_db = promo_result.scalar_one_or_none()
            if promo_db:
                promo_db.credits_spent += cfg.CREDITS_PER_PUBLISH
                promo_db.channels_used += 1

        # Deduct credits from creator
        async with db_session() as session:
            result = await session.execute(select(User).where(User.id == promo.creator_id))
            creator = result.scalar_one_or_none()
            if creator:
                creator.credits -= cfg.CREDITS_PER_PUBLISH

        logger.info(f"📤 Promo #{promo.id} → channel {channel.channel_id} | message {message_id}")

    except Exception as e:
        logger.error(f"Error processing promotion #{promo.id}: {e}")

# ─────────────────────────────────────────────────────────────────
#  SCHEDULER - VIEW TRACKING
# ─────────────────────────────────────────────────────────────────
async def view_tracking_engine(bot: Bot):
    """Track views on posted promotions and award credits - runs every 15 minutes."""
    logger.info("👁 View Tracking: Starting cycle...")
    try:
        async with db_session() as session:
            # Get all promotion posts that haven't expired or completed
            result = await session.execute(
                select(PromotionPost).join(Promotion).where(
                    Promotion.status.in_(["running", "approved"])
                ).order_by(asc(PromotionPost.last_checked))
                .limit(100)
            )
            posts = result.scalars().all()

        for post in posts:
            await track_post_views(bot, post)

    except Exception as e:
        logger.error(f"View tracking error: {e}")

async def track_post_views(bot: Bot, post: PromotionPost):
    """Track views for a single promotional post."""
    try:
        async with db_session() as session:
            # Get channel info
            ch_result = await session.execute(select(Channel).where(Channel.id == post.channel_id))
            channel = ch_result.scalar_one_or_none()
            if not channel:
                return

            promo_result = await session.execute(select(Promotion).where(Promotion.id == post.promotion_id))
            promo = promo_result.scalar_one_or_none()
            if not promo:
                return

        # Fetch message views from Telegram
        try:
            messages = await bot.get_messages(channel.channel_id, message_ids=[post.message_id])
            if not messages or not messages[0]:
                return
            current_views = getattr(messages[0], "views", 0) or 0
        except Exception:
            # Try alternative: get_message (non-batch)
            try:
                msg = await bot.forward_message(channel.channel_id, channel.channel_id, post.message_id)
                current_views = getattr(msg, "views", 0) or 0
            except Exception:
                return

        async with db_session() as session:
            post_result = await session.execute(select(PromotionPost).where(PromotionPost.id == post.id))
            post_db = post_result.scalar_one_or_none()
            if not post_db:
                return

            prev_views = post_db.current_views
            new_views = max(current_views, prev_views)
            delta = new_views - prev_views
            already_credited_views = post_db.credits_tracked

            post_db.current_views = new_views
            post_db.delta_views += delta
            post_db.last_checked = datetime.utcnow()

            # Calculate new credits to award (per 100 views)
            total_trackable_views = post_db.delta_views
            new_credit_blocks = (total_trackable_views // 100) - (already_credited_views // 100)

            if new_credit_blocks > 0:
                credits_to_award = new_credit_blocks * cfg.CREDITS_PER_100_VIEWS
                post_db.credits_earned += credits_to_award
                post_db.credits_tracked = total_trackable_views

                # Update channel avg views
                ch_result = await session.execute(select(Channel).where(Channel.id == post.channel_id))
                ch = ch_result.scalar_one_or_none()
                if ch:
                    ch.avg_views = (ch.avg_views * 0.8) + (new_views * 0.2)  # Rolling avg
                    ch.score = ch.avg_views + (ch.total_joins * 2)

                owner_result = await session.execute(
                    select(User).join(Channel, User.id == Channel.owner_id).where(Channel.id == post.channel_id)
                )
                owner = owner_result.scalar_one_or_none()
                if owner:
                    owner.credits += credits_to_award
                    logger.info(f"👁 View credit: +{credits_to_award} to user {owner.telegram_id} for {delta} new views")

    except Exception as e:
        logger.error(f"Error tracking views for post {post.id}: {e}")

# ─────────────────────────────────────────────────────────────────
#  SCHEDULER SETUP
# ─────────────────────────────────────────────────────────────────
def setup_scheduler(bot: Bot) -> AsyncIOScheduler:
    scheduler = AsyncIOScheduler(timezone="UTC")
    scheduler.add_job(
        promotion_engine, "interval",
        minutes=cfg.PROMOTION_ENGINE_INTERVAL,
        args=[bot],
        id="promotion_engine",
        name="Promotion Engine",
        replace_existing=True,
    )
    scheduler.add_job(
        view_tracking_engine, "interval",
        minutes=cfg.VIEW_TRACKING_INTERVAL,
        args=[bot],
        id="view_tracking",
        name="View Tracking",
        replace_existing=True,
    )
    return scheduler

# ─────────────────────────────────────────────────────────────────
#  BOT COMMANDS SETUP
# ─────────────────────────────────────────────────────────────────
async def set_bot_commands(bot: Bot):
    commands = [
        BotCommand(command="start", description="🏠 Main menu"),
        BotCommand(command="connect", description="📡 Connect a channel"),
        BotCommand(command="create_promo", description="🚀 Create a promotion"),
        BotCommand(command="mycredits", description="💰 Check balance"),
        BotCommand(command="mychannels", description="📡 My channels"),
        BotCommand(command="mypromotions", description="📢 My promotions"),
        BotCommand(command="leaderboard", description="🏆 Leaderboard"),
    ]
    await bot.set_my_commands(commands, scope=BotCommandScopeDefault())
    logger.info("✅ Bot commands set")

# ─────────────────────────────────────────────────────────────────
#  MAIN APPLICATION
# ─────────────────────────────────────────────────────────────────
async def on_startup(bot: Bot):
    await init_db()
    await set_bot_commands(bot)
    logger.info("✅ Bot started successfully!")
    # Notify admins
    for admin_id in cfg.ADMIN_IDS:
        try:
            await bot.send_message(admin_id, "🟢 <b>ChannelBoost Bot is online!</b>", parse_mode="HTML")
        except Exception:
            pass

async def on_shutdown(bot: Bot):
    logger.info("🔴 Bot shutting down...")
    for admin_id in cfg.ADMIN_IDS:
        try:
            await bot.send_message(admin_id, "🔴 <b>ChannelBoost Bot is offline.</b>", parse_mode="HTML")
        except Exception:
            pass

async def main():
    if not cfg.BOT_TOKEN:
        logger.error("❌ BOT_TOKEN is not set in .env file!")
        sys.exit(1)

    # Redis Storage for FSM
    storage = RedisStorage.from_url(cfg.REDIS_URL)

    # Bot & Dispatcher
    bot = Bot(
        token=cfg.BOT_TOKEN,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML)
    )
    dp = Dispatcher(storage=storage)

    # Middlewares
    dp.message.middleware(UserMiddleware())
    dp.callback_query.middleware(UserMiddleware())
    dp.message.middleware(AdminMiddleware())
    dp.callback_query.middleware(AdminMiddleware())

    # Include router
    dp.include_router(router)

    # Lifecycle hooks
    dp.startup.register(lambda: on_startup(bot))
    dp.shutdown.register(lambda: on_shutdown(bot))

    # Scheduler
    scheduler = setup_scheduler(bot)
    scheduler.start()
    logger.info(f"⏰ Scheduler started: Engine every {cfg.PROMOTION_ENGINE_INTERVAL}min, Views every {cfg.VIEW_TRACKING_INTERVAL}min")

    if cfg.WEBHOOK_URL:
        # Webhook mode
        logger.info(f"🌐 Starting in webhook mode: {cfg.WEBHOOK_URL}")
        await bot.set_webhook(
            url=cfg.WEBHOOK_URL + cfg.WEBHOOK_PATH,
            drop_pending_updates=True,
            allowed_updates=dp.resolve_used_update_types(),
        )
        app = web.Application()
        SimpleRequestHandler(dispatcher=dp, bot=bot).register(app, path=cfg.WEBHOOK_PATH)
        setup_application(app, dp, bot=bot)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, cfg.WEBAPP_HOST, cfg.WEBAPP_PORT)
        await site.start()
        logger.info(f"✅ Webhook server running on {cfg.WEBAPP_HOST}:{cfg.WEBAPP_PORT}")
        # Keep running
        await asyncio.Event().wait()
    else:
        # Polling mode
        logger.info("🔄 Starting in polling mode...")
        await bot.delete_webhook(drop_pending_updates=True)
        await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())

    scheduler.shutdown()

if __name__ == "__main__":
    asyncio.run(main())
