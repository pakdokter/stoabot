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
    build_belanja_conv,
    cmd_saldo, cmd_riwayat, cmd_cari,
    build_transaction_conv, build_edit_conv, build_hapus_conv,
)
from bot.handlers.report import cmd_ringkas, build_laporan_conv, build_statement_conv
from bot.handlers.ocr import build_ocr_conv

logger.remove()
logger.add(
    sys.stdout,
    format="<green>{time:YYYY-MM-DD HH:mm:ss}</green> | <level>{level}</level> | {message}",
    level=settings.log_level,
)

async def cmd_start(update, context):
    from bot.handlers.auth import ensure_registered
    if not await ensure_registered(update, context):
        return
    await update.message.reply_text(
        f"👋 Halo *{update.effective_user.first_name}*!\n\n"
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

async def cmd_help(update, context):
    await cmd_start(update, context)

async def unknown_command(update, context):
    await update.message.reply_text(
        "❓ Perintah tidak dikenali. Ketik /help untuk daftar perintah."
    )

async def post_init(application: Application):
    await init_db()
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

def create_app() -> Application:
    app = (
        Application.builder()
        .token(settings.telegram_bot_token)
        .post_init(post_init)
        .build()
    )
    app.add_handler(build_belanja_conv())
    app.add_handler(build_transaction_conv())
    app.add_handler(build_edit_conv())
    app.add_handler(build_hapus_conv())
    app.add_handler(build_laporan_conv())
    app.add_handler(build_statement_conv())
    app.add_handler(build_ocr_conv())
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("saldo", cmd_saldo))
    app.add_handler(CommandHandler("riwayat", cmd_riwayat))
    app.add_handler(CommandHandler("ringkas", cmd_ringkas))
    app.add_handler(CommandHandler("cari", cmd_cari))
    app.add_handler(CommandHandler("adduser", cmd_adduser))
    app.add_handler(CommandHandler("users", cmd_users))
    app.add_handler(MessageHandler(filters.COMMAND, unknown_command))
    return app

if __name__ == "__main__":
    app = create_app()
    app.run_polling(allowed_updates=Update.ALL_TYPES)
