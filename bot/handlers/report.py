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
from bot.models import Transaction, User
from bot.config import settings
from bot.services.balance import get_summary
from bot.services.pdf_service import generate_statement_pdf
from bot.utils.formatters import fmt_rupiah, fmt_date, fmt_date_full, parse_date
from bot.handlers.auth import ensure_registered

# States
(
    LAPORAN_MENU,
    LAPORAN_FROM, LAPORAN_TO,
    STMT_BULAN, STMT_TAHUN,
    LPTEKS_INPUT,
) = range(6)


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

    user_id = update.effective_user.id
    is_admin = user_id in settings.admin_ids

    # Admin: tampilkan pilihan user dulu
    if is_admin:
        async with AsyncSessionLocal() as session:
            from sqlalchemy import select as _sel
            result = await session.execute(
                _sel(User).where(User.is_active == True).order_by(User.full_name)
            )
            all_users = result.scalars().all()

        rows = [[InlineKeyboardButton(
            f"👤 Semua User", callback_data="stmt_user:all"
        )]]
        for u in all_users:
            rows.append([InlineKeyboardButton(
                f"{'👤' if u.id in settings.admin_ids else '🧑'} {u.full_name}",
                callback_data=f"stmt_user:{u.id}"
            )])

        await update.message.reply_text(
            "📄 *E-Statement PDF*\n\nPilih user:",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(rows),
        )
        return STMT_BULAN

    # Staff biasa: langsung ke pilihan bulan (hanya data sendiri)
    context.user_data["stmt_target_user_id"] = user_id
    return await _show_month_picker(update.message, context)


async def _show_month_picker(msg_or_reply, context):
    """Tampilkan picker bulan untuk statement."""
    from dateutil.relativedelta import relativedelta
    today = date.today()
    buttons = []
    row = []
    for i in range(5, -1, -1):
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

    target_id = context.user_data.get("stmt_target_user_id")
    target_name = context.user_data.get("stmt_target_name", "")
    header = f"📄 *E-Statement PDF*"
    if target_name and target_name != "all":
        header += f"\n👤 {target_name}"
    elif target_name == "all":
        header += "\n👥 Semua User"
    header += "\n\nPilih bulan:"

    kwargs = dict(text=header, parse_mode="Markdown",
                  reply_markup=InlineKeyboardMarkup(buttons))
    if hasattr(msg_or_reply, 'edit_message_text'):
        await msg_or_reply.edit_message_text(**kwargs)
    else:
        await msg_or_reply.reply_text(**kwargs)
    return STMT_BULAN


