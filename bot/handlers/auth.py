"""
Auth handler — verifikasi user dengan session 24 jam.

Flow:
  User baru      → auto-register → minta nama
  < 24 jam       → langsung allow
  > 24 jam       → tanya "Apakah ini [nama]?" → Ya/Bukan
"""
from datetime import datetime, timezone, timedelta
from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.ext import ContextTypes
from sqlalchemy import select
from loguru import logger

from bot.database import AsyncSessionLocal
from bot.models import User
from bot.config import settings

SESSION_TIMEOUT_HOURS = 24


async def ensure_registered(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """
    Middleware auth. Return True jika user boleh lanjut.
    Menangani verifikasi 24 jam otomatis.
    """
    tg_user = update.effective_user
    if not tg_user:
        return False

    user_id = tg_user.id
    username = tg_user.username or "no_username"
    full_name = tg_user.full_name or tg_user.first_name or "User"

    # Cek apakah sudah diverifikasi dalam session ini
    if context.user_data.get("session_verified"):
        return True

    try:
        async with AsyncSessionLocal() as session:
            result = await session.execute(select(User).where(User.id == user_id))
            user = result.scalar_one_or_none()

            if user is None:
                # User baru — auto-register
                role = "admin" if user_id in settings.admin_ids else "staff"
                user = User(
                    id=user_id,
                    username=username,
                    full_name=full_name,
                    role=role,
                    is_active=True,
                    last_seen=None,
                )
                session.add(user)
                await session.commit()
                logger.info(f"[AUTH] auto-registered uid={user_id} role={role}")
                context.user_data["db_user"] = user
                context.user_data["session_verified"] = True
                return True

            if not user.is_active:
                logger.warning(f"[AUTH] rejected uid={user_id} reason=inactive")
                try:
                    await update.message.reply_text("⛔ Akun Anda dinonaktifkan. Hubungi admin.")
                except Exception:
                    pass
                return False

            now = datetime.now(timezone.utc)
            last_seen = user.last_seen

            # Jika last_seen None (user lama sebelum fitur ini) atau < 24 jam → allow
            if last_seen is None or (now - last_seen) < timedelta(hours=SESSION_TIMEOUT_HOURS):
                # Update last_seen
                user.last_seen = now
                await session.commit()
                context.user_data["db_user"] = user
                context.user_data["session_verified"] = True
                logger.debug(f"[AUTH] allowed uid={user_id} last_seen={last_seen}")
                return True

            # > 24 jam — perlu verifikasi ulang
            logger.info(f"[AUTH] uid={user_id} session expired, asking verification")
            context.user_data["pending_verify_user"] = user
            context.user_data["pending_verify_uid"] = user_id
            await _ask_verification(update, user)
            return False

    except Exception as e:
        logger.exception(f"[AUTH] db error uid={user_id}: {e} — allowing fallback")
        context.user_data["db_user"] = User(
            id=user_id, username=username, full_name=full_name,
            role="admin" if user_id in settings.admin_ids else "staff",
            is_active=True,
        )
        context.user_data["session_verified"] = True
        return True


async def _ask_verification(update: Update, user: User):
    """Tanya apakah ini user yang benar setelah 24 jam."""
    name = user.full_name or "pengguna"
    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton(f"✅ Ya, ini {name}", callback_data=f"verify:yes"),
        InlineKeyboardButton("❌ Bukan saya", callback_data="verify:no"),
    ]])
    try:
        await update.message.reply_text(
            f"👋 Hei! Apakah ini *{name}*?",
            parse_mode="Markdown",
            reply_markup=keyboard,
        )
    except Exception as e:
        logger.error(f"[AUTH] ask_verification failed: {e}")


async def handle_verify_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handler tombol Ya/Bukan saat verifikasi 24 jam."""
    query = update.callback_query
    user_id = update.effective_user.id

    try:
        await query.answer()
    except Exception:
        pass

    action = query.data.split(":")[1]
    user: User = context.user_data.get("pending_verify_user")

    if action == "yes":
        # Verifikasi berhasil — update last_seen
        try:
            async with AsyncSessionLocal() as session:
                db_user = await session.get(User, user_id)
                if db_user:
                    db_user.last_seen = datetime.now(timezone.utc)
                    await session.commit()
                    context.user_data["db_user"] = db_user
        except Exception as e:
            logger.error(f"[AUTH] update last_seen failed: {e}")

        context.user_data["session_verified"] = True
        context.user_data.pop("pending_verify_user", None)
        context.user_data.pop("pending_verify_uid", None)

        name = user.full_name if user else "kamu"
        try:
            await query.edit_message_text(
                f"✅ Selamat datang kembali, *{name}*!\\n\\n"
                f"Ketik perintah yang ingin dilakukan.",
                parse_mode="Markdown",
            )
        except Exception:
            pass

    elif action == "no":
        # Bukan user ini — minta nama baru
        context.user_data.pop("pending_verify_user", None)
        context.user_data.pop("pending_verify_uid", None)
        context.user_data["waiting_name"] = True
        context.user_data["override_user_id"] = user_id

        try:
            await query.edit_message_text(
                "Maaf! Siapa nama kamu?\n\nKetik nama lengkap kamu:"
            )
        except Exception:
            pass


async def handle_name_input(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """
    Handler input nama — dipanggil saat waiting_name=True.
    Return True jika nama berhasil disimpan.
    """
    if not context.user_data.get("waiting_name"):
        return False

    name = update.message.text.strip()
    if len(name) < 2:
        await update.message.reply_text("❌ Nama terlalu pendek. Ketik nama lengkap kamu:")
        return True  # Tetap dalam mode waiting_name

    user_id = update.effective_user.id

    try:
        async with AsyncSessionLocal() as session:
            user = await session.get(User, user_id)
            if user:
                user.full_name = name
                user.last_seen = datetime.now(timezone.utc)
                await session.commit()
                context.user_data["db_user"] = user
    except Exception as e:
        logger.error(f"[AUTH] save name failed: {e}")

    context.user_data.pop("waiting_name", None)
    context.user_data["session_verified"] = True

    await update.message.reply_text(
        f"✅ Nama disimpan: *{name}*\\n\\n"
        f"Sekarang kamu bisa mulai.\\n"
        f"Ketik /start untuk melihat menu.",
        parse_mode="Markdown",
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
        status = "✅" if u.is_active else "❌"
        last = u.last_seen.strftime("%d/%m %H:%M") if u.last_seen else "belum login"
        lines.append(f"{status} `{u.id}` — {u.full_name} [{u.role}] _{last}_")
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")
