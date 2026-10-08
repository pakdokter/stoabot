"""
Rekap handler — kumpulkan catatan/nota yang di-forward, lalu keluarkan buku kas 9 kolom.

  /rekap_mulai   — mulai sesi pengumpulan (per chat/grup)
  (forward teks/foto nota ke chat)
  /rekap_status  — lihat yang sudah terkumpul
  /rekap_selesai — simpan ke database + kirim XLSX & PDF
  /rekap_batal   — buang sesi tanpa menyimpan

Catatan grup: matikan Privacy Mode bot di @BotFather (/setprivacy → Disable)
atau jadikan bot admin grup, agar bot menerima pesan forward.
"""
import io
from datetime import date, datetime
from zoneinfo import ZoneInfo

from loguru import logger
from telegram import Update
from telegram.ext import (
    ApplicationHandlerStop, CommandHandler, ContextTypes, MessageHandler, filters,
)

from bot.config import settings
from bot.database import AsyncSessionLocal
from bot.handlers.auth import ensure_registered
from bot.models import Attachment, Transaction
from bot.services.audit import log_create
from bot.services.balance import get_running_balance
from bot.services.ocr_service import process_receipt
from bot.services import rekap_buku_kas
from bot.services.rekap_service import RekapEntry, guess_category, parse_note_text
from bot.utils.formatters import fmt_date, fmt_rupiah

KEY = "rekap_session"


def _forward_info(msg) -> tuple[date, str]:
    """Tanggal & nama pengirim asli dari pesan forward (fallback: hari ini / pengirim)."""
    tz = ZoneInfo(settings.timezone)
    origin = getattr(msg, "forward_origin", None)
    when = getattr(origin, "date", None) or msg.date
    tgl = when.astimezone(tz).date() if when else datetime.now(tz).date()
    name = ""
    if origin is not None:
        name = (
            getattr(getattr(origin, "sender_user", None), "full_name", None)
            or getattr(origin, "sender_user_name", None)
            or getattr(getattr(origin, "chat", None), "title", None)
            or ""
        )
    return tgl, name or (msg.from_user.full_name if msg.from_user else "")


