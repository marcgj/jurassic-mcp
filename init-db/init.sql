CREATE DATABASE stores_demo WITH LOG;
DATABASE stores_demo;

CREATE TABLE customers (
    customer_id SERIAL PRIMARY KEY,
    customer_code VARCHAR(20) NOT NULL,
    full_name VARCHAR(120) NOT NULL,
    created_at DATETIME YEAR TO SECOND NOT NULL
);

CREATE UNIQUE INDEX ux_customers_code ON customers(customer_code);

CREATE TABLE orders (
    order_id SERIAL PRIMARY KEY,
    customer_id INTEGER NOT NULL,
    order_date DATE NOT NULL,
    amount DECIMAL(12,2) NOT NULL
);

CREATE INDEX ix_orders_customer_id ON orders(customer_id);

ALTER TABLE orders
    ADD CONSTRAINT FOREIGN KEY (customer_id)
    REFERENCES customers(customer_id)
    CONSTRAINT fk_orders_customer;

CREATE TABLE order_items (
    order_item_id SERIAL PRIMARY KEY,
    order_id INTEGER NOT NULL,
    sku VARCHAR(40) NOT NULL,
    quantity INTEGER NOT NULL,
    price DECIMAL(12,2) NOT NULL
);

CREATE INDEX ix_order_items_order_id ON order_items(order_id);

ALTER TABLE order_items
    ADD CONSTRAINT FOREIGN KEY (order_id)
    REFERENCES orders(order_id)
    CONSTRAINT fk_items_order;
