#!/usr/bin/env bash
# Salin SEMUA tabel schema public dari database lama ke schema baru di Postgres induk.
# Sumber hanya DIBACA (pg_dump). Pakai: SRC_URL=... NEW_URL=... ./copy_app_schema.sh stoa
#   stoa     <- DB stoabot lama     | menuplan <- DB menuplan lama | (recon: shared_rules -> shared)
# Setelah itu: psql "$NEW_URL" -f 002_views.sql
set -euo pipefail
SCHEMA="${1:?schema tujuan: stoa | menuplan}"
: "${SRC_URL:?}" "${NEW_URL:?}"
case "$SCHEMA" in stoa|menuplan) ;; *) echo "schema harus stoa atau menuplan"; exit 1;; esac
if [ "$(psql "$NEW_URL" -Atc "select count(*) from information_schema.tables where table_schema='$SCHEMA'")" != "0" ]; then
  echo "schema $SCHEMA sudah berisi tabel; hentikan (cegah dobel)"; exit 1
fi
pg_dump "$SRC_URL" --schema=public --no-owner --no-privileges \
  | sed -E "s/\bpublic\./${SCHEMA}./g; /^CREATE SCHEMA public/d; /^COMMENT ON SCHEMA public/d" \
  | psql "$NEW_URL" -v ON_ERROR_STOP=1 -c "SET search_path=${SCHEMA}" -f -
psql "$NEW_URL" -Atc "select '$SCHEMA', count(*) from information_schema.tables where table_schema='$SCHEMA'"
