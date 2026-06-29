"""
Main entrypoint — assembles all handlers and starts the bot.
"""
import sys
from loguru import logger
from telegram import Update, BotCommand
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    CallbackQueryHandler, filters,
)

from bot.config import settings
from bot.database import init_db, AsyncSessionLocal
from bot.models import User
from bot.handlers.auth import (
    cmd_adduser, cmd_listuser, cmd_deleteuser, cmd_resetpass,
    cmd_users, cmd_deactivate,
    cmd_logout, build_login_conv,
    handle_verify_callback,
    handle_name_input as auth_name_input,
    ensure_registered,
)
from bot.handlers.transaction import (
    cmd_saldo, cmd_riwayat, cmd_cari,
    build_transaction_conv, build_edit_conv, build_hapus_conv,
)
from bot.handlers.report import cmd_ringkas, build_laporan_conv, build_statement_conv, build_laporan_teks_conv
from bot.handlers.ocr import build_ocr_conv


# ── Logging ──────────────────────────────────────────────────────────

logger.remove()
logger.add(
    sys.stdout,
    format="<green>{time:YYYY-MM-DD HH:mm:ss}</green> | <level>{level}</level> | {message}",
    level=settings.log_level,
)


# ── /start ────────────────────────────────────────────────────────────

async def cmd_start(update, context):
    if not await ensure_registered(update, context):
        return

    user_id = update.effective_user.id
    tg_name = update.effective_user.full_name or update.effective_user.first_name or ""

    async with AsyncSessionLocal() as session:
        user = await session.get(User, user_id)
        has_name = (
            user and user.full_name
            and user.full_name not in ("no_username", "")
            and len(user.full_name) > 1
        )

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
        "*📥 Pemasukan & Pengeluaran:*\n"
        "/masuk — catat pemasukan\n"
        "/keluar — catat pengeluaran (pilih toko atau pasar)\n\n"
        "*📊 Laporan:*\n"
        "/saldo — cek saldo saat ini\n"
        "/riwayat — riwayat transaksi\n"
        "/ringkas — ringkasan bulan ini\n"
        "/laporan — laporan per periode\n"
        "/statement — e-statement PDF\n"
        "/laporan\\_teks — rekap laporan teks harian staff\n\n"
        "*✏️ Manajemen:*\n"
        "/edit — edit transaksi\n"
        "/hapus — hapus transaksi\n"
        "/cari — cari transaksi\n\n"
        "📸 Kirim *foto struk* untuk input otomatis!\n\n"
        "/batal — batalkan perintah aktif",
        parse_mode="Markdown",
    )


async def handle_name_input(update, context):
    """Delegasi ke auth handler untuk input nama."""
    await auth_name_input(update, context)


async def cmd_help(update, context):
    await cmd_start(update, context)


async def unknown_command(update, context):
    await update.message.reply_text(
        "❓ Perintah tidak dikenali. Ketik /help untuk daftar perintah."
    )


# ── Post init ─────────────────────────────────────────────────────────

async def post_init(application: Application):
    await init_db()
    logger.info("Database initialized")
    await application.bot.set_my_commands([
        BotCommand("masuk",        "Catat pemasukan"),
        BotCommand("keluar",       "Catat pengeluaran (pilih toko/pasar)"),
        BotCommand("saldo",        "Cek saldo saat ini"),
        BotCommand("riwayat",      "Riwayat transaksi"),
        BotCommand("ringkas",      "Ringkasan bulan ini"),
        BotCommand("laporan",      "Laporan per periode"),
        BotCommand("statement",    "E-statement PDF"),
        BotCommand("laporan_teks", "Rekap laporan teks harian staff"),
        BotCommand("edit",         "Edit transaksi"),
        BotCommand("hapus",        "Hapus transaksi"),
        BotCommand("cari",         "Cari transaksi"),
        BotCommand("batal",        "Batalkan perintah aktif"),
    ])
    logger.info(f"Starting bot — {settings.business_name}")


# ── App builder ───────────────────────────────────────────────────────

def create_app() -> Application:
    app = (
        Application.builder()
        .token(settings.telegram_bot_token)
        .post_init(post_init)
        .build()
    )

    # ConversationHandlers — harus didaftarkan lebih dulu
    app.add_handler(build_transaction_conv())
    app.add_handler(build_edit_conv())
    app.add_handler(build_hapus_conv())
    app.add_handler(build_laporan_conv())
    app.add_handler(build_statement_conv())
    app.add_handler(build_ocr_conv())

    # Command handlers
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("saldo", cmd_saldo))
    app.add_handler(CommandHandler("riwayat", cmd_riwayat))
    app.add_handler(CommandHandler("ringkas", cmd_ringkas))
    app.add_handler(build_laporan_teks_conv())
    app.add_handler(CommandHandler("cari", cmd_cari))
    # Auth commands
    app.add_handler(build_login_conv())
    app.add_handler(CommandHandler("logout", cmd_logout))
    app.add_handler(CommandHandler("adduser", cmd_adduser))
    app.add_handler(CommandHandler("listuser", cmd_listuser))
    app.add_handler(CommandHandler("deleteuser", cmd_deleteuser))
    app.add_handler(CommandHandler("resetpass", cmd_resetpass))
    app.add_handler(CommandHandler("users", cmd_users))

    # Callback & message handlers
    app.add_handler(CallbackQueryHandler(handle_verify_callback, pattern="^verify:"))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_name_input), group=99)
    app.add_handler(MessageHandler(filters.COMMAND, unknown_command))

    return app


# ── Entry ─────────────────────────────────────────────────────────────

app = create_app()

if __name__ == "__main__":
    app.run_polling(allowed_updates=Update.ALL_TYPES)
