-- Postgres induk Stoa: schema per aplikasi + schema shared.
CREATE SCHEMA IF NOT EXISTS shared;
CREATE SCHEMA IF NOT EXISTS stoa;      -- stoabot (users, transactions, ...)
CREATE SCHEMA IF NOT EXISTS menuplan;
CREATE SCHEMA IF NOT EXISTS recon;

-- ── shared: katalog bahan tunggal ───────────────────────────────────
CREATE TABLE IF NOT EXISTS shared.bahan_kategori (
    nama   TEXT PRIMARY KEY,
    urutan INT NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS shared.bahan (
    kode           TEXT PRIMARY KEY,
    nama           TEXT NOT NULL,
    satuan         TEXT CHECK (satuan IN ('g','ml','pcs')),
    jenis          TEXT CHECK (jenis IN ('mentah','frozen','kemasan','pelengkap')),
    kategori       TEXT REFERENCES shared.bahan_kategori(nama) ON UPDATE CASCADE ON DELETE SET NULL,
    kategori_recon TEXT,                     -- 'Belanja Bahan' | 'Kemasan' (kamus bot-recon)
    updated_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS shared.bahan_alias (
    id         BIGSERIAL PRIMARY KEY,
    bahan_kode TEXT NOT NULL REFERENCES shared.bahan(kode) ON UPDATE CASCADE ON DELETE CASCADE,
    match      TEXT NOT NULL,
    isi        NUMERIC(14,4),                -- isi per kemasan (dari menuplan)
    toko       TEXT,                         -- NULL = semua toko
    sumber     TEXT NOT NULL DEFAULT 'manual'  -- menuplan | recon | stoa | manual
);
CREATE INDEX IF NOT EXISTS idx_alias_bahan ON shared.bahan_alias (bahan_kode);
CREATE UNIQUE INDEX IF NOT EXISTS uq_alias_match
    ON shared.bahan_alias (lower(match), COALESCE(lower(toko), ''));

-- Riwayat harga belanja = VIEW atas stoa.item_prices (diisi stoabot), bahan_kode
-- di-resolve lewat alias saat dibaca. Tidak ada salinan data yang bisa basi.
-- (dibuat di 002_views.sql setelah schema stoa terisi)

CREATE TABLE IF NOT EXISTS shared.shared_rules (
    key        TEXT PRIMARY KEY,
    value      JSONB NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ── menuplan: harga manual yang merujuk harga belanja ───────────────
CREATE TABLE IF NOT EXISTS menuplan.harga_manual (
    bahan_kode TEXT PRIMARY KEY REFERENCES shared.bahan(kode) ON UPDATE CASCADE ON DELETE CASCADE,
    harga      NUMERIC(15,2) NOT NULL CHECK (harga >= 0),  -- per satuan bahan (g/ml/pcs)
    catatan    TEXT,
    diubah_oleh TEXT,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

