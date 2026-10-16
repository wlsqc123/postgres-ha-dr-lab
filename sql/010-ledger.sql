CREATE TABLE IF NOT EXISTS accounts (
    id integer PRIMARY KEY,
    balance bigint NOT NULL CHECK (balance >= 0)
);
INSERT INTO accounts VALUES (1, 1000000), (2, 1000000) ON CONFLICT DO NOTHING;

CREATE TABLE IF NOT EXISTS request_history (
    request_id uuid PRIMARY KEY,
    source integer NOT NULL REFERENCES accounts(id),
    destination integer NOT NULL REFERENCES accounts(id),
    amount bigint NOT NULL CHECK (amount > 0),
    CHECK (source <> destination)
);

CREATE TABLE IF NOT EXISTS ledger_entries (
    request_id uuid NOT NULL REFERENCES request_history(request_id),
    account_id integer NOT NULL REFERENCES accounts(id),
    delta bigint NOT NULL CHECK (delta <> 0),
    PRIMARY KEY (request_id, account_id)
);
