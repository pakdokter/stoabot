# Postgres induk (stoa-db)

Satu Postgres, satu schema per aplikasi: `shared`, `stoa`, `menuplan`, `recon`.
Katalog bahan + alias digabung di `shared.bahan` / `shared.bahan_alias`.
Harga belanja di `shared.harga_belanja`; menuplan bisa override lewat
`menuplan.harga_manual` (lihat view `menuplan.v_harga_efektif`).

Database lama TIDAK diubah (jadi backup).

```bash
# isi sendiri di terminal (jangan commit / jangan tempel di chat)
export NEW_URL=... MENUPLAN_URL=... STOA_URL=... RECON_BAHAN=/path/ke/bahan.json
psql "$NEW_URL" -f 001_schemas.sql
python3 migrate_bahan.py            # simulasi (default), hanya laporan
python3 migrate_bahan.py --apply    # tulis ke shared.*
```
