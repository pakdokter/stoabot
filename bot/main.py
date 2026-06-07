"""
Main entrypoint — assembles all handlers and starts the bot.
"""
import sys
from loguru import logger
from telegram import Update, BotCommand
from telegram.ext import Application, CommandHandler, MessageHandler, filters

from bot.config import settings
from bot.database import init_db
from bot.handlers.auth import cmd_adduser, cmd_users
from bot.handlers.transaction import (
    cmd_saldo, cmd_riwayat, cmd_cari,
    build_transaction_conv, build_edit_conv, build_hapus_conv,
)
from bot.handlers.report import cmd_ringkas, build_laporan_conv, build_statement_conv
from bot.handlers.ocr import build_ocr_conv


# ──────────────────────────────────────────────
# Logging setup
# ──────────────────────────────────────────────

logger.remove()
logger.add(
    sys.stdout,
    format="<green>{time:YYYY-MM-DD HH:mm:ss}</green> | <level>{level}</level> | {message}",
    level=settings.log_level,
)


# ──────────────────────────────────────────────
# /start and /help
# ──────────────────────────────────────────────

async def cmd_start(update, context):
    from bot.handlers.auth import ensure_registered
    from bot.database import AsyncSessionLocal
    from bot.models import User
    if not await ensure_registered(update, context):
        return

    user_id = update.effective_user.id
    tg_name = update.effective_user.full_name or update.effective_user.first_name or ""

    # Cek apakah user sudah punya nama di DB
    async with AsyncSessionLocal() as session:
        user = await session.get(User, user_id)
        has_name = user and user.full_name and user.full_name != "no_username" and len(user.full_name) > 1

    if not has_name:
        context.user_data["waiting_name"] = True
        await update.message.reply_text(
            f"👋 Halo! Selamat datang di bot keuangan *{settings.business_name}*.\n\n"
            f"Sebelum mulai, ketik *nama lengkap* kamu:",
            parse_mode="Markdown",
        )
        return

    await update.message.reply_text(
        f"👋 Halo *{tg_name}*!\n\n"
        f"Saya adalah bot pencatat keuangan *{settings.business_name}*.\n\n"
        "*Perintah tersedia:*\n"
        "/masuk — catat pemasukan\n"
        "/keluar — catat pengeluaran\n"
        "/saldo — cek saldo\n"
        "/riwayat — riwayat transaksi\n"
        "/laporan — laporan per periode\n"
        "/ringkas — ringkasan bulan ini\n"
        "/statement — e-statement PDF\n"
        "/edit — edit transaksi\n"
        "/hapus — hapus transaksi\n"
        "/cari — cari transaksi\n\n"
        "📸 Kirim *foto struk* untuk input otomatis!\n\n"
        "/batal — batalkan perintah aktif",
        parse_mode="Markdown",
    )


async def handle_name_input(update, context):
    """Handler untuk input nama user saat /start pertama kali."""
    if not context.user_data.get("waiting_name"):
        return
    name = update.message.text.strip()
    if len(name) < 2:
        await update.message.reply_text("❌ Nama terlalu pendek. Ketik nama lengkap kamu:")
        return
    user_id = update.effective_user.id
    from bot.database import AsyncSessionLocal
    from bot.models import User
    async with AsyncSessionLocal() as session:
        user = await session.get(User, user_id)
        if user:
            user.full_name = name
            await session.commit()
    context.user_data.pop("waiting_name", None)
    await update.message.reply_text(
        f"✅ Nama disimpan: *{name}*\n\n"
        f"Sekarang kamu bisa mulai mencatat keuangan.\n\n"
        "*Perintah tersedia:*\n"
        "/masuk — catat pemasukan\n"
        "/keluar — catat pengeluaran\n"
        "/saldo — cek saldo\n"
        "/riwayat — riwayat transaksi\n"
        "/laporan — laporan per periode\n\n"
        "📸 Kirim *foto struk* untuk input otomatis!",
        parse_mode="Markdown",
    )


async def cmd_help(update, context):
    await cmd_start(update, context)


async def unknown_command(update, context):
    await update.message.reply_text(
        "❓ Perintah tidak dikenali. Ketik /help untuk daftar perintah."
    )


# ──────────────────────────────────────────────
# Post init
# ──────────────────────────────────────────────

async def post_init(application: Application):
    await init_db()
    logger.info("Database initialized")
    await application.bot.set_my_commands([
        BotCommand("masuk", "Catat pemasukan"),
        BotCommand("keluar", "Catat pengeluaran"),
        BotCommand("saldo", "Cek saldo"),
        BotCommand("riwayat", "Riwayat transaksi"),
        BotCommand("laporan", "Laporan per periode"),
        BotCommand("ringkas", "Ringkasan bulan ini"),
        BotCommand("statement", "E-statement PDF"),
        BotCommand("edit", "Edit transaksi"),
        BotCommand("hapus", "Hapus transaksi"),
        BotCommand("cari", "Cari transaksi"),
        BotCommand("batal", "Batalkan perintah aktif"),
    ])
    logger.info(f"Starting bot — {settings.business_name}")


# ──────────────────────────────────────────────
# App builder
# ──────────────────────────────────────────────

def create_app() -> Application:
    app = (
        Application.builder()
        .token(settings.telegram_bot_token)
        .post_init(post_init)
        .build()
    )

    app.add_handler(build_transaction_conv())
    app.add_handler(build_edit_conv())
    app.add_handler(build_hapus_conv())
    app.add_handler(build_laporan_conv())
    app.add_handler(build_statement_conv())
    app.add_handler(build_ocr_conv())

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_name_input), group=99)
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("saldo", cmd_saldo))
    app.add_handler(CommandHandler("riwayat", cmd_riwayat))
    app.add_handler(CommandHandler("ringkas", cmd_ringkas))
    app.add_handler(CommandHandler("cari", cmd_cari))
    app.add_handler(CommandHandler("adduser", cmd_adduser))
    app.add_handler(CommandHandler("users", cmd_users))
    app.add_handler(MessageHandler(filters.COMMAND, unknown_command))

    return app


# ──────────────────────────────────────────────
# Entry — gunakan run_polling langsung tanpa asyncio.run
# ──────────────────────────────────────────────

if __name__ == "__main__":
    app = create_app()
    app.run_polling(allowed_updates=Update.ALL_TYPES)
