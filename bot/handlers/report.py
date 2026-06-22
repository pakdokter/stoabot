import re
"""
Report handlers:
  /laporan  — pilihan periode: hari ini, minggu ini, bulan ini, atau rentang tanggal
  /ringkas  — dashboard bulan ini
  /statement — PDF rekening koran
"""
import io
from calendar import monthrange
from datetime import date, timedelta

from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.ext import (
    ContextTypes, ConversationHandler, CommandHandler,
    MessageHandler, CallbackQueryHandler, filters,
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
    LAPORAN_MENU,
    LAPORAN_FROM, LAPORAN_TO,
    STMT_BULAN, STMT_TAHUN,
) = range(5)


# ── /ringkas ──────────────────────────────────────────────────────────

async def cmd_ringkas(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await ensure_registered(update, context):
        return

    user_id = update.effective_user.id
    today = date.today()
    first_day = date(today.year, today.month, 1)

    async with AsyncSessionLocal() as session:
        summary = await get_summary(session, user_id=user_id, date_from=first_day, date_to=today)

    days_passed = (today - first_day).days + 1
    avg_keluar = summary["total_keluar"] / days_passed if days_passed > 0 else 0

    db_user = context.user_data.get("db_user")
    user_name = db_user.full_name if db_user else update.effective_user.full_name or "User"
    user_name_safe = re.sub(r'([_*])', r'\\\1', user_name)

    await update.message.reply_text(
        f"📊 *Ringkasan Laporan Keuangan*\n"
        f"👤 {user_name_safe}\n" 
        f"_{fmt_date_full(first_day)} — {fmt_date_full(today)}_\n\n"
        f"Pemasukan:\n*{fmt_rupiah(summary['total_masuk'])}*\n\n"
        f"Pengeluaran:\n*{fmt_rupiah(summary['total_keluar'])}*\n\n"
        f"Laba Bersih:\n*{fmt_rupiah(summary['saldo'])}*\n\n"
        f"Jumlah Transaksi:\n*{summary['jumlah']}*\n\n"
        f"Rata-rata Pengeluaran Harian:\n*{fmt_rupiah(avg_keluar)}*",
        parse_mode="Markdown",
    )


# ── /laporan ──────────────────────────────────────────────────────────

async def cmd_laporan(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await ensure_registered(update, context):
        return ConversationHandler.END

    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("📅 Hari ini", callback_data="laporan:hari"),
            InlineKeyboardButton("📆 Minggu ini", callback_data="laporan:minggu"),
        ],
        [
            InlineKeyboardButton("🗓 Bulan ini", callback_data="laporan:bulan"),
            InlineKeyboardButton("✏️ Rentang tanggal", callback_data="laporan:custom"),
        ],
    ])

    await update.message.reply_text(
        "📊 *Laporan Transaksi*\n\nPilih periode:",
        parse_mode="Markdown",
        reply_markup=keyboard,
    )
    return LAPORAN_MENU


async def laporan_menu_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user_id = update.effective_user.id
    try:
        await query.answer()
    except Exception:
        pass

    today = date.today()
    choice = query.data.split(":")[1]

    if choice == "hari":
        date_from = today
        date_to = today
        return await _show_laporan(query, user_id, date_from, date_to)

    elif choice == "minggu":
        date_from = today - timedelta(days=today.weekday())  # Senin
        date_to = today
        return await _show_laporan(query, user_id, date_from, date_to)

    elif choice == "bulan":
        date_from = date(today.year, today.month, 1)
        date_to = today
        return await _show_laporan(query, user_id, date_from, date_to)

    elif choice == "custom":
        try:
            await query.edit_message_text(
                "📅 Masukkan tanggal mulai:\n_(Format: DD/MM/YYYY, contoh: 01/06/2026)_",
                parse_mode="Markdown",
            )
        except Exception:
            pass
        return LAPORAN_FROM

    return ConversationHandler.END


async def laporan_from(update: Update, context: ContextTypes.DEFAULT_TYPE):
    d = parse_date(update.message.text)
    if not d:
        await update.message.reply_text("❌ Format tidak valid. Contoh: 01/06/2026")
        return LAPORAN_FROM
    context.user_data["laporan_from"] = d
    await update.message.reply_text("Tanggal akhir? (DD/MM/YYYY)")
    return LAPORAN_TO


