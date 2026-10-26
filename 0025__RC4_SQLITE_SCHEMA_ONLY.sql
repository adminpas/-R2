-- Schema-only export from the bundled RC4 pilot SQLite database. No data rows included.
-- Reference copy only. Review and migrate explicitly; NOT a production migration.
PRAGMA foreign_keys=ON;

CREATE TABLE ad_slots(id INTEGER PRIMARY KEY, slot_key TEXT UNIQUE NOT NULL, title TEXT NOT NULL, subtitle TEXT NOT NULL DEFAULT '', target_url TEXT NOT NULL DEFAULT '', active INTEGER NOT NULL DEFAULT 1, updated_at TEXT NOT NULL);

CREATE TABLE audit_log(id INTEGER PRIMARY KEY, user_id INTEGER, event TEXT NOT NULL, object_type TEXT NOT NULL DEFAULT '', object_id TEXT NOT NULL DEFAULT '', detail TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL);

CREATE TABLE categories(id INTEGER PRIMARY KEY, parent_id INTEGER REFERENCES categories(id) ON DELETE RESTRICT, name_ar TEXT NOT NULL, name_en TEXT NOT NULL DEFAULT '', folder_path TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL);

CREATE TABLE financial_accounts(
          id INTEGER PRIMARY KEY, code TEXT UNIQUE NOT NULL, name_ar TEXT NOT NULL,
          account_type TEXT NOT NULL CHECK(account_type IN ('ASSET','LIABILITY','EQUITY','REVENUE','EXPENSE')),
          store_id INTEGER REFERENCES merchant_stores(id) ON DELETE RESTRICT,
          active INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL
        );

CREATE TABLE ledger_entries(
          id INTEGER PRIMARY KEY, transaction_id INTEGER NOT NULL REFERENCES ledger_transactions(id) ON DELETE RESTRICT,
          account_id INTEGER NOT NULL REFERENCES financial_accounts(id) ON DELETE RESTRICT,
          side TEXT NOT NULL CHECK(side IN ('DEBIT','CREDIT')), amount_minor INTEGER NOT NULL CHECK(amount_minor>0),
          currency TEXT NOT NULL CHECK(currency IN ('SAR','YER','USD')), memo TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL
        );

CREATE TABLE ledger_transactions(
          id INTEGER PRIMARY KEY, txn_key TEXT UNIQUE NOT NULL, txn_type TEXT NOT NULL,
          reference_type TEXT NOT NULL DEFAULT '', reference_id TEXT NOT NULL DEFAULT '',
          description TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'POSTED' CHECK(status IN ('POSTED','REVERSED')),
          created_by INTEGER REFERENCES users(id), created_at TEXT NOT NULL
        );

CREATE TABLE merchant_stores(
          id INTEGER PRIMARY KEY, store_key TEXT UNIQUE NOT NULL, slug TEXT UNIQUE NOT NULL,
          display_name TEXT NOT NULL, owner_user_id INTEGER UNIQUE REFERENCES users(id) ON DELETE SET NULL,
          status TEXT NOT NULL DEFAULT 'PENDING_REVIEW' CHECK(status IN ('PENDING_REVIEW','APPROVED','SUSPENDED')),
          contact_name TEXT NOT NULL DEFAULT '', contact_phone TEXT NOT NULL DEFAULT '', contact_email TEXT NOT NULL DEFAULT '',
          created_at TEXT NOT NULL, updated_at TEXT NOT NULL
        );

CREATE TABLE products(id INTEGER PRIMARY KEY, category_id INTEGER REFERENCES categories(id) ON DELETE RESTRICT, merchant_id INTEGER REFERENCES users(id), title_ar TEXT NOT NULL, title_en TEXT NOT NULL DEFAULT '', brand TEXT NOT NULL DEFAULT '', price REAL NOT NULL DEFAULT 0 CHECK(price>=0), condition TEXT NOT NULL DEFAULT 'USED_CLEAN' CHECK(condition IN ('NEW_4K','NEAR_NEW_BLURAY','USED_CLEAN','REFURBISHED','READY_NO_MAINTENANCE')), availability TEXT NOT NULL DEFAULT 'AVAILABLE' CHECK(availability IN ('AVAILABLE','HOLD_24H','SOLD')), description TEXT NOT NULL DEFAULT '', specs_json TEXT NOT NULL DEFAULT '{}', image_path TEXT NOT NULL DEFAULT '', video_path TEXT NOT NULL DEFAULT '', active INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL, updated_at TEXT NOT NULL);

CREATE TABLE quote_requests(id INTEGER PRIMARY KEY, product_id INTEGER NOT NULL REFERENCES products(id), customer_name TEXT NOT NULL, phone TEXT NOT NULL, details TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'NEW');

CREATE TABLE reservations(id INTEGER PRIMARY KEY, product_id INTEGER NOT NULL REFERENCES products(id), customer_name TEXT NOT NULL, phone TEXT NOT NULL, note TEXT NOT NULL DEFAULT '', status TEXT NOT NULL DEFAULT 'ACTIVE' CHECK(status IN ('ACTIVE','EXPIRED','CANCELLED','CONVERTED')), created_at TEXT NOT NULL, expires_at TEXT NOT NULL);

