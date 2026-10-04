CREATE SCHEMA IF NOT EXISTS lab;
CREATE TABLE IF NOT EXISTS lab.orders (
    order_id BIGINT PRIMARY KEY CHECK (order_id > 0),
    order_date DATE NOT NULL,
    city TEXT NOT NULL,
    category TEXT NOT NULL,
    quantity INTEGER NOT NULL CHECK (quantity BETWEEN 1 AND 1000000),
    unit_price NUMERIC(12,2) NOT NULL CHECK (unit_price >= 0),
    status TEXT NOT NULL CHECK (status IN ('completed', 'pending', 'cancelled')),
    revenue NUMERIC(20,2) GENERATED ALWAYS AS (
        CASE WHEN status = 'completed' THEN quantity * unit_price ELSE 0 END
    ) STORED
);
GRANT USAGE ON SCHEMA lab TO sales_etl;
GRANT SELECT, INSERT, UPDATE ON lab.orders TO sales_etl;
