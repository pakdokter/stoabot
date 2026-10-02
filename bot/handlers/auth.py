"""
Auth handler — login dengan password yang di-generate admin.

Flow:
  User baru/belum login → /login → minta password
  Password benar        → sesi aktif 24 jam
  Password salah        → tolak
  Admin                 → /adduser nama password [role]
                          /listuser
                          /deleteuser nama
"""
import hashlib
import secrets
from datetime import datetime, timezone, timedelta

from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.ext import ContextTypes, CommandHandler, MessageHandler, filters, ConversationHandler
from sqlalchemy import select
from loguru import logger

from bot.database import AsyncSessionLocal
from bot.models import User
from bot.config import settings

SESSION_TIMEOUT_HOURS = 24 * 7  # 7 hari — diperpanjang agar tidak perlu login ulang setiap hari
WAITING_USERNAME = 10
WAITING_PASSWORD = 11


def _hash(password: str) -> str:
    return hashlib.sha256(password.strip().encode()).hexdigest()


async def ensure_registered(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """
    Middleware auth. Return True jika user sudah login dan sesi masih aktif.
    Admin (TELEGRAM_ADMIN_IDS) selalu lolos tanpa password.
    """
    tg_user = update.effective_user
    if not tg_user:
        return False

    user_id = tg_user.id

    # Admin bypass
    if user_id in settings.admin_ids:
        if not context.user_data.get("session_verified"):
            async with AsyncSessionLocal() as session:
                user = await session.get(User, user_id)
                if not user:
                    user = User(
                        id=user_id,
                        username=tg_user.username or "",
                        full_name=tg_user.full_name or "Admin",
                        role="admin",
                        is_active=True,
                        last_seen=datetime.now(timezone.utc),
                    )
                    session.add(user)
                    await session.commit()
                context.user_data["db_user"] = user
            context.user_data["session_verified"] = True
        return True

    # Cek sesi in-memory
    if context.user_data.get("session_verified"):
        return True

    # Cek DB — apakah user terdaftar dan punya password
    try:
        async with AsyncSessionLocal() as session:
            user = await session.get(User, user_id)

        if user and user.is_active and user.pin:
            # Cek last_seen — sesi 7 hari
            now = datetime.now(timezone.utc)
            if user.last_seen and (now - user.last_seen) < timedelta(hours=SESSION_TIMEOUT_HOURS):
                # Rolling session: update last_seen max sekali per jam agar timer reset
                needs_update = (
                    not user.last_seen or
                    (now - user.last_seen) > timedelta(hours=1)
                )
                if needs_update:
                    async with AsyncSessionLocal() as session2:
                        user2 = await session2.get(User, user_id)
                        if user2:
                            user2.last_seen = now
                            await session2.commit()
                            context.user_data["db_user"] = user2
                        else:
                            context.user_data["db_user"] = user
                else:
                    context.user_data["db_user"] = user
                context.user_data["session_verified"] = True
                return True

        # Belum login / sesi expired
        msg = update.message or (update.callback_query.message if update.callback_query else None)
        if msg:
            await msg.reply_text(
                "🔐 Kamu belum login.\n\n"
                "Ketik /login untuk masuk."
            )
        return False

    except Exception as e:
        logger.exception(f"[AUTH] error uid={user_id}: {e}")
        return False


# ─────────────────────────────────────────────
# /login
# ─────────────────────────────────────────────

async def cmd_login(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Mulai proses login — minta username dulu."""
    user_id = update.effective_user.id

    # Admin tidak perlu login
    if user_id in settings.admin_ids:
        await update.message.reply_text("✅ Kamu adalah admin, tidak perlu login.")
        return ConversationHandler.END

    # Cek sudah login
    if context.user_data.get("session_verified"):
        db_user = context.user_data.get("db_user")
        name = db_user.full_name if db_user else "kamu"
        await update.message.reply_text(f"✅ Kamu sudah login sebagai *{name}*.", parse_mode="Markdown")
        return ConversationHandler.END

    await update.message.reply_text(
        "🔐 *Login Stoabot*\n\nUsername:",
        parse_mode="Markdown",
    )
    return WAITING_USERNAME


async def handle_username_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Proses input username, lalu minta password."""
    username = update.message.text.strip()

    if len(username) < 2:
        await update.message.reply_text("❌ Username tidak valid. Coba lagi:")
        return WAITING_USERNAME

    context.user_data["login_username"] = username.lower()

    await update.message.reply_text("Password:")
    return WAITING_PASSWORD


async def handle_password_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Proses input password — cari user berdasarkan username + password."""
    user_id = update.effective_user.id
    password = update.message.text.strip()
    username = context.user_data.get("login_username", "")

    # Hapus pesan password agar tidak terlihat di chat
    try:
        await update.message.delete()
    except Exception:
        pass

    if not username:
        await update.message.reply_text("❌ Session expired. Ketik /login ulang.")
        return ConversationHandler.END

    try:
        async with AsyncSessionLocal() as session:
            # Cari berdasarkan pin_name (username yang dibuat admin)
            result = await session.execute(
                select(User).where(
                    User.pin_name == username,
                    User.is_active == True,
                )
            )
            user = result.scalar_one_or_none()

            if not user:
                await update.message.reply_text(
                    "❌ Username tidak ditemukan.\n"
                    "Hubungi admin untuk mendapatkan akses."
                )
                context.user_data.pop("login_username", None)
                return ConversationHandler.END

            if not user.pin or user.pin != _hash(password):
                await update.message.reply_text(
                    "❌ Password salah.\nKetik /login untuk coba lagi."
                )
                context.user_data.pop("login_username", None)
                return ConversationHandler.END

            if not user.is_active:
                await update.message.reply_text("⛔ Akun dinonaktifkan. Hubungi admin.")
                return ConversationHandler.END

            # Bind telegram_id ke akun jika belum (login pertama kali)
            if user.id < 0:
                user.id = user_id
                user.username = update.effective_user.username or ""

            user.last_seen = datetime.now(timezone.utc)
            await session.commit()
            await session.refresh(user)

            context.user_data["db_user"] = user
            context.user_data["session_verified"] = True
            context.user_data.pop("login_username", None)

            await update.message.reply_text(
                f"✅ *Login berhasil!*\n\n"
                f"Selamat datang, *{user.full_name}*.\n"
                f"Sesi aktif 24 jam.\n\n"
                f"Ketik /start untuk melihat menu.",
                parse_mode="Markdown",
            )
            logger.info(f"[AUTH] login uid={user_id} name={user.full_name}")
            return ConversationHandler.END

    except Exception as e:
        logger.exception(f"[AUTH] login error uid={user_id}: {e}")
        await update.message.reply_text("❌ Terjadi error. Coba lagi.")
        return ConversationHandler.END


async def cmd_logout(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Logout — hapus sesi."""
    _p = {k: context.user_data[k] for k in ("db_user",) if k in context.user_data}
    context.user_data.clear()
    db_user = _p.get("db_user")
    name = db_user.full_name if db_user else "kamu"
    await update.message.reply_text(f"👋 Sampai jumpa, *{name}*. Sesi dihapus.", parse_mode="Markdown")


def build_login_conv() -> ConversationHandler:
    return ConversationHandler(
        entry_points=[CommandHandler("login", cmd_login)],
        states={
            WAITING_USERNAME: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, handle_username_input),
            ],
            WAITING_PASSWORD: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, handle_password_input),
            ],
        },
        fallbacks=[CommandHandler("batal", lambda u, c: ConversationHandler.END)],
        allow_reentry=True,
    )


