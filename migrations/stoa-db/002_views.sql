-- Jalankan SETELAH data schema stoa disalin (copy_app_schema.sh) dan shared.bahan terisi.
CREATE OR REPLACE VIEW shared.harga_belanja AS
SELECT ip.id,
       a.bahan_kode,
       ip.item_name, ip.item_name_raw, ip.toko, ip.unit,
       ip.qty, ip.unit_price, ip.total_price,
       ip.transaction_date, ip.transaction_id, ip.created_at
FROM stoa.item_prices ip
LEFT JOIN stoa.transactions t ON t.id = ip.transaction_id
LEFT JOIN LATERAL (
    SELECT x.bahan_kode FROM shared.bahan_alias x
    WHERE lower(x.match) IN (lower(COALESCE(ip.item_name_raw, ip.item_name)), lower(ip.item_name))
      AND (x.toko IS NULL OR lower(x.toko) = lower(ip.toko))
    ORDER BY (x.toko IS NOT NULL) DESC, x.id
    LIMIT 1
) a ON TRUE
WHERE ip.transaction_id IS NULL OR (t.id IS NOT NULL AND t.is_deleted = FALSE);

-- Harga efektif: manual kalau ada, kalau tidak harga belanja terakhir.
CREATE OR REPLACE VIEW menuplan.v_harga_efektif AS
SELECT b.kode,
       b.nama,
       hb.harga_belanja,
       hb.tgl_belanja,
       hm.harga                               AS harga_manual,
       COALESCE(hm.harga, hb.harga_belanja)   AS harga_efektif,
       CASE WHEN hm.harga IS NOT NULL THEN 'manual' ELSE 'belanja' END AS sumber
FROM shared.bahan b
LEFT JOIN LATERAL (
    SELECT h.unit_price AS harga_belanja, h.transaction_date AS tgl_belanja
    FROM shared.harga_belanja h
    WHERE h.bahan_kode = b.kode AND h.unit_price IS NOT NULL
    ORDER BY h.transaction_date DESC, h.created_at DESC
    LIMIT 1
) hb ON TRUE
LEFT JOIN menuplan.harga_manual hm ON hm.bahan_kode = b.kode;

-- ── recon: kamus bahan bot-recon (pemetaan alias -> nama baku Kas-Buku) ──
-- Menyimpan pemetaan asli recon utuh, termasuk yang kalah konflik di shared.bahan_alias.
CREATE TABLE IF NOT EXISTS recon.kamus_bahan (
    alias     TEXT PRIMARY KEY,           -- huruf kecil, dicocokkan utuh
    nama_baku TEXT NOT NULL,
    kategori  TEXT NOT NULL               -- 'Belanja Bahan' | 'Kemasan'
);