async def stmt_user_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin memilih user untuk statement."""
    query = update.callback_query
    await query.answer()

    data = query.data  # "stmt_user:all" atau "stmt_user:123456789"
    target = data.split(":")[1]

    if target == "all":
        context.user_data["stmt_target_user_id"] = "all"
        context.user_data["stmt_target_name"] = "all"
    else:
        target_id = int(target)
        context.user_data["stmt_target_user_id"] = target_id
        async with AsyncSessionLocal() as session:
            u = await session.get(User, target_id)
            context.user_data["stmt_target_name"] = u.full_name if u else str(target_id)

    return await _show_month_picker(query, context)


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


async def _generate_statement(update_or_query, context):
    month = context.user_data["stmt_month"]
    year = context.user_data["stmt_year"]
    target_user_id = context.user_data.get("stmt_target_user_id")

    if hasattr(update_or_query, "effective_user"):
        caller_id = update_or_query.effective_user.id
        reply_target = update_or_query.message
    else:
        caller_id = update_or_query.from_user.id
        reply_target = update_or_query.message

    if not target_user_id:
        target_user_id = caller_id

    date_from = date(year, month, 1)
    last_day = monthrange(year, month)[1]
    date_to = date(year, month, last_day)

    if target_user_id == "all":
        await reply_target.reply_text("⏳ Membuat PDF untuk semua user...")
        async with AsyncSessionLocal() as session:
            result = await session.execute(
                select(User).where(User.is_active == True).order_by(User.full_name)
            )
            all_users = result.scalars().all()
        for u in all_users:
            await _generate_single_statement(reply_target, u.id, u.full_name,
                                              date_from, date_to, month, year)
        return ConversationHandler.END

    await reply_target.reply_text("⏳ Membuat PDF statement...")
    async with AsyncSessionLocal() as session:
        _u = await session.get(User, target_user_id)
        user_name = _u.full_name if _u else str(target_user_id)
    await _generate_single_statement(reply_target, target_user_id, user_name,
                                     date_from, date_to, month, year)
    return ConversationHandler.END


async def _generate_single_statement(reply_target, user_id, user_name,
                                      date_from, date_to, month, year):
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
        pre_summary = await get_summary(session, user_id=user_id,
                                        date_to=date_from - timedelta(days=1))
        saldo_awal = pre_summary["saldo"]

    if not txs:
        await reply_target.reply_text(
            f"\U0001f4ed *{user_name}*: Tidak ada transaksi di "
            f"{date_from.strftime('%B %Y')}.",
            parse_mode="Markdown",
        )
        return

    try:
        pdf_bytes = generate_statement_pdf(txs, date_from, date_to, saldo_awal, user_name=user_name)
        filename = f"statement_{user_name.replace(' ', '_')}_{year}_{month:02d}.pdf"
        await reply_target.reply_document(
            document=io.BytesIO(pdf_bytes),
            filename=filename,
            caption=f"\U0001f4c4 E-Statement {date_from.strftime('%B %Y')} — {user_name}",
        )
    except Exception as e:
        logger.error(f"PDF generation failed for {user_name}: {e}")
        await reply_target.reply_text(f"❌ Gagal generate PDF untuk {user_name}: {e}")


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
                CallbackQueryHandler(stmt_user_callback, pattern="^stmt_user:"),
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


# ── /laporan_teks — parser laporan belanja harian teks staff ──────────────────

async def cmd_laporan_teks(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /laporan_teks — kirim laporan belanja harian format teks, bot parse dan simpan.

    Format yang diterima:
      *DD Bulan
      • Toko - Nominal
      • Uang Masuk - Nominal
      • Belanja Pasar
      -item - harga
      =total
      (total_harian)
    """
    from bot.services.report_parser import parse_report_text
    from bot.models import Transaction, MarketItem
    from bot.services.balance import get_running_balance
    from bot.services.sheets import append_transaction as sheets_append
    from bot.services.audit import log_create
    import re

    if not await ensure_registered(update, context):
        return

    user_id = update.effective_user.id

    # Ambil teks dari pesan (setelah /laporan_teks)
    text = update.message.text or ""
    # Hapus command prefix
    text = re.sub(r'^/laporan_teks\s*', '', text, flags=re.IGNORECASE).strip()

    if not text:
        await update.message.reply_text(
            "📋 *Cara pakai /laporan\\_teks*\n\n"
            "Kirim laporan belanja harian langsung setelah command:\n\n"
            "`/laporan_teks`\n"
            "`*10 Juni`\n"
            "`• Uang masuk - 1.000.000`\n"
            "`• Dinda - 185.000`\n"
            "`• Belanja Pasar`\n"
            "`-Tomat 1kg - 8.000`\n"
            "`=8.000`\n\n"
            "Bot akan parse dan simpan semua transaksi sekaligus.",
            parse_mode="Markdown",
        )
        return

    # Parse
    result = parse_report_text(text)

    if not result.transactions:
        msg = "❌ Tidak ada transaksi yang bisa dibaca.\n\n"
        if result.errors:
            msg += "Baris bermasalah:\n"
            for e in result.errors[:5]:
                msg += f"  • {e}\n"
        await update.message.reply_text(msg)
        return

    # Preview dulu sebelum simpan
    from bot.utils.formatters import fmt_rupiah, fmt_date

    def _esc_local(t): return str(t).replace('_', r'\_').replace('*', r'\*')

    lines = [f"📋 *Preview Laporan* ({len(result.transactions)} transaksi)\n"]

    current_date = None
    for tx in result.transactions:
        if tx.tx_date != current_date:
            current_date = tx.tx_date
            lines.append(f"\n📅 *{fmt_date(tx.tx_date)}*")
        sym = "➕" if tx.tx_type == 'masuk' else "➖"
        lines.append(f"  {sym} {_esc_local(tx.description)} — *{fmt_rupiah(tx.amount)}*")

    lines.append(f"\n💰 Total masuk : *{fmt_rupiah(result.total_masuk)}*")
    lines.append(f"💸 Total keluar: *{fmt_rupiah(result.total_keluar)}*")

    if result.errors:
        lines.append(f"\n⚠️ {len(result.errors)} baris tidak terbaca:")
        for e in result.errors[:3]:
            lines.append(f"  • {_esc_local(e)}")

    if result.skipped_days:
        lines.append(f"\n📌 Hari tidak belanja: {len(result.skipped_days)} hari")

    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Simpan semua", callback_data="lpteks:simpan"),
        InlineKeyboardButton("❌ Batal", callback_data="lpteks:batal"),
    ]])

    # Simpan result di context untuk callback
    context.user_data["lpteks_result"] = result
    context.user_data["lpteks_user_id"] = user_id

    await update.message.reply_text(
        "\n".join(lines),
        parse_mode="Markdown",
        reply_markup=keyboard,
    )