# ─────────────────────────────────────────────
# Admin commands
# ─────────────────────────────────────────────

def _is_admin(update: Update) -> bool:
    return update.effective_user.id in settings.admin_ids


async def cmd_adduser(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /adduser nama password [staff|admin]
    Admin generate akun untuk staff.
    """
    if not _is_admin(update):
        await update.message.reply_text("⛔ Hanya admin.")
        return

    args = context.args
    if len(args) < 2:
        await update.message.reply_text(
            "Usage: /adduser <nama> <password> [staff|admin]\n\n"
            "Contoh: /adduser Baiq baiq123\n"
            "Contoh: /adduser Ainun ainun456 staff"
        )
        return

    role = "staff"
    if args[-1] in ("staff", "admin"):
        role = args[-1]
        name = " ".join(args[:-2])
        password = args[-2]
    else:
        name = " ".join(args[:-1])
        password = args[-1]

    if len(name) < 2:
        await update.message.reply_text("❌ Nama terlalu pendek.")
        return
    if len(password) < 4:
        await update.message.reply_text("❌ Password minimal 4 karakter.")
        return

    hashed = _hash(password)

    try:
        async with AsyncSessionLocal() as session:
            # Cek duplikat nama
            result = await session.execute(
                select(User).where(User.pin_name == name.lower())
            )
            existing = result.scalar_one_or_none()
            if existing:
                await update.message.reply_text(f"❌ Nama '{name}' sudah terdaftar.")
                return

            # Buat user baru dengan ID placeholder (akan di-bind saat login)
            # Gunakan ID negatif random sebagai placeholder
            placeholder_id = -secrets.randbelow(10**9)
            user = User(
                id=placeholder_id,
                username="",
                full_name=name,
                pin_name=name.lower(),
                pin=hashed,
                role=role,
                is_active=True,
            )
            session.add(user)
            await session.commit()

        await update.message.reply_text(
            f"✅ *Akun berhasil dibuat*\n\n"
            f"Nama: *{name}*\n"
            f"Password: `{password}`\n"
            f"Role: {role}\n\n"
            f"Bagikan info ini ke staff.\n"
            f"Mereka login dengan /login lalu masukkan password.",
            parse_mode="Markdown",
        )
        logger.info(f"[AUTH] adduser name={name} role={role}")

    except Exception as e:
        logger.exception(f"[AUTH] adduser error: {e}")
        await update.message.reply_text(f"❌ Error: {e}")


async def cmd_listuser(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/listuser — tampilkan semua user."""
    if not _is_admin(update):
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
        status = "✅" if u.is_active else "❌"
        last = u.last_seen.strftime("%d/%m %H:%M") if u.last_seen else "belum login"
        has_pin = "🔑" if u.pin else "🔓"
        role_label = "👑" if u.role == "admin" else "👤"
        lines.append(f"{status}{has_pin}{role_label} *{u.full_name}* — _{last}_")

    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


async def cmd_deleteuser(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/deleteuser nama — nonaktifkan user."""
    if not _is_admin(update):
        await update.message.reply_text("⛔ Hanya admin.")
        return

    if not context.args:
        await update.message.reply_text("Usage: /deleteuser <nama>")
        return

    name = " ".join(context.args)

    async with AsyncSessionLocal() as session:
        result = await session.execute(
            select(User).where(User.pin_name == name.lower())
        )
        user = result.scalar_one_or_none()

        if not user:
            await update.message.reply_text(f"❌ User '{name}' tidak ditemukan.")
            return

        user.is_active = False
        await session.commit()

    await update.message.reply_text(f"✅ User *{user.full_name}* dinonaktifkan.", parse_mode="Markdown")


async def cmd_resetpass(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/resetpass nama password_baru — reset password user."""
    if not _is_admin(update):
        await update.message.reply_text("⛔ Hanya admin.")
        return

    args = context.args
    if len(args) < 2:
        await update.message.reply_text("Usage: /resetpass <nama> <password_baru>")
        return

    new_pass = args[-1]
    name = " ".join(args[:-1])

    async with AsyncSessionLocal() as session:
        result = await session.execute(
            select(User).where(User.pin_name == name.lower())
        )
        user = result.scalar_one_or_none()

        if not user:
            await update.message.reply_text(f"❌ User '{name}' tidak ditemukan.")
            return

        user.pin = _hash(new_pass)
        await session.commit()

    await update.message.reply_text(
        f"✅ Password *{user.full_name}* direset.\n"
        f"Password baru: `{new_pass}`",
        parse_mode="Markdown",
    )


# Legacy — tetap untuk kompatibilitas
async def handle_verify_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    pass

async def handle_name_input(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    return False

async def cmd_deactivate(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await cmd_deleteuser(update, context)

async def cmd_users(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await cmd_listuser(update, context)