async def cmd_rekap_mulai(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await ensure_registered(update, context):
        return
    context.chat_data[KEY] = {"entries": [], "skipped": [], "owner": update.effective_user.id}
    await update.message.reply_text(
        "📒 *Sesi rekap dimulai.*\n\n"
        "Forward catatan / foto nota ke chat ini.\n"
        "Format teks: satu baris = satu transaksi, contoh:\n"
        "`beli kopi 150rb`\n`gaji barista Rp 1.500.000`\n`+ penjualan hari ini 2jt` (awalan + = masuk)\n\n"
        "/rekap\\_status — cek progres\n/rekap\\_selesai — simpan & buat rekap\n/rekap\\_batal — batalkan",
        parse_mode="Markdown",
    )


async def collect_forward(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Group -1: tangkap forward saat sesi aktif; stop agar OCR handler biasa tidak ikut jalan."""
    sess = context.chat_data.get(KEY)
    msg = update.effective_message
    if not sess or not msg or not getattr(msg, "forward_origin", None):
        return
    if not await ensure_registered(update, context):
        raise ApplicationHandlerStop

    tgl, sumber = _forward_info(msg)
    text = msg.text or msg.caption or ""
    entries, skipped = parse_note_text(text, tgl, sumber)
    note = ""

    if msg.photo:
        file_id = msg.photo[-1].file_id
        if entries:
            entries[0].file_id = file_id
        else:
            try:
                res = await process_receipt(context.bot, file_id)
            except Exception as e:
                logger.exception(f"[REKAP] OCR gagal: {e}")
                res = None
            if res and res.total:
                merchant = res.merchant or "Nota belanja"
                uraian = merchant + (f" ({len(res.items)} item)" if res.items else "")
                entries.append(RekapEntry(
                    tanggal=res.tx_date or tgl, type="keluar", amount=res.total,
                    uraian=uraian, kategori=guess_category(uraian, "keluar"), sumber=sumber,
                    file_id=file_id, ocr_raw_text=res.raw_text, ocr_confidence=res.confidence,
                ))
            else:
                skipped.append("[foto nota — total tidak terbaca]")

    sess["entries"].extend(entries)
    sess["skipped"].extend(skipped)

    lines = [f"✅ {len(entries)} dicatat"] if entries else []
    for e in entries:
        lines.append(f"• {fmt_date(e.tanggal)} {'➕' if e.type == 'masuk' else '➖'} "
                     f"{e.uraian} — {fmt_rupiah(e.amount)}")
    if skipped:
        lines.append(f"⚠️ {len(skipped)} baris dilewati (nominal tidak terbaca):")
        lines.extend(f"  ◦ {s[:60]}" for s in skipped[:5])
    await msg.reply_text("\n".join(lines) or "⚠️ Tidak ada yang bisa dicatat.", quote=True)
    raise ApplicationHandlerStop


async def cmd_rekap_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    sess = context.chat_data.get(KEY)
    if not sess:
        await update.message.reply_text("Tidak ada sesi rekap. Mulai dengan /rekap_mulai")
        return
    ent = sess["entries"]
    masuk = sum(e.amount for e in ent if e.type == "masuk")
    keluar = sum(e.amount for e in ent if e.type == "keluar")
    await update.message.reply_text(
        f"📒 Sesi rekap aktif\nTerkumpul: *{len(ent)}* transaksi\n"
        f"Masuk: *{fmt_rupiah(masuk)}*\nKeluar: *{fmt_rupiah(keluar)}*\n"
        f"Dilewati: {len(sess['skipped'])}",
        parse_mode="Markdown",
    )


async def cmd_rekap_batal(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if context.chat_data.pop(KEY, None) is None:
        await update.message.reply_text("Tidak ada sesi rekap aktif.")
        return
    await update.message.reply_text("❌ Sesi rekap dibatalkan, tidak ada yang disimpan.")


async def cmd_rekap_selesai(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await ensure_registered(update, context):
        return
    sess = context.chat_data.get(KEY)
    if not sess:
        await update.message.reply_text("Tidak ada sesi rekap. Mulai dengan /rekap_mulai")
        return
    entries: list[RekapEntry] = sess["entries"]
    if not entries:
        await update.message.reply_text("Belum ada catatan terkumpul. /rekap_batal untuk membatalkan.")
        return

    user_id = update.effective_user.id
    await update.message.reply_text("⏳ Menyimpan & menyusun rekap...")
    try:
        async with AsyncSessionLocal() as session:
            saldo_awal = await get_running_balance(session)
            for e in entries:
                tx = Transaction(
                    user_id=user_id, type=e.type, amount=e.amount, description=e.uraian,
                    category=e.kategori, transaction_date=e.tanggal,
                )
                session.add(tx)
                await session.flush()
                if e.file_id:
                    session.add(Attachment(
                        transaction_id=tx.id, telegram_file_id=e.file_id,
                        ocr_raw_text=e.ocr_raw_text, ocr_confidence=e.ocr_confidence,
                    ))
                await log_create(session, user_id, tx)
            await session.commit()
    except Exception as e:
        logger.exception(f"[REKAP] simpan gagal: {e}")
        await update.message.reply_text("❌ Gagal menyimpan. Sesi tetap aktif, coba /rekap_selesai lagi.")
        return

    rows = rekap_buku_kas.build_rows(entries, saldo_awal)
    d, k, akhir = rekap_buku_kas.totals(rows)
    stamp = date.today().strftime("%Y%m%d")
    await update.message.reply_document(
        io.BytesIO(rekap_buku_kas.generate_xlsx(rows)), filename=f"rekap_kas_{stamp}.xlsx",
        caption=(f"📒 Rekap {len(entries)} transaksi\nDebit (masuk): {fmt_rupiah(d)}\n"
                 f"Kredit (keluar): {fmt_rupiah(k)}\nSaldo akhir: {fmt_rupiah(akhir)}"),
    )
    await update.message.reply_document(io.BytesIO(rekap_buku_kas.generate_pdf(rows)), filename=f"rekap_kas_{stamp}.pdf")
    if sess["skipped"]:
        await update.message.reply_text(
            f"⚠️ {len(sess['skipped'])} baris dilewati. Catat manual lewat /masuk atau /keluar."
        )
    context.chat_data.pop(KEY, None)


def register(app):
    fwd = filters.FORWARDED & (filters.TEXT | filters.PHOTO) & ~filters.COMMAND
    app.add_handler(MessageHandler(fwd, collect_forward), group=-1)
    app.add_handler(CommandHandler("rekap_mulai", cmd_rekap_mulai))
    app.add_handler(CommandHandler("rekap_status", cmd_rekap_status))
    app.add_handler(CommandHandler("rekap_selesai", cmd_rekap_selesai))
    app.add_handler(CommandHandler("rekap_batal", cmd_rekap_batal))
