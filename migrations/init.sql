-- ============================================================
-- stoabot — database schema
-- ============================================================

CREATE EXTENSION IF NOT EXISTS "uuid-ossp";

-- ------------------------------------------------------------
-- users
-- ------------------------------------------------------------
CREATE TABLE users (
    id          BIGINT PRIMARY KEY,          -- telegram user_id
    username    VARCHAR(64),
    full_name   VARCHAR(128) NOT NULL,
    role        VARCHAR(16) NOT NULL DEFAULT 'staff'
                    CHECK (role IN ('admin','staff')),
    is_active   BOOLEAN NOT NULL DEFAULT TRUE,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- ------------------------------------------------------------
-- transactions
-- ------------------------------------------------------------
CREATE TABLE transactions (
    id              UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    user_id         BIGINT NOT NULL REFERENCES users(id),
    type            VARCHAR(8) NOT NULL CHECK (type IN ('masuk','keluar')),
    amount          NUMERIC(15,2) NOT NULL CHECK (amount > 0),
    description     TEXT NOT NULL,
    category        VARCHAR(64),
    transaction_date DATE NOT NULL DEFAULT CURRENT_DATE,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    is_deleted      BOOLEAN NOT NULL DEFAULT FALSE,
    deleted_at      TIMESTAMPTZ
);

CREATE INDEX idx_transactions_user_date
    ON transactions(user_id, transaction_date DESC)
    WHERE is_deleted = FALSE;

CREATE INDEX idx_transactions_date
    ON transactions(transaction_date DESC)
    WHERE is_deleted = FALSE;

-- ------------------------------------------------------------
-- attachments
-- ------------------------------------------------------------
CREATE TABLE attachments (
    id              UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    transaction_id  UUID REFERENCES transactions(id) ON DELETE CASCADE,
    telegram_file_id VARCHAR(256) NOT NULL,
    file_type       VARCHAR(32) DEFAULT 'image',
    ocr_raw_text    TEXT,
    ocr_confidence  NUMERIC(5,2),
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- ------------------------------------------------------------
-- audit_logs — setiap INSERT/UPDATE/DELETE tercatat
-- ------------------------------------------------------------
CREATE TABLE audit_logs (
    id              UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    user_id         BIGINT NOT NULL REFERENCES users(id),
    action          VARCHAR(16) NOT NULL CHECK (action IN ('create','update','delete')),
    table_name      VARCHAR(64) NOT NULL,
    record_id       UUID NOT NULL,
    old_values      JSONB,
    new_values      JSONB,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- ------------------------------------------------------------
-- trigger: auto-update updated_at
-- ------------------------------------------------------------
CREATE OR REPLACE FUNCTION set_updated_at()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = NOW();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER trg_users_updated_at
    BEFORE UPDATE ON users
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();

CREATE TRIGGER trg_transactions_updated_at
    BEFORE UPDATE ON transactions
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();
