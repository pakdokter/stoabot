"""
Report handlers:
  /laporan  — filter rentang tanggal
  /ringkas  — dashboard bulan ini
  /statement — PDF rekening koran
"""
import io
from calendar import monthrange
from datetime import date, timedelta

from telegram import Update
from telegram.ext import (
    ContextTypes, ConversationHandler, CommandHandler, MessageHandler, filters,
)
from sqlalchemy import select, desc
from loguru import logger

from bot.database import AsyncSessionLocal
from bot.models import Transaction
from bot.services.balance import get_summary
from bot.services.pdf_service import generate_statement_pdf
from bot.utils.formatters import fmt_rupiah, fmt_date, fmt_date_full, parse_date
from bot.handlers.auth import ensure_registered

# States
(
    LAPORAN_FROM, LAPORAN_TO,
    STMT_BULAN, STMT_TAHUN,
) = range(4)


# ──────────────────────────────────────────────
# /ringkas — dashboard bulan ini
# ──────────────────────────────────────────────

async def cmd_ringkas(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await ensure_registered(update, context):
        return

    today = date.today()
    first_day = date(today.year, today.month, 1)

    async with AsyncSessionLocal() as session:
        summary = await get_summary(session, date_from=first_day, date_to=today)

    days_passed = (today - first_day).days + 1
    avg_keluar = summary["total_keluar"] / days_passed if days_passed > 0 else 0

    await update.message.reply_text(
        f"📊 *Ringkasan Bulan Ini*\n"
        f"_{fmt_date_full(first_day)} — {fmt_date_full(today)}_\n\n"
        f"Pemasukan:\n*{fmt_rupiah(summary['total_masuk'])}*\n\n"
        f"Pengeluaran:\n*{fmt_rupiah(summary['total_keluar'])}*\n\n"
        f"Laba Bersih:\n*{fmt_rupiah(summary['saldo'])}*\n\n"
        f"Jumlah Transaksi:\n*{summary['jumlah']}*\n\n"
        f"Rata-rata Pengeluaran Harian:\n*{fmt_rupiah(avg_keluar)}*",
        parse_mode="Markdown",
    )


# ──────────────────────────────────────────────
# /laporan — filter tanggal
# ──────────────────────────────────────────────

async def cmd_laporan(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await ensure_registered(update, context):
        return ConversationHandler.END
    await update.message.reply_text("📅 Tanggal mulai? (DD/MM/YYYY)\nContoh: 01/07/2026")
    return LAPORAN_FROM


async def laporan_from(update: Update, context: ContextTypes.DEFAULT_TYPE):
    d = parse_date(update.message.text)
    if not d:
        await update.message.reply_text("❌ Format tidak valid. Contoh: 01/07/2026")
        return LAPORAN_FROM
    context.user_data["laporan_from"] = d
    await update.message.reply_text("Tanggal akhir? (DD/MM/YYYY)")
    return LAPORAN_TO


async def laporan_to(update: Update, context: ContextTypes.DEFAULT_TYPE):
    d = parse_date(update.message.text)
    if not d:
        await update.message.reply_text("❌ Format tidak valid.")
        return LAPORAN_TO

    date_from = context.user_data["laporan_from"]
    date_to = d

    if date_to < date_from:
        await update.message.reply_text("❌ Tanggal akhir tidak boleh sebelum tanggal mulai.")
        return LAPORAN_TO

    async with AsyncSessionLocal() as session:
        summary = await get_summary(session, date_from=date_from, date_to=date_to)

        result = await session.execute(
            select(Transaction)
            .where(
                Transaction.is_deleted == False,
                Transaction.transaction_date >= date_from,
                Transaction.transaction_date <= date_to,
            )
            .order_by(desc(Transaction.transaction_date))
            .limit(30)
        )
        txs = result.scalars().all()

    lines = [
        f"📊 *Laporan {fmt_date(date_from)} s/d {fmt_date(date_to)}*\n",
        f"Total Masuk:\n*{fmt_rupiah(summary['total_masuk'])}*\n",
        f"Total Keluar:\n*{fmt_rupiah(summary['total_keluar'])}*\n",
        f"Saldo Periode:\n*{fmt_rupiah(summary['saldo'])}*\n",
        f"Jumlah Transaksi: {summary['jumlah']}\n",
        "─────────────────",
    ]

    for tx in txs:
        sign = "+" if tx.type == "masuk" else "-"
        lines.append(f"*{fmt_date(tx.transaction_date)}* {sign}{fmt_rupiah(tx.amount)}\n_{tx.description}_")

    if not txs:
        lines.append("_(tidak ada transaksi dalam periode ini)_")

    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")
    context.user_data.clear()
    return ConversationHandler.END


# ──────────────────────────────────────────────
# /statement — PDF
# ──────────────────────────────────────────────

async def cmd_statement(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await ensure_registered(update, context):
        return ConversationHandler.END

    today = date.today()
    await update.message.reply_text(
        f"📄 *E-Statement PDF*\n\nBulan? (1-12, contoh: 7 untuk Juli)\n"
        f"Atau ketik `sekarang` untuk bulan ini ({today.month}/{today.year})",
        parse_mode="Markdown",
    )
    return STMT_BULAN


async def stmt_bulan(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip().lower()
    today = date.today()

    if text in ("sekarang", "ini", "now"):
        context.user_data["stmt_month"] = today.month
        context.user_data["stmt_year"] = today.year
        return await _generate_statement(update, context)

    try:
        month = int(text)
        if not 1 <= month <= 12:
            raise ValueError
    except ValueError:
        await update.message.reply_text("❌ Masukkan angka bulan (1-12).")
        return STMT_BULAN

    context.user_data["stmt_month"] = month
    await update.message.reply_text(f"Tahun? (contoh: {today.year})")
    return STMT_TAHUN


async def stmt_tahun(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        year = int(update.message.text.strip())
        if year < 2000 or year > 2100:
            raise ValueError
    except ValueError:
        await update.message.reply_text("❌ Tahun tidak valid.")
        return STMT_TAHUN

    context.user_data["stmt_year"] = year
    return await _generate_statement(update, context)


async def _generate_statement(update: Update, context: ContextTypes.DEFAULT_TYPE):
    month = context.user_data["stmt_month"]
    year = context.user_data["stmt_year"]
    date_from = date(year, month, 1)
    last_day = monthrange(year, month)[1]
    date_to = date(year, month, last_day)

    await update.message.reply_text("⏳ Membuat PDF statement...")

    async with AsyncSessionLocal() as session:
        result = await session.execute(
            select(Transaction)
            .where(
                Transaction.is_deleted == False,
                Transaction.transaction_date >= date_from,
                Transaction.transaction_date <= date_to,
            )
            .order_by(Transaction.transaction_date)
        )
        txs = result.scalars().all()

        # Saldo awal = semua transaksi sebelum periode ini
        from bot.services.balance import get_summary
        pre_summary = await get_summary(session, date_to=date_from - timedelta(days=1))
        saldo_awal = pre_summary["saldo"]

    try:
        pdf_bytes = generate_statement_pdf(txs, date_from, date_to, saldo_awal)
        filename = f"statement_{year}_{month:02d}.pdf"
        await update.message.reply_document(
            document=io.BytesIO(pdf_bytes),
            filename=filename,
            caption=f"📄 E-Statement {date_from.strftime('%B %Y')}",
        )
    except Exception as e:
        logger.error(f"PDF generation failed: {e}")
        await update.message.reply_text("❌ Gagal membuat PDF. Coba lagi.")

    context.user_data.clear()
    return ConversationHandler.END


# ──────────────────────────────────────────────
# Cancel
# ──────────────────────────────────────────────

async def cmd_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    await update.message.reply_text("❌ Dibatalkan.")
    return ConversationHandler.END


# ──────────────────────────────────────────────
# Builders
# ──────────────────────────────────────────────

def build_laporan_conv() -> ConversationHandler:
    return ConversationHandler(
        entry_points=[CommandHandler("laporan", cmd_laporan)],
        states={
            LAPORAN_FROM: [MessageHandler(filters.TEXT & ~filters.COMMAND, laporan_from)],
            LAPORAN_TO: [MessageHandler(filters.TEXT & ~filters.COMMAND, laporan_to)],
        },
        fallbacks=[CommandHandler("batal", cmd_cancel)],
        conversation_timeout=300,
    )


def build_statement_conv() -> ConversationHandler:
    return ConversationHandler(
        entry_points=[CommandHandler("statement", cmd_statement)],
        states={
            STMT_BULAN: [MessageHandler(filters.TEXT & ~filters.COMMAND, stmt_bulan)],
            STMT_TAHUN: [MessageHandler(filters.TEXT & ~filters.COMMAND, stmt_tahun)],
        },
        fallbacks=[CommandHandler("batal", cmd_cancel)],
        conversation_timeout=300,
    )