async def laporan_to(update: Update, context: ContextTypes.DEFAULT_TYPE):
    d = parse_date(update.message.text)
    if not d:
        await update.message.reply_text("❌ Format tidak valid.")
        return LAPORAN_TO

    date_from = context.user_data.get("laporan_from")
    date_to = d

    if not date_from:
        await update.message.reply_text("❌ Sesi habis. Coba /laporan lagi.")
        return ConversationHandler.END

    if date_to < date_from:
        await update.message.reply_text("❌ Tanggal akhir tidak boleh sebelum tanggal mulai.")
        return LAPORAN_TO

    user_id = update.effective_user.id
    return await _show_laporan(update, user_id, date_from, date_to, from_message=True)


async def _show_laporan(update_or_query, user_id: int, date_from: date, date_to: date, from_message: bool = False):
    """Tampilkan laporan untuk rentang tanggal tertentu."""
    async with AsyncSessionLocal() as session:
        summary = await get_summary(session, user_id=user_id, date_from=date_from, date_to=date_to)

        result = await session.execute(
            select(Transaction)
            .where(
                Transaction.is_deleted == False,
                Transaction.user_id == user_id,
                Transaction.transaction_date >= date_from,
                Transaction.transaction_date <= date_to,
            )
            .order_by(Transaction.transaction_date, Transaction.created_at)
            .limit(30)
        )
        txs = result.scalars().all()

    # Ambil nama user
    from bot.database import AsyncSessionLocal as _ASL
    from bot.models import User as _User
    async with _ASL() as _s:
        _u = await _s.get(_User, user_id)
        user_name = _u.full_name if _u else str(user_id)
    user_name_safe = re.sub(r'([_*])', r'\\\1', user_name)

    # Label periode
    if date_from == date_to:
        periode = f"📅 {fmt_date(date_from)}"
    else:
        periode = f"📅 {fmt_date(date_from)} s/d {fmt_date(date_to)}"

    lines = [
        f"📊 *Laporan Transaksi*\n"
        f"👤 {user_name_safe}\n"
        f"{periode}\n",
        f"Total Masuk: *{fmt_rupiah(summary['total_masuk'])}*",
        f"Total Keluar: *{fmt_rupiah(summary['total_keluar'])}*",
        f"Saldo Periode: *{fmt_rupiah(summary['saldo'])}*",
        f"Jumlah Transaksi: *{summary['jumlah']}*",
        "─────────────────",
    ]

    for tx in txs:
        sign = "+" if tx.type == "masuk" else "-"
        lines.append(f"*{fmt_date(tx.transaction_date)}* {sign}{fmt_rupiah(tx.amount)}\n_{tx.description}_")

    if not txs:
        lines.append("_(tidak ada transaksi dalam periode ini)_")

    msg = "\n".join(lines)

    try:
        if from_message:
            await update_or_query.message.reply_text(msg, parse_mode="Markdown")
        else:
            await update_or_query.edit_message_text(msg, parse_mode="Markdown")
    except Exception as e:
        logger.error(f"[LAPORAN] reply failed: {e}")
        try:
            await update_or_query.message.reply_text(msg, parse_mode="Markdown")
        except Exception:
            pass

    return ConversationHandler.END


# ── /statement ────────────────────────────────────────────────────────