CREATE TABLE security_quarantine(
          id INTEGER PRIMARY KEY, target_type TEXT NOT NULL CHECK(target_type IN ('PRODUCT','SPARE_PART')),
          target_id INTEGER NOT NULL, store_id INTEGER REFERENCES merchant_stores(id),
          uploader_user_id INTEGER REFERENCES users(id), media_kind TEXT NOT NULL CHECK(media_kind IN ('image','video')),
          original_name TEXT NOT NULL, stored_path TEXT NOT NULL, size_bytes INTEGER NOT NULL,
          sha256 TEXT NOT NULL, declared_content_type TEXT NOT NULL DEFAULT 'application/octet-stream',
          scan_status TEXT NOT NULL DEFAULT 'QUARANTINED' CHECK(scan_status IN ('QUARANTINED','CLEAN','INFECTED','SCAN_UNAVAILABLE','RELEASED','REJECTED')),
          scan_engine TEXT NOT NULL DEFAULT '', scan_detail TEXT NOT NULL DEFAULT '',
          created_at TEXT NOT NULL, scanned_at TEXT NOT NULL DEFAULT '', released_at TEXT NOT NULL DEFAULT ''
        );

CREATE TABLE spare_part_categories(
          id INTEGER PRIMARY KEY, parent_id INTEGER REFERENCES spare_part_categories(id) ON DELETE RESTRICT,
          name_ar TEXT NOT NULL, slug TEXT UNIQUE NOT NULL, active INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL
        );

CREATE TABLE spare_part_quote_requests(
          id INTEGER PRIMARY KEY, part_id INTEGER NOT NULL REFERENCES spare_parts(id) ON DELETE RESTRICT,
          store_id INTEGER NOT NULL REFERENCES merchant_stores(id) ON DELETE RESTRICT,
          customer_name TEXT NOT NULL, phone TEXT NOT NULL, details TEXT NOT NULL DEFAULT '',
          status TEXT NOT NULL DEFAULT 'NEW' CHECK(status IN ('NEW','CONTACTED','QUOTED','CLOSED')),
          created_at TEXT NOT NULL
        );

CREATE TABLE spare_parts(
          id INTEGER PRIMARY KEY, store_id INTEGER NOT NULL REFERENCES merchant_stores(id) ON DELETE RESTRICT,
          category_id INTEGER NOT NULL REFERENCES spare_part_categories(id) ON DELETE RESTRICT,
          name_ar TEXT NOT NULL, name_en TEXT NOT NULL DEFAULT '', part_number TEXT NOT NULL DEFAULT '',
          brand TEXT NOT NULL DEFAULT '', compatible_equipment TEXT NOT NULL DEFAULT '', model_range TEXT NOT NULL DEFAULT '',
          specifications TEXT NOT NULL DEFAULT '', application TEXT NOT NULL DEFAULT '', condition TEXT NOT NULL DEFAULT 'NEW',
          unit_price REAL NOT NULL DEFAULT 0 CHECK(unit_price>=0), currency TEXT NOT NULL DEFAULT 'SAR',
          stock_quantity INTEGER NOT NULL DEFAULT 0 CHECK(stock_quantity>=0), description TEXT NOT NULL DEFAULT '',
          image_path TEXT NOT NULL DEFAULT '', video_path TEXT NOT NULL DEFAULT '',
          status TEXT NOT NULL DEFAULT 'PENDING_REVIEW' CHECK(status IN ('DRAFT','PENDING_REVIEW','PUBLISHED','REJECTED','ARCHIVED')),
          review_note TEXT NOT NULL DEFAULT '', created_by INTEGER REFERENCES users(id), created_at TEXT NOT NULL, updated_at TEXT NOT NULL
        );

CREATE TABLE users(id INTEGER PRIMARY KEY, username TEXT UNIQUE NOT NULL, password_hash TEXT NOT NULL, role TEXT NOT NULL CHECK(role IN ('SUPER_ADMIN','ASSISTANT_ADMIN','MERCHANT','MAINTENANCE_TECH','SHIPPING_COMPANY')), is_active INTEGER NOT NULL DEFAULT 1, must_change_password INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL);

CREATE INDEX ledger_entries_account_idx ON ledger_entries(account_id,currency,created_at);

CREATE INDEX ledger_entries_txn_idx ON ledger_entries(transaction_id,id);

CREATE UNIQUE INDEX one_active_hold_per_product ON reservations(product_id) WHERE status='ACTIVE';

CREATE INDEX security_quarantine_status_idx ON security_quarantine(scan_status,created_at);

CREATE INDEX spare_parts_part_number_idx ON spare_parts(part_number);

CREATE INDEX spare_parts_store_status_idx ON spare_parts(store_id,status,updated_at);

CREATE TRIGGER ledger_entries_no_delete BEFORE DELETE ON ledger_entries BEGIN SELECT RAISE(ABORT,'ledger entries are immutable; post a reversal'); END;

CREATE TRIGGER ledger_entries_no_update BEFORE UPDATE ON ledger_entries BEGIN SELECT RAISE(ABORT,'ledger entries are immutable; post a reversal'); END;

CREATE TRIGGER ledger_transactions_no_delete BEFORE DELETE ON ledger_transactions BEGIN SELECT RAISE(ABORT,'ledger transactions are immutable; post a reversal'); END;

CREATE TRIGGER ledger_transactions_no_update BEFORE UPDATE ON ledger_transactions BEGIN SELECT RAISE(ABORT,'ledger transactions are immutable; post a reversal'); END;