async def handle_lpteks_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle konfirmasi simpan/batal laporan teks."""
    from bot.services.report_parser import ParseResult
    from bot.models import Transaction, MarketItem
    from bot.services.balance import get_running_balance
    from bot.services.sheets import append_transaction as sheets_append
    from bot.services.audit import log_create
    from sqlalchemy import func as sqlfunc, select

    query = update.callback_query
    await query.answer()
    data = query.data

    if data == "lpteks:batal":
        await query.edit_message_text("❌ Dibatalkan.")
        return

    if data != "lpteks:simpan":
        return

    result: ParseResult = context.user_data.get("lpteks_result")
    user_id: int = context.user_data.get("lpteks_user_id", update.effective_user.id)

    if not result or not result.transactions:
        await query.edit_message_text("❌ Data tidak ditemukan. Kirim ulang.")
        return

    await query.edit_message_text("⏳ Menyimpan transaksi...")

    from bot.utils.formatters import fmt_rupiah, fmt_date
    saved = 0
    failed = 0

    try:
        async with AsyncSessionLocal() as session:
            for tx_data in result.transactions:
                try:
                    tx = Transaction(
                        user_id=user_id,
                        type=tx_data.tx_type,
                        amount=tx_data.amount,
                        description=tx_data.description[:200],
                        category=tx_data.category or None,
                        transaction_date=tx_data.tx_date,
                    )
                    session.add(tx)
                    await session.flush()
                    await log_create(session, user_id, tx)

                    # Update katalog market_items jika pasar
                    if tx_data.category == 'pasar':
                        item_name = re.sub(r'^Pasar\s*-\s*', '', tx_data.description).strip()
                        existing = await session.execute(
                            select(MarketItem).where(
                                sqlfunc.lower(MarketItem.name) == item_name.lower()
                            )
                        )
                        cat_item = existing.scalar_one_or_none()
                        if cat_item:
                            cat_item.use_count += 1
                            cat_item.last_price = tx_data.amount
                            cat_item.last_used = tx_data.tx_date
                        else:
                            session.add(MarketItem(
                                name=item_name.title(),
                                last_price=tx_data.amount,
                                use_count=1,
                                last_used=tx_data.tx_date,
                            ))

                    saved += 1
                except Exception as e:
                    logger.error(f"[LPTEKS] failed tx: {e}")
                    failed += 1

            await session.commit()
            saldo = await get_running_balance(session, user_id)

        # Google Sheets
        for tx_data in result.transactions:
            try:
                await sheets_append(
                    user_id=user_id,
                    user_name=update.effective_user.full_name or "",
                    tx_type=tx_data.tx_type,
                    amount=tx_data.amount,
                    description=tx_data.description,
                    tx_date=tx_data.tx_date,
                )
            except Exception as e:
                logger.warning(f"[LPTEKS] sheets: {e}")

        msg = (
            f"✅ *{saved} transaksi berhasil disimpan*"
            + (f"\n❌ {failed} gagal" if failed else "")
            + f"\n\n💰 Total masuk : *{fmt_rupiah(result.total_masuk)}*"
            + f"\n💸 Total keluar: *{fmt_rupiah(result.total_keluar)}*"
            + f"\n\n💳 Saldo: *{fmt_rupiah(saldo)}*"
        )
        try:
            await query.edit_message_text(msg, parse_mode="Markdown")
        except Exception:
            await query.message.reply_text(msg.replace('*','').replace('_',''))

    except Exception as e:
        logger.exception(f"[LPTEKS] save error: {e}")
        await query.edit_message_text(f"❌ Error: {e}")




# ── /laporan_teks — parser laporan belanja harian teks staff ──────────────────

def _esc_md(t: str) -> str:
    return str(t).replace('_', r'\_').replace('*', r'\*').replace('`', r'\`').replace('[', r'\[')


async def cmd_laporan_teks(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/laporan_teks — entry point, minta user paste laporan."""
    if not await ensure_registered(update, context):
        return ConversationHandler.END

    await update.message.reply_text(
        "📋 *Rekap Laporan Teks*\n\n"
        "Paste laporan belanja harianmu di pesan berikutnya.\n\n"
        "Format yang diterima:\n"
        "`*10 Juni`\n"
        "`• Uang masuk - 1.000.000`\n"
        "`• Dinda - 185.000`\n"
        "`• Belanja Pasar`\n"
        "`-Tomat 1kg - 8.000`\n"
        "`=8.000`\n\n"
        "Atau ketik /batal untuk keluar.",
        parse_mode="Markdown",
    )
    return LPTEKS_INPUT


