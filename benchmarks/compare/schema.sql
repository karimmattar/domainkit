-- The one table every app reads and writes, and its rows.
DROP TABLE IF EXISTS compare_items;
CREATE TABLE compare_items (
    id uuid PRIMARY KEY,
    version integer NOT NULL DEFAULT 1,
    name varchar(50) NOT NULL,
    price numeric(10, 2) NOT NULL CHECK (price >= 0),
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX compare_items_created ON compare_items (created_at DESC, id);
INSERT INTO compare_items (id, name, price, created_at)
SELECT gen_random_uuid(), 'item ' || n, (n % 1000) + 0.99, now() - n * interval '1 second'
FROM generate_series(1, 1000) AS n;