async def cmd_statement(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await ensure_registered(update, context):
        return ConversationHandler.END

    today = date.today()

    # Buat tombol 6 bulan terakhir + opsi bulan lain
    buttons = []
    row = []
    for i in range(5, -1, -1):
        from dateutil.relativedelta import relativedelta
        d = today - relativedelta(months=i)
        label = d.strftime("%b %Y")
        cb = f"stmt:{d.month}:{d.year}"
        row.append(InlineKeyboardButton(label, callback_data=cb))
        if len(row) == 3:
            buttons.append(row)
            row = []
    if row:
        buttons.append(row)
    buttons.append([InlineKeyboardButton("✏️ Bulan lain", callback_data="stmt:other")])

    await update.message.reply_text(
        "📄 *E-Statement PDF*\n\nPilih bulan:",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(buttons),
    )
    return STMT_BULAN


async def stmt_bulan_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handler tombol pilihan bulan statement."""
    query = update.callback_query
    try:
        await query.answer()
    except Exception:
        pass

    data = query.data  # "stmt:6:2026" atau "stmt:other"
    if data == "stmt:other":
        try:
            await query.edit_message_text(
                "Ketik bulan dan tahun:\n_(contoh: 6/2026 atau 06/2026)_",
                parse_mode="Markdown",
            )
        except Exception:
            pass
        return STMT_TAHUN

    parts = data.split(":")
    month = int(parts[1])
    year = int(parts[2])
    context.user_data["stmt_month"] = month
    context.user_data["stmt_year"] = year
    return await _generate_statement(query, context)


async def stmt_bulan(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Input manual bulan/tahun — dipakai saat user klik Bulan lain."""
    text = update.message.text.strip()
    today = date.today()

    # Format: "6/2026" atau "6 2026" atau "06/2026"
    m = re.match(r'^(\d{1,2})[/\s](\d{4})$', text)
    if m:
        month = int(m.group(1))
        year = int(m.group(2))
        if 1 <= month <= 12 and 2000 <= year <= 2100:
            context.user_data["stmt_month"] = month
            context.user_data["stmt_year"] = year
            return await _generate_statement(update, context)

    await update.message.reply_text("❌ Format tidak valid. Contoh: 6/2026 atau 06/2026")
    return STMT_TAHUN


async def stmt_tahun(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Alias untuk stmt_bulan — dipakai di state STMT_TAHUN."""
    return await stmt_bulan(update, context)


async def _generate_statement(update_or_query, context: ContextTypes.DEFAULT_TYPE):
    month = context.user_data["stmt_month"]
    year = context.user_data["stmt_year"]
    # Support both Update (message) dan CallbackQuery
    if hasattr(update_or_query, 'effective_user'):
        user_id = update_or_query.effective_user.id
        reply_target = update_or_query.message
    else:
        user_id = update_or_query.from_user.id
        reply_target = update_or_query.message
    date_from = date(year, month, 1)
    last_day = monthrange(year, month)[1]
    date_to = date(year, month, last_day)

    await reply_target.reply_text("⏳ Membuat PDF statement...")

    async with AsyncSessionLocal() as session:
        result = await session.execute(
            select(Transaction)
            .where(
                Transaction.is_deleted == False,
                Transaction.user_id == user_id,
                Transaction.transaction_date >= date_from,
                Transaction.transaction_date <= date_to,
            )
            .order_by(Transaction.transaction_date)
        )
        txs = result.scalars().all()

        pre_summary = await get_summary(
            session, user_id=user_id,
            date_to=date_from - timedelta(days=1)
        )
        saldo_awal = pre_summary["saldo"]

    # Ambil nama user dulu sebelum generate PDF
    from bot.database import AsyncSessionLocal as _ASL
    from bot.models import User as _User
    async with _ASL() as _s:
        _u = await _s.get(_User, user_id)
        user_name = _u.full_name if _u else str(user_id)

    try:
        pdf_bytes = generate_statement_pdf(txs, date_from, date_to, saldo_awal, user_name=user_name)
        filename = f"statement_{year}_{month:02d}.pdf"

        await reply_target.reply_document(
            document=io.BytesIO(pdf_bytes),
            filename=filename,
            caption=f"📄 E-Statement {date_from.strftime('%B %Y')} — {user_name}",
        )
    except Exception as e:
        logger.error(f"PDF generation failed: {e}")
        await reply_target.reply_text("❌ Gagal membuat PDF. Coba lagi.")

    _p = {k: context.user_data[k] for k in ("session_verified", "db_user") if k in context.user_data}
    context.user_data.clear()
    context.user_data.update(_p)
    return ConversationHandler.END


# ── Cancel ────────────────────────────────────────────────────────────

async def cmd_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    _p = {k: context.user_data[k] for k in ("session_verified", "db_user") if k in context.user_data}
    context.user_data.clear()
    context.user_data.update(_p)
    await update.message.reply_text("❌ Dibatalkan.")
    return ConversationHandler.END


# ── Builders ──────────────────────────────────────────────────────────

def build_laporan_conv() -> ConversationHandler:
    return ConversationHandler(
        entry_points=[CommandHandler("laporan", cmd_laporan)],
        states={
            LAPORAN_MENU: [
                CallbackQueryHandler(laporan_menu_callback, pattern="^laporan:"),
            ],
            LAPORAN_FROM: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, laporan_from),
            ],
            LAPORAN_TO: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, laporan_to),
            ],
        },
        fallbacks=[CommandHandler("batal", cmd_cancel)],
        allow_reentry=True,
        conversation_timeout=300,
    )


def build_statement_conv() -> ConversationHandler:
    return ConversationHandler(
        entry_points=[CommandHandler("statement", cmd_statement)],
        states={
            STMT_BULAN: [
                CallbackQueryHandler(stmt_bulan_callback, pattern="^stmt:"),
                MessageHandler(filters.TEXT & ~filters.COMMAND, stmt_bulan),
            ],
            STMT_TAHUN: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, stmt_tahun),
            ],
        },
        fallbacks=[CommandHandler("batal", cmd_cancel)],
        allow_reentry=True,
        conversation_timeout=300,
    )
