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

-- Baris harga belanja aktif untuk Menuplan (KATALOG_BERSAMA): tanpa resolusi alias, kolom sama dengan sp_harga.
CREATE OR REPLACE VIEW shared.harga_belanja_aktif AS
SELECT ip.id, ip.item_name, ip.item_name_raw, ip.toko, ip.unit,
       COALESCE(ip.qty, 1) AS qty,
       COALESCE(ip.total_price, ip.unit_price * COALESCE(ip.qty, 1)) AS total_price,
       ip.transaction_date, ip.transaction_id, ip.created_at
FROM stoa.item_prices ip
LEFT JOIN stoa.transactions t ON t.id = ip.transaction_id
WHERE (ip.transaction_id IS NULL OR (t.id IS NOT NULL AND t.is_deleted = FALSE))
  AND COALESCE(ip.total_price, ip.unit_price) > 0;
