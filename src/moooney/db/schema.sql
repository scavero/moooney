-- Esquema de moooney. Fase 1 usa imports y transactions;
-- el resto se crea ya con su forma definitiva.

CREATE TABLE IF NOT EXISTS imports (
    id          INTEGER PRIMARY KEY,
    sha256      TEXT NOT NULL UNIQUE,
    filename    TEXT NOT NULL,
    imported_at TEXT NOT NULL,
    rows_total  INTEGER NOT NULL,
    rows_new    INTEGER NOT NULL,
    date_min    TEXT,
    date_max    TEXT
);

CREATE TABLE IF NOT EXISTS transactions (
    id            INTEGER PRIMARY KEY,
    fingerprint   TEXT NOT NULL UNIQUE,
    date          TEXT NOT NULL,
    value_date    TEXT,
    concept       TEXT NOT NULL,
    concept_clean TEXT,
    amount        REAL NOT NULL,
    balance       REAL,
    account       TEXT NOT NULL,
    flow_type     TEXT,
    category      TEXT,
    subcategory   TEXT,
    confidence    REAL,
    import_id     INTEGER REFERENCES imports(id),
    created_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS merchants (
    key         TEXT PRIMARY KEY,
    category    TEXT,
    subcategory TEXT,
    flow_type   TEXT,
    source      TEXT,
    confidence  REAL,
    updated_at  TEXT
);

-- Caché recompute: el vector de una clave puede guardarse antes de que la
-- clave tenga entrada en merchants (se embedding al primer categorize).
CREATE TABLE IF NOT EXISTS merchant_embeddings (
    key        TEXT PRIMARY KEY,
    model      TEXT NOT NULL,
    vector     BLOB NOT NULL,
    updated_at TEXT
);

CREATE TABLE IF NOT EXISTS budgets (
    id           INTEGER PRIMARY KEY,
    category     TEXT NOT NULL,
    month        TEXT NOT NULL,
    limit_amount REAL NOT NULL,
    source       TEXT
);

CREATE TABLE IF NOT EXISTS alerts (
    id             INTEGER PRIMARY KEY,
    type           TEXT NOT NULL,
    severity       TEXT,
    date           TEXT,
    transaction_id INTEGER REFERENCES transactions(id),
    message        TEXT,
    acknowledged   INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS llm_calls (
    id            INTEGER PRIMARY KEY,
    ts            TEXT NOT NULL,
    model         TEXT NOT NULL,
    purpose       TEXT,
    input_tokens  INTEGER,
    output_tokens INTEGER,
    payload_hash  TEXT
);

CREATE INDEX IF NOT EXISTS idx_transactions_date ON transactions(date);
CREATE INDEX IF NOT EXISTS idx_transactions_category ON transactions(category);
