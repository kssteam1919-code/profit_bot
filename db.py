import os
from decimal import Decimal

import asyncpg

pool: asyncpg.Pool = None

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    user_id BIGINT PRIMARY KEY,
    active_batch INT
);
CREATE TABLE IF NOT EXISTS batches (
    id SERIAL PRIMARY KEY,
    user_id BIGINT NOT NULL,
    name TEXT NOT NULL,
    created_at TIMESTAMPTZ DEFAULT now()
);
CREATE TABLE IF NOT EXISTS items (
    id SERIAL PRIMARY KEY,
    batch_id INT NOT NULL REFERENCES batches(id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    qty NUMERIC(12,3) NOT NULL DEFAULT 1,
    buy NUMERIC(14,2) NOT NULL,
    sell NUMERIC(14,2)
);
CREATE INDEX IF NOT EXISTS items_batch_idx ON items(batch_id);
"""


async def init():
    global pool
    pool = await asyncpg.create_pool(os.environ["DATABASE_URL"], min_size=1, max_size=4)
    await pool.execute(SCHEMA)


async def new_batch(uid: int, name: str) -> int:
    async with pool.acquire() as c:
        bid = await c.fetchval(
            "INSERT INTO batches(user_id, name) VALUES($1,$2) RETURNING id", uid, name)
        await c.execute(
            "INSERT INTO users(user_id, active_batch) VALUES($1,$2) "
            "ON CONFLICT (user_id) DO UPDATE SET active_batch=$2", uid, bid)
    return bid


async def active_batch(uid: int, default_name: str) -> dict:
    row = await pool.fetchrow(
        "SELECT b.id, b.name FROM users u JOIN batches b ON b.id=u.active_batch "
        "WHERE u.user_id=$1 AND b.user_id=$1", uid)
    if row:
        return dict(row)
    bid = await new_batch(uid, default_name)
    return {"id": bid, "name": default_name}


async def set_active(uid: int, bid: int) -> bool:
    ok = await pool.fetchval("SELECT 1 FROM batches WHERE id=$1 AND user_id=$2", bid, uid)
    if ok:
        await pool.execute(
            "INSERT INTO users(user_id, active_batch) VALUES($1,$2) "
            "ON CONFLICT (user_id) DO UPDATE SET active_batch=$2", uid, bid)
    return bool(ok)


async def list_batches(uid: int):
    return await pool.fetch(
        "SELECT b.id, b.name, COALESCE(SUM(i.qty),0) AS qty, COUNT(i.id) AS n "
        "FROM batches b LEFT JOIN items i ON i.batch_id=b.id "
        "WHERE b.user_id=$1 GROUP BY b.id ORDER BY b.id DESC LIMIT 15", uid)


async def delete_batch(uid: int, bid: int):
    await pool.execute("DELETE FROM batches WHERE id=$1 AND user_id=$2", bid, uid)
    await pool.execute(
        "UPDATE users SET active_batch=NULL WHERE user_id=$1 AND active_batch=$2", uid, bid)


async def add_items(bid: int, rows):
    """rows: [(name, qty, buy, sell|None)]"""
    async with pool.acquire() as c:
        await c.executemany(
            "INSERT INTO items(batch_id,name,qty,buy,sell) VALUES($1,$2,$3,$4,$5)",
            [(bid, n, q, b, s) for n, q, b, s in rows])


async def items(bid: int):
    return await pool.fetch("SELECT * FROM items WHERE batch_id=$1 ORDER BY id", bid)


async def set_sell(item_id: int, bid: int, sell: Decimal):
    await pool.execute("UPDATE items SET sell=$1 WHERE id=$2 AND batch_id=$3", sell, item_id, bid)


async def apply_markup(bid: int, pct: Decimal) -> int:
    r = await pool.execute(
        "UPDATE items SET sell=ROUND(buy*(1+$2/100)) WHERE batch_id=$1 AND sell IS NULL", bid, pct)
    return int(r.split()[-1])


async def next_unpriced(bid: int, skip):
    return await pool.fetchrow(
        "SELECT * FROM items WHERE batch_id=$1 AND sell IS NULL AND NOT (id = ANY($2::int[])) "
        "ORDER BY id LIMIT 1", bid, list(skip))


async def delete_last(bid: int):
    return await pool.fetchrow(
        "DELETE FROM items WHERE id=(SELECT MAX(id) FROM items WHERE batch_id=$1) RETURNING name", bid)


async def totals(bid: int):
    return await pool.fetchrow(
        """SELECT COUNT(*) AS n,
                  COALESCE(SUM(qty),0) AS qty,
                  COALESCE(SUM(buy*qty),0) AS cost_all,
                  COUNT(*) FILTER (WHERE sell IS NULL) AS unpriced,
                  COALESCE(SUM(qty) FILTER (WHERE sell IS NOT NULL),0) AS qty_priced,
                  COALESCE(SUM(buy*qty) FILTER (WHERE sell IS NOT NULL),0) AS cost_priced,
                  COALESCE(SUM(sell*qty),0) AS revenue
           FROM items WHERE batch_id=$1""", bid)
