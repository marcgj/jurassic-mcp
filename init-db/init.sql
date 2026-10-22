-- ---------------------------------------------------------------------------
-- stores_demo: sample schema for the jurassic-mcp test environment.
--
-- The database itself is created by init-db/apply_init.py
-- (CREATE DATABASE stores_demo WITH LOG, issued from a sysmaster
-- connection); this script is then executed inside stores_demo.
--
-- To load it by hand with dbaccess instead:
--   echo 'CREATE DATABASE stores_demo WITH LOG;' | dbaccess - -
--   dbaccess stores_demo init.sql
--
-- A few tables/columns deliberately use cryptic "legacy" names (artmae,
-- tabgen, am_*, tg_*, ord_sts ...) so the description feature of the MCP
-- server can be demonstrated (see config.test.yml).
-- Dates are built with MDY()/DATETIME literals so they do not depend on
-- DBDATE / GL_DATE settings.
-- ---------------------------------------------------------------------------

-- Generic code table (typical legacy "tabla general"): tg_tip = code family,
-- tg_cod = code inside the family.
CREATE TABLE tabgen (
    tg_tip   CHAR(4)      NOT NULL,
    tg_cod   CHAR(4)      NOT NULL,
    tg_des   VARCHAR(60)  NOT NULL,
    PRIMARY KEY (tg_tip, tg_cod) CONSTRAINT pk_tabgen
);

-- Item master ("articulos maestro").
CREATE TABLE artmae (
    am_cod   CHAR(12)      NOT NULL,
    am_des   VARCHAR(80)   NOT NULL,
    am_fam   CHAR(4),
    am_pvp   DECIMAL(12,2) NOT NULL,
    am_baja  CHAR(1)       DEFAULT 'N' NOT NULL,
    PRIMARY KEY (am_cod) CONSTRAINT pk_artmae
);

CREATE TABLE customers (
    customer_id    SERIAL                  NOT NULL,
    customer_code  VARCHAR(20)             NOT NULL,
    full_name      VARCHAR(120)            NOT NULL,
    created_at     DATETIME YEAR TO SECOND NOT NULL,
    PRIMARY KEY (customer_id) CONSTRAINT pk_customers
);

CREATE UNIQUE INDEX ux_customers_code ON customers (customer_code);

CREATE TABLE orders (
    order_id     SERIAL         NOT NULL,
    customer_id  INTEGER        NOT NULL,
    order_date   DATE           NOT NULL,
    amount       DECIMAL(12,2)  NOT NULL,
    ord_sts      CHAR(1)        DEFAULT 'P' NOT NULL,
    PRIMARY KEY (order_id) CONSTRAINT pk_orders
);

CREATE INDEX ix_orders_customer_id ON orders (customer_id);

ALTER TABLE orders
    ADD CONSTRAINT FOREIGN KEY (customer_id)
    REFERENCES customers (customer_id)
    CONSTRAINT fk_orders_customer;

CREATE TABLE order_items (
    order_item_id  SERIAL         NOT NULL,
    order_id       INTEGER        NOT NULL,
    sku            CHAR(12)       NOT NULL,
    quantity       INTEGER        NOT NULL,
    price          DECIMAL(12,2)  NOT NULL,
    PRIMARY KEY (order_item_id) CONSTRAINT pk_order_items
);

CREATE INDEX ix_order_items_order_id ON order_items (order_id);
CREATE INDEX ix_order_items_sku ON order_items (sku);

ALTER TABLE order_items
    ADD CONSTRAINT FOREIGN KEY (order_id)
    REFERENCES orders (order_id)
    CONSTRAINT fk_items_order;

ALTER TABLE order_items
    ADD CONSTRAINT FOREIGN KEY (sku)
    REFERENCES artmae (am_cod)
    CONSTRAINT fk_items_artmae;

-- ---------------------------------------------------------------------------
-- Sample data (Informix has no multi-row VALUES: one INSERT per row)
-- ---------------------------------------------------------------------------
INSERT INTO tabgen (tg_tip, tg_cod, tg_des) VALUES ('FAM', 'HERR', 'Herramientas');
INSERT INTO tabgen (tg_tip, tg_cod, tg_des) VALUES ('FAM', 'ELEC', 'Material electrico');
INSERT INTO tabgen (tg_tip, tg_cod, tg_des) VALUES ('OSTS', 'P', 'Pending');
INSERT INTO tabgen (tg_tip, tg_cod, tg_des) VALUES ('OSTS', 'S', 'Shipped');
INSERT INTO tabgen (tg_tip, tg_cod, tg_des) VALUES ('OSTS', 'X', 'Cancelled; do not invoice');

INSERT INTO artmae (am_cod, am_des, am_fam, am_pvp) VALUES ('HAM-001', 'Claw hammer 500g', 'HERR', 12.50);
INSERT INTO artmae (am_cod, am_des, am_fam, am_pvp) VALUES ('SCR-010', 'Screwdriver set (10 pcs)', 'HERR', 24.90);
INSERT INTO artmae (am_cod, am_des, am_fam, am_pvp, am_baja) VALUES ('CAB-2X15', 'Cable 2x1.5mm, 100m', 'ELEC', 59.00, 'S');

INSERT INTO customers (customer_code, full_name, created_at)
    VALUES ('C0001', 'Acme Hardware Ltd.', DATETIME(2023-03-01 09:15:00) YEAR TO SECOND);
INSERT INTO customers (customer_code, full_name, created_at)
    VALUES ('C0002', 'O''Brien & Sons', DATETIME(2024-07-12 17:40:00) YEAR TO SECOND);

INSERT INTO orders (customer_id, order_date, amount, ord_sts) VALUES (1, MDY(1, 15, 2024), 37.40, 'S');
INSERT INTO orders (customer_id, order_date, amount, ord_sts) VALUES (2, MDY(2, 3, 2025), 59.00, 'P');

INSERT INTO order_items (order_id, sku, quantity, price) VALUES (1, 'HAM-001', 1, 12.50);
INSERT INTO order_items (order_id, sku, quantity, price) VALUES (1, 'SCR-010', 1, 24.90);
INSERT INTO order_items (order_id, sku, quantity, price) VALUES (2, 'CAB-2X15', 1, 59.00);
