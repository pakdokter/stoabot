"""
Auth handler — registrasi dan verifikasi user.
Hanya user yang terdaftar di tabel users yang dapat menggunakan bot.
"""
from telegram import Update
from telegram.ext import ContextTypes
from sqlalchemy import select
from loguru import logger

from bot.database import AsyncSessionLocal
from bot.models import User
from bot.config import settings


async def ensure_registered(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """
    Middleware: cek apakah pengirim pesan terdaftar dan aktif.
    Return True jika boleh lanjut, False jika ditolak.
    """
    tg_user = update.effective_user
    if not tg_user:
        return False

    async with AsyncSessionLocal() as session:
        result = await session.execute(
            select(User).where(User.id == tg_user.id)
        )
        user = result.scalar_one_or_none()

        if user is None:
            # Auto-register jika admin id
            if tg_user.id in settings.admin_ids:
                user = User(
                    id=tg_user.id,
                    username=tg_user.username,
                    full_name=tg_user.full_name or tg_user.first_name or "Admin",
                    role="admin",
                )
                session.add(user)
                await session.commit()
                logger.info(f"Auto-registered admin: {tg_user.id}")
                return True
            else:
                await update.message.reply_text(
                    "⛔ Akses ditolak.\n"
                    "Hubungi admin untuk mendapatkan akses ke bot ini."
                )
                return False

        if not user.is_active:
            await update.message.reply_text("⛔ Akun Anda dinonaktifkan. Hubungi admin.")
            return False

        # Simpan user ke context untuk dipakai handler lain
        context.user_data["db_user"] = user
        return True


async def cmd_adduser(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin command: /adduser <telegram_id> <nama> [staff|admin]"""
    tg_user = update.effective_user
    if tg_user.id not in settings.admin_ids:
        await update.message.reply_text("⛔ Hanya admin yang dapat menambah user.")
        return

    args = context.args
    if len(args) < 2:
        await update.message.reply_text("Usage: /adduser <telegram_id> <nama lengkap> [staff|admin]")
        return

    try:
        new_id = int(args[0])
        full_name = " ".join(args[1:-1]) if len(args) > 2 and args[-1] in ("staff", "admin") else " ".join(args[1:])
        role = args[-1] if args[-1] in ("staff", "admin") else "staff"

        async with AsyncSessionLocal() as session:
            existing = await session.get(User, new_id)
            if existing:
                await update.message.reply_text(f"User {new_id} sudah terdaftar.")
                return
            user = User(id=new_id, full_name=full_name, role=role)
            session.add(user)
            await session.commit()

        await update.message.reply_text(
            f"✅ User berhasil ditambahkan\n"
            f"ID: {new_id}\n"
            f"Nama: {full_name}\n"
            f"Role: {role}"
        )
    except ValueError:
        await update.message.reply_text("❌ telegram_id harus berupa angka.")


async def cmd_users(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin command: /users — lihat daftar user."""
    if update.effective_user.id not in settings.admin_ids:
        await update.message.reply_text("⛔ Hanya admin.")
        return

    async with AsyncSessionLocal() as session:
        result = await session.execute(select(User).order_by(User.created_at))
        users = result.scalars().all()

    if not users:
        await update.message.reply_text("Belum ada user terdaftar.")
        return

    lines = ["👥 *Daftar User*\n"]
    for u in users:
        status = "✅" if u.is_active else "❌"
        lines.append(f"{status} `{u.id}` — {u.full_name} [{u.role}]")
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")