async def handle_lpteks_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Terima teks laporan, parse, tampilkan preview."""
    from bot.services.report_parser import parse_report_text

    text = (update.message.text or "").strip()
    if not text:
        await update.message.reply_text("❌ Teks kosong. Coba lagi atau /batal.")
        return LPTEKS_INPUT

    result = parse_report_text(text)

    if not result.transactions:
        msg = "❌ Tidak ada transaksi yang bisa dibaca.\n\n"
        if result.errors:
            msg += "Baris bermasalah:\n"
            for e in result.errors[:5]:
                msg += f"  • {e}\n"
        msg += "\nCoba lagi atau /batal."
        await update.message.reply_text(msg)
        return LPTEKS_INPUT

    # Preview
    lines = [f"📋 *Preview* ({len(result.transactions)} transaksi)\n"]
    current_date = None
    for tx in result.transactions:
        if tx.tx_date != current_date:
            current_date = tx.tx_date
            lines.append(f"\n📅 *{fmt_date(tx.tx_date)}*")
        sym = "➕" if tx.tx_type == 'masuk' else "➖"
        lines.append(f"  {sym} {_esc_md(tx.description)} — *{fmt_rupiah(tx.amount)}*")

    lines.append(f"\n💰 Masuk : *{fmt_rupiah(result.total_masuk)}*")
    lines.append(f"💸 Keluar: *{fmt_rupiah(result.total_keluar)}*")

    if result.errors:
        lines.append(f"\n⚠️ {len(result.errors)} baris tidak terbaca:")
        for e in result.errors[:3]:
            lines.append(f"  • {_esc_md(e)}")

    if result.skipped_days:
        lines.append(f"\n📌 {len(result.skipped_days)} hari tidak belanja")

    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Simpan semua", callback_data="lpteks:simpan"),
        InlineKeyboardButton("✏️ Ulangi", callback_data="lpteks:ulangi"),
        InlineKeyboardButton("❌ Batal", callback_data="lpteks:batal"),
    ]])

    context.user_data["lpteks_result"] = result
    context.user_data["lpteks_user_id"] = update.effective_user.id

    await update.message.reply_text(
        "\n".join(lines),
        parse_mode="Markdown",
        reply_markup=keyboard,
    )
    return LPTEKS_INPUT


async def handle_lpteks_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle konfirmasi simpan/ulangi/batal."""
    from bot.services.report_parser import ParseResult
    from bot.models import Transaction, MarketItem
    from bot.services.balance import get_running_balance
    from bot.services.sheets import append_transaction as sheets_append
    from bot.services.audit import log_create
    from sqlalchemy import func as sqlfunc, select

    query = update.callback_query
    await query.answer()
    data = query.data

    if data == "lpteks:batal":
        await query.edit_message_text("❌ Dibatalkan.")
        return ConversationHandler.END

    if data == "lpteks:ulangi":
        await query.edit_message_text(
            "✏️ Paste ulang laporan belanjamu:"
        )
        return LPTEKS_INPUT

    if data != "lpteks:simpan":
        return LPTEKS_INPUT

    result: ParseResult = context.user_data.get("lpteks_result")
    user_id: int = context.user_data.get("lpteks_user_id", update.effective_user.id)

    if not result or not result.transactions:
        await query.edit_message_text("❌ Data tidak ditemukan. Kirim ulang /laporan_teks.")
        return ConversationHandler.END

    await query.edit_message_text("⏳ Menyimpan...")

    saved = 0
    failed = 0

    try:
        async with AsyncSessionLocal() as session:
            for tx_data in result.transactions:
                try:
                    tx = Transaction(
                        user_id=user_id,
                        type=tx_data.tx_type,
                        amount=tx_data.amount,
                        description=tx_data.description[:200],
                        category=tx_data.category or None,
                        transaction_date=tx_data.tx_date,
                    )
                    session.add(tx)
                    await session.flush()
                    await log_create(session, user_id, tx)

                    # Update katalog market_items jika pasar
                    if tx_data.category == 'pasar':
                        item_name = re.sub(r'^Pasar\s*-\s*', '', tx_data.description).strip()
                        existing = await session.execute(
                            select(MarketItem).where(
                                sqlfunc.lower(MarketItem.name) == item_name.lower()
                            )
                        )
                        cat_item = existing.scalar_one_or_none()
                        if cat_item:
                            cat_item.use_count += 1
                            cat_item.last_price = tx_data.amount
                            cat_item.last_used = tx_data.tx_date
                        else:
                            session.add(MarketItem(
                                name=item_name.title(),
                                last_price=tx_data.amount,
                                use_count=1,
                                last_used=tx_data.tx_date,
                            ))

                    saved += 1
                except Exception as e:
                    logger.error(f"[LPTEKS] tx failed: {e}")
                    failed += 1

            await session.commit()
            saldo = await get_running_balance(session, user_id)

        # Google Sheets
        for tx_data in result.transactions:
            try:
                await sheets_append(
                    user_id=user_id,
                    user_name=update.effective_user.full_name or "",
                    tx_type=tx_data.tx_type,
                    amount=tx_data.amount,
                    description=tx_data.description,
                    tx_date=tx_data.tx_date,
                )
            except Exception as e:
                logger.warning(f"[LPTEKS] sheets: {e}")

        msg = (
            f"✅ *{saved} transaksi tersimpan*"
            + (f"\n❌ {failed} gagal" if failed else "")
            + f"\n\n💰 Masuk : *{fmt_rupiah(result.total_masuk)}*"
            + f"\n💸 Keluar: *{fmt_rupiah(result.total_keluar)}*"
            + f"\n\n💳 Saldo: *{fmt_rupiah(saldo)}*"
        )
        try:
            await query.edit_message_text(msg, parse_mode="Markdown")
        except Exception:
            await query.message.reply_text(msg.replace('*', '').replace('_', ''))

    except Exception as e:
        logger.exception(f"[LPTEKS] save error: {e}")
        await query.edit_message_text(f"❌ Error: {e}")

    return ConversationHandler.END


def build_laporan_teks_conv() -> ConversationHandler:
    return ConversationHandler(
        entry_points=[CommandHandler("laporan_teks", cmd_laporan_teks)],
        states={
            LPTEKS_INPUT: [
                CallbackQueryHandler(handle_lpteks_callback, pattern="^lpteks:"),
                MessageHandler(filters.TEXT & ~filters.COMMAND, handle_lpteks_input),
            ],
        },
        fallbacks=[CommandHandler("batal", lambda u, c: ConversationHandler.END)],
        allow_reentry=True,
        conversation_timeout=600,
    )
