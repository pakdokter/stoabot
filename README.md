# stoabot — Telegram Bot Keuangan UMKM

Bot Telegram untuk pencatatan keuangan harian coffee shop / UMKM.
Dibangun untuk **Stoa Space**, Selong, Lombok.

---

## Arsitektur Sistem

```
Telegram API
     │
     ▼
python-telegram-bot (polling)
     │
     ├── ConversationHandlers (masuk/keluar/edit/hapus/laporan/statement)
     ├── MessageHandler (foto → OCR pipeline)
     └── CommandHandlers (saldo/riwayat/cari/ringkas)
          │
          ├── Services
          │    ├── balance.py   — kalkulasi saldo real-time (SQL aggregate)
          │    ├── audit.py     — log setiap perubahan ke audit_logs
          │    ├── ocr_service  — Tesseract / Google Vision pipeline
          │    └── pdf_service  — ReportLab PDF statement
          │
          └── PostgreSQL (via SQLAlchemy async)
               ├── users
               ├── transactions
               ├── attachments
               └── audit_logs
```

---

## Instalasi di VPS Ubuntu 22.04

### 1. Persiapan server

```bash
sudo apt update && sudo apt upgrade -y
sudo apt install -y docker.io docker-compose-plugin git
sudo systemctl enable --now docker
sudo usermod -aG docker $USER
```

### 2. Clone & konfigurasi

```bash
git clone https://github.com/yourrepo/stoabot.git
cd stoabot
cp .env.example .env
nano .env
```

Isi minimal di `.env`:
```
TELEGRAM_BOT_TOKEN=xxxxxxx:xxxxxxxx
TELEGRAM_ADMIN_IDS=123456789
DATABASE_URL=postgresql+asyncpg://stoabot:stoabot_secret@db:5432/stoabot
DATABASE_URL_SYNC=postgresql://stoabot:stoabot_secret@db:5432/stoabot
DB_PASSWORD=stoabot_secret
BUSINESS_NAME=Stoa Space
BUSINESS_ADDRESS=Jl. Pattimura 107, Selong, Lombok
```

### 3. Jalankan

```bash
docker compose up -d --build
docker compose logs -f bot
```

### 4. Verifikasi

```bash
docker compose ps
# Semua service harus "healthy"
```

---

## Mendapatkan Telegram Bot Token

1. Buka Telegram, cari **@BotFather**
2. Kirim `/newbot`
3. Ikuti instruksi, simpan token
4. Kirim `/setcommands` ke BotFather (opsional, bot set otomatis)

## Mendapatkan Telegram User ID

1. Cari **@userinfobot** di Telegram
2. Kirim pesan apapun, bot akan balas dengan ID Anda
3. Masukkan ID ke `TELEGRAM_ADMIN_IDS` di `.env`

---

## Pertama Kali Gunakan

1. Buka bot di Telegram
2. Kirim `/start`
3. Bot otomatis mendaftarkan Anda karena ID ada di `TELEGRAM_ADMIN_IDS`
4. Tambah staff: `/adduser 987654321 Nama Staff staff`

---

## Contoh Percakapan Lengkap

### Catat pemasukan
```
Anda: /masuk
Bot:  💰 Catat Pemasukan
      Nominal?
Anda: 150000
Bot:  Keterangan?
Anda: Penjualan kopi
Bot:  Tanggal? (kosongkan jika hari ini)
Anda: [enter / kosong]
Bot:  ✅ Transaksi berhasil disimpan
      Jenis: ➕ MASUK
      Nominal: Rp150.000
      Keterangan: Penjualan kopi
      💰 Saldo saat ini: Rp4.350.000
```

### Foto struk
```
Anda: [kirim foto struk Indomaret]
Bot:  🔍 Memproses struk...
      📄 Hasil Baca Struk ✅
      Toko: Indomaret
      Tanggal: 15 Jul 2026
      Total: Rp87.500
      [Ya] [Edit] [Tidak]
Anda: [tap Ya]
Bot:  ✅ Transaksi berhasil disimpan...
      💰 Saldo saat ini: Rp4.262.500
```

### Laporan periode
```
Anda: /laporan
Bot:  📅 Tanggal mulai?
Anda: 01/07/2026
Bot:  Tanggal akhir?
Anda: 31/07/2026
Bot:  📊 Laporan 01 Jul 2026 s/d 31 Jul 2026
      Total Masuk: Rp8.500.000
      Total Keluar: Rp4.237.500
      Saldo Periode: Rp4.262.500
      ...
```

### E-Statement PDF
```
Anda: /statement
Bot:  Bulan?
Anda: 7
Bot:  Tahun?
Anda: 2026
Bot:  ⏳ Membuat PDF statement...
      [kirim file: statement_2026_07.pdf]
```

---

## OCR Provider

### Tesseract (default, gratis)
- Sudah terinstall di Docker image
- Cocok untuk struk dengan teks jelas
- Set: `OCR_PROVIDER=tesseract`

### Google Cloud Vision (lebih akurat)
1. Buat project di [console.cloud.google.com](https://console.cloud.google.com)
2. Enable Cloud Vision API
3. Buat Service Account, download JSON key
4. Simpan ke `./credentials/google-vision.json`
5. Set di `.env`: `OCR_PROVIDER=google`

---

## Menjalankan Tests

```bash
# Install dependencies lokal
pip install -r requirements.txt

# Jalankan tests
pytest tests/ -v

# Dengan coverage
pytest tests/ -v --cov=bot --cov-report=term-missing
```

---

## Backup Database

```bash
# Backup
docker exec stoabot-db pg_dump -U stoabot stoabot > backup_$(date +%Y%m%d).sql

# Restore
docker exec -i stoabot-db psql -U stoabot stoabot < backup_20260715.sql
```

---

## Update Bot

```bash
git pull
docker compose up -d --build
```

---

## Struktur Database (ERD)

```
users (id PK, full_name, role, is_active)
  │
  ├──< transactions (id PK, user_id FK, type, amount, description,
  │                  transaction_date, is_deleted, deleted_at)
  │         │
  │         └──< attachments (id PK, transaction_id FK,
  │                           telegram_file_id, ocr_raw_text, ocr_confidence)
  │
  └──< audit_logs (id PK, user_id FK, action, table_name,
                   record_id, old_values JSONB, new_values JSONB)
```

---

## Troubleshooting

| Masalah | Solusi |
|---------|--------|
| Bot tidak merespons | Cek `docker compose logs bot` |
| Database connection error | Tunggu DB healthy, cek DATABASE_URL |
| OCR tidak akurat | Foto lebih terang/dekat, atau ganti ke Google Vision |
| PDF tidak terkirim | Cek storage Telegram (max 50MB, struk aman) |
| User tidak bisa akses | Tambah via `/adduser` atau cek `TELEGRAM_ADMIN_IDS` |
