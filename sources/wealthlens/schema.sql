-- Synthetic WealthLens schema (demo). Align with the real WealthLens schema before pointing at live data.
DROP TABLE IF EXISTS budgets, goals, holdings, asset_prices, assets, transactions, categories, accounts CASCADE;

CREATE TABLE accounts (
    id            integer PRIMARY KEY,
    name          text NOT NULL,
    institution   text NOT NULL,
    account_type  text NOT NULL CHECK (account_type IN ('bank','credit_card','brokerage')),
    currency      char(3) NOT NULL,
    opened_on     date NOT NULL
);

CREATE TABLE categories (
    id         integer PRIMARY KEY,
    name       text NOT NULL,
    kind       text NOT NULL CHECK (kind IN ('income','expense','transfer')),
    parent_id  integer REFERENCES categories(id)
);

CREATE TABLE transactions (
    id           integer PRIMARY KEY,
    account_id   integer NOT NULL REFERENCES accounts(id),
    category_id  integer NOT NULL REFERENCES categories(id),
    txn_date     date NOT NULL,
    amount       numeric(14,2) NOT NULL CHECK (amount >= 0),
    currency     char(3) NOT NULL,
    txn_type     text NOT NULL CHECK (txn_type IN ('debit','credit')),
    merchant     text,
    description  text
);
CREATE INDEX idx_transactions_date ON transactions (txn_date);
CREATE INDEX idx_transactions_account ON transactions (account_id);
CREATE INDEX idx_transactions_category ON transactions (category_id);

CREATE TABLE assets (
    id          integer PRIMARY KEY,
    symbol      text NOT NULL UNIQUE,
    name        text NOT NULL,
    asset_type  text NOT NULL CHECK (asset_type IN ('stock','mutual_fund','etf','bond')),
    currency    char(3) NOT NULL
);

CREATE TABLE asset_prices (
    asset_id    integer NOT NULL REFERENCES assets(id),
    price_date  date NOT NULL,
    close       numeric(14,4) NOT NULL,
    PRIMARY KEY (asset_id, price_date)
);

CREATE TABLE holdings (
    id          integer PRIMARY KEY,
    account_id  integer NOT NULL REFERENCES accounts(id),
    asset_id    integer NOT NULL REFERENCES assets(id),
    quantity    numeric(18,4) NOT NULL,
    avg_cost    numeric(14,4) NOT NULL,
    as_of       date NOT NULL
);

CREATE TABLE budgets (
    id           integer PRIMARY KEY,
    category_id  integer NOT NULL REFERENCES categories(id),
    month        date NOT NULL,
    amount       numeric(14,2) NOT NULL,
    UNIQUE (category_id, month)
);

CREATE TABLE goals (
    id              integer PRIMARY KEY,
    name            text NOT NULL,
    target_amount   numeric(14,2) NOT NULL,
    current_amount  numeric(14,2) NOT NULL,
    target_date     date NOT NULL
);

COMMENT ON TABLE transactions IS 'One row per bank or card transaction. amount is always positive; txn_type gives the direction.';
COMMENT ON COLUMN transactions.amount IS 'Absolute amount in the transaction currency.';
COMMENT ON COLUMN transactions.txn_type IS 'debit = money out, credit = money in.';
COMMENT ON TABLE categories IS 'Spending/income categories. kind separates expense, income and transfers.';
COMMENT ON TABLE holdings IS 'Current investment positions per brokerage account.';
COMMENT ON TABLE asset_prices IS 'Month-end closing prices per asset.';
COMMENT ON TABLE budgets IS 'Monthly budget per expense category.';
COMMENT ON TABLE goals IS 'Savings goals with target and current amount.';
