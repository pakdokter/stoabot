"""
Auth handler — auto-register semua user baru.
ROOT CAUSE FIX: sebelumnya hanya admin yang auto-register, semua user baru ditolak.
"""
from telegram import Update
from telegram.ext import ContextTypes
from sqlalchemy import select
from loguru import logger

from bot.database import AsyncSessionLocal
from bot.models import User
from bot.config import settings


async def ensure_registered(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    tg_user = update.effective_user
    if not tg_user:
        return False

    user_id = tg_user.id
    username = tg_user.username or "no_username"
    full_name = tg_user.full_name or tg_user.first_name or "User"

    try:
        async with AsyncSessionLocal() as session:
            result = await session.execute(select(User).where(User.id == user_id))
            user = result.scalar_one_or_none()

            if user is None:
                role = "admin" if user_id in settings.admin_ids else "staff"
                user = User(id=user_id, username=username, full_name=full_name, role=role, is_active=True)
                session.add(user)
                await session.commit()
                logger.info(f"[AUTH] auto-registered uid={user_id} username={username!r} role={role}")

            elif not user.is_active:
                logger.warning(f"[AUTH] rejected uid={user_id} reason=inactive")
                await update.message.reply_text("⛔ Akun Anda dinonaktifkan. Hubungi admin.")
                return False

            else:
                logger.debug(f"[AUTH] allowed uid={user_id} role={user.role}")

            context.user_data["db_user"] = user
            return True

    except Exception as e:
        logger.exception(f"[AUTH] db error uid={user_id} error={e} — allowing fallback")
        context.user_data["db_user"] = User(
            id=user_id, username=username, full_name=full_name,
            role="admin" if user_id in settings.admin_ids else "staff", is_active=True,
        )
        return True


async def cmd_adduser(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in settings.admin_ids:
        await update.message.reply_text("⛔ Hanya admin.")
        return
    args = context.args
    if len(args) < 2:
        await update.message.reply_text("Usage: /adduser <telegram_id> <nama> [staff|admin]")
        return
    try:
        new_id = int(args[0])
        role = args[-1] if args[-1] in ("staff", "admin") else "staff"
        full_name = " ".join(args[1:-1]) if len(args) > 2 and args[-1] in ("staff", "admin") else " ".join(args[1:])
        async with AsyncSessionLocal() as session:
            existing = await session.get(User, new_id)
            if existing:
                await update.message.reply_text(f"User {new_id} sudah terdaftar.")
                return
            session.add(User(id=new_id, full_name=full_name, role=role))
            await session.commit()
        await update.message.reply_text(f"✅ Ditambahkan\nID: {new_id}\nNama: {full_name}\nRole: {role}")
    except ValueError:
        await update.message.reply_text("❌ telegram_id harus angka.")


async def cmd_deactivate(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in settings.admin_ids:
        await update.message.reply_text("⛔ Hanya admin.")
        return
    if not context.args:
        await update.message.reply_text("Usage: /deactivate <telegram_id>")
        return
    try:
        target_id = int(context.args[0])
        async with AsyncSessionLocal() as session:
            user = await session.get(User, target_id)
            if not user:
                await update.message.reply_text("User tidak ditemukan.")
                return
            user.is_active = False
            await session.commit()
        await update.message.reply_text(f"✅ User {target_id} dinonaktifkan.")
    except ValueError:
        await update.message.reply_text("❌ telegram_id harus angka.")


async def cmd_users(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in settings.admin_ids:
        await update.message.reply_text("⛔ Hanya admin.")
        return
    async with AsyncSessionLocal() as session:
        result = await session.execute(select(User).order_by(User.created_at))
        users = result.scalars().all()
    if not users:
        await update.message.reply_text("Belum ada user.")
        return
    lines = ["👥 *Daftar User*\n"]
    for u in users:
        lines.append(f"{'✅' if u.is_active else '❌'} `{u.id}` — {u.full_name} [{u.role}]")
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")
