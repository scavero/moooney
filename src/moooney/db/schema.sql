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
CREATE INDEX IF NOT EXISTS idx_transactions_account_date ON transactions(account, date);
CREATE INDEX IF NOT EXISTS idx_transactions_category ON transactions(category);

-- Fase 2.5: enriquecimiento externo. Una clave se consulta una sola vez.
CREATE TABLE IF NOT EXISTS merchant_enrichment (
    key         TEXT PRIMARY KEY,
    provider    TEXT,
    query       TEXT NOT NULL,
    summary     TEXT,
    place_types TEXT,
    url         TEXT,
    helped      INTEGER,
    fetched_at  TEXT NOT NULL
);

-- Registro de CADA consulta externa (haya resultado o no): el egreso web auditable.
CREATE TABLE IF NOT EXISTS web_calls (
    id           INTEGER PRIMARY KEY,
    ts           TEXT NOT NULL,
    provider     TEXT NOT NULL,
    query        TEXT NOT NULL,
    status       TEXT NOT NULL,
    payload_hash TEXT
);

-- Fase 3: recurrentes detectados (se recalcula entero en cada `recurring`).
CREATE TABLE IF NOT EXISTS recurring (
    key           TEXT PRIMARY KEY,
    category      TEXT,
    flow_type     TEXT NOT NULL,
    period_days   INTEGER NOT NULL,
    period_label  TEXT NOT NULL,
    n             INTEGER NOT NULL,
    amount_avg    REAL NOT NULL,
    amount_last   REAL NOT NULL,
    amount_cv     REAL NOT NULL,
    first_seen    TEXT NOT NULL,
    last_seen     TEXT NOT NULL,
    next_expected TEXT NOT NULL,
    active        INTEGER NOT NULL DEFAULT 1,
    updated_at    TEXT
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_budgets_category_month ON budgets(category, month);

-- Fase 5: informes generados (con modelo o sin él). Se guarda cada versión.
CREATE TABLE IF NOT EXISTS reports (
    id         INTEGER PRIMARY KEY,
    month      TEXT NOT NULL,
    kind       TEXT NOT NULL,
    model      TEXT,
    content    TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_reports_month ON reports(month);

-- Etiquetas libres en movimientos sueltos (viaje, reforma, boda…): agrupan gastos de
-- distintas categorías sin tocar la memoria de comercios. Minúsculas, hasta 40 caracteres.
CREATE TABLE IF NOT EXISTS transaction_tags (
    transaction_id INTEGER NOT NULL REFERENCES transactions(id) ON DELETE CASCADE,
    tag            TEXT NOT NULL,
    created_at     TEXT NOT NULL,
    PRIMARY KEY (transaction_id, tag)
);
CREATE INDEX IF NOT EXISTS idx_transaction_tags_tag ON transaction_tags(tag);
