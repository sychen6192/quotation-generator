"""報價紀錄：存在一個 SQLite 檔（只用標準函式庫）。

每張報價單存一份快照（JSON），之後就算選項、客戶資料改了，舊報價單也不會變。
資料列不會刪除；不要的報價單改成「作廢」，所以單號永遠不會重複使用。
"""
from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

import quotation as q

SCHEMA_VERSION = 1
_SCHEMA = """
CREATE TABLE IF NOT EXISTS quotes (
    id INTEGER PRIMARY KEY,
    quote_no TEXT NOT NULL UNIQUE,
    no_prefix TEXT NOT NULL,
    no_seq INTEGER NOT NULL,
    quote_date TEXT NOT NULL,
    valid_until TEXT NOT NULL,
    seller TEXT NOT NULL,
    customer_name TEXT NOT NULL,
    phone TEXT NOT NULL,
    tax_id TEXT NOT NULL DEFAULT '',
    company_name TEXT NOT NULL DEFAULT '',
    company_address TEXT NOT NULL DEFAULT '',
    tax_included INTEGER NOT NULL,
    subtotal_cents INTEGER NOT NULL,
    tax_cents INTEGER NOT NULL,
    total_cents INTEGER NOT NULL,
    status TEXT NOT NULL DEFAULT 'open',
    status_changed_at TEXT,
    copied_from TEXT,
    data TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE (no_prefix, no_seq)
);
CREATE TABLE IF NOT EXISTS quote_items (
    quote_id INTEGER NOT NULL REFERENCES quotes(id) ON DELETE CASCADE,
    line_no INTEGER NOT NULL,
    name TEXT NOT NULL,
    quantity TEXT NOT NULL,
    price TEXT NOT NULL,
    PRIMARY KEY (quote_id, line_no)
);
CREATE INDEX IF NOT EXISTS quotes_tax_id ON quotes (tax_id, id);
CREATE INDEX IF NOT EXISTS quote_items_name ON quote_items (name);
"""

STATUSES = {'open': '已報價', 'won': '已成交', 'lost': '未成交', 'void': '作廢'}
# 只能從「已報價」改成其他狀態，或從其他狀態改回「已報價」
_ALLOWED_TRANSITIONS = {('open', s) for s in STATUSES if s != 'open'} | {(s, 'open') for s in STATUSES if s != 'open'}

PAGE_SIZE = 25
MAX_KEYWORDS = 20  # 每個關鍵字多一組 SQL 條件，太多會超過 SQLite 的上限


class StatusConflict(Exception):
    """狀態已經被別人（或另一個分頁）改掉了。"""


@dataclass(frozen=True)
class QuoteRecord:
    quote: q.Quotation
    status: str
    copied_from: Optional[str]
    created_at: str

    @property
    def status_label(self) -> str:
        return STATUSES.get(self.status, self.status)

    def is_expired(self, today: date) -> bool:
        # 過期不另外存，查詢時用有效日期判斷，所以不需要排程
        return self.status == 'open' and self.quote.valid_until < today


def connect(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    # isolation_level=None：自己控制交易，取號時用 BEGIN IMMEDIATE 鎖住寫入
    conn = sqlite3.connect(str(path), timeout=10, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA foreign_keys = ON')
    if conn.execute('PRAGMA user_version').fetchone()[0] < SCHEMA_VERSION:
        conn.executescript(_SCHEMA)
        conn.execute(f'PRAGMA user_version = {SCHEMA_VERSION}')
    return conn


def _cents(value: Decimal) -> int:
    return int(value.quantize(q.CENT) * 100)


def create_quote(conn: sqlite3.Connection, quote: q.Quotation,
                 write_files: Callable[[q.Quotation], None], copied_from: Optional[str] = None) -> str:
    """取號並存檔。write_files 在交易裡執行；它失敗時整筆取消，單號也不會被用掉。

    取號用 BEGIN IMMEDIATE 鎖住資料庫，多個請求、多個行程同時產生也不會拿到同一個號碼。
    """
    prefix = q.quote_no_prefix(quote.quote_date)
    conn.execute('BEGIN IMMEDIATE')
    try:
        seq = conn.execute('SELECT COALESCE(MAX(no_seq), 0) + 1 FROM quotes WHERE no_prefix = ?',
                           (prefix,)).fetchone()[0]
        quote.quote_no = q.format_quote_no(quote.quote_date, seq)
        write_files(quote)
        totals = quote.totals
        cursor = conn.execute(
            """INSERT INTO quotes (quote_no, no_prefix, no_seq, quote_date, valid_until, seller,
                   customer_name, phone, tax_id, company_name, company_address, tax_included,
                   subtotal_cents, tax_cents, total_cents, copied_from, data, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (quote.quote_no, prefix, seq, quote.quote_date.isoformat(), quote.valid_until.isoformat(),
             quote.seller, quote.customer_name, quote.phone, quote.tax_id, quote.company_name,
             quote.company_address, int(quote.tax_included), _cents(totals.subtotal),
             _cents(totals.tax), _cents(totals.total), copied_from,
             json.dumps(quote.to_dict(), ensure_ascii=False), datetime.now(q.TAIPEI).isoformat(timespec='seconds')))
        conn.executemany(
            'INSERT INTO quote_items (quote_id, line_no, name, quantity, price) VALUES (?, ?, ?, ?, ?)',
            [(cursor.lastrowid, i, p.name, str(p.quantity), str(p.price))
             for i, p in enumerate(quote.products, start=1)])
        conn.execute('COMMIT')
    except BaseException:
        conn.execute('ROLLBACK')
        quote.quote_no = ''
        raise
    return quote.quote_no


def _record(row: sqlite3.Row) -> QuoteRecord:
    return QuoteRecord(q.Quotation.from_dict(json.loads(row['data'])), row['status'],
                       row['copied_from'], row['created_at'])


def get_quote(conn: sqlite3.Connection, quote_no: str) -> Optional[QuoteRecord]:
    row = conn.execute('SELECT * FROM quotes WHERE quote_no = ?', (quote_no,)).fetchone()
    return _record(row) if row else None


def _like(keyword: str) -> str:
    # 使用者輸入的 % 和 _ 要當成一般字元
    return '%' + keyword.replace('\\', '\\\\').replace('%', '\\%').replace('_', '\\_') + '%'


def search_quotes(conn: sqlite3.Connection, keywords: str = '', page: int = 1
                  ) -> Tuple[List[QuoteRecord], int]:
    """依單號、客戶、公司、統編、電話或品名搜尋；多個關鍵字要全部符合。回傳 (這頁的資料, 總筆數)。"""
    where, params = [], []
    for keyword in list(dict.fromkeys(keywords.split()))[:MAX_KEYWORDS]:
        pattern = _like(keyword)
        where.append(
            "((quote_no || ' ' || customer_name || ' ' || company_name || ' ' || tax_id || ' ' || phone)"
            " LIKE ? ESCAPE '\\' OR EXISTS (SELECT 1 FROM quote_items i"
            " WHERE i.quote_id = quotes.id AND i.name LIKE ? ESCAPE '\\'))")
        params += [pattern, pattern]
    sql_where = ' WHERE ' + ' AND '.join(where) if where else ''
    total = conn.execute(f'SELECT COUNT(*) FROM quotes{sql_where}', params).fetchone()[0]
    rows = conn.execute(f'SELECT * FROM quotes{sql_where} ORDER BY id DESC LIMIT ? OFFSET ?',
                        params + [PAGE_SIZE, (max(page, 1) - 1) * PAGE_SIZE]).fetchall()
    return [_record(row) for row in rows], total


def set_status(conn: sqlite3.Connection, quote_no: str, current: str, new: str) -> None:
    """只有在狀態仍是 current 時才改，避免兩個分頁互相覆蓋。"""
    if (current, new) not in _ALLOWED_TRANSITIONS:
        raise ValueError(f'不能從「{STATUSES.get(current, current)}」改成「{STATUSES.get(new, new)}」')
    changed = conn.execute(
        'UPDATE quotes SET status = ?, status_changed_at = ? WHERE quote_no = ? AND status = ?',
        (new, datetime.now(q.TAIPEI).isoformat(timespec='seconds'), quote_no, current)).rowcount
    if not changed:
        raise StatusConflict(quote_no)


def find_customer(conn: sqlite3.Connection, tax_id: str) -> Optional[Dict[str, str]]:
    """這個統編最近一次報價的客戶資料（作廢的不算，通常是打錯了）。"""
    row = conn.execute(
        """SELECT quote_no, quote_date, customer_name, phone, company_name, company_address
           FROM quotes WHERE tax_id = ? AND status != 'void' ORDER BY id DESC LIMIT 1""", (tax_id,)).fetchone()
    if not row:
        return None
    return {'quote_no': row['quote_no'], 'quote_date': row['quote_date'], 'cname': row['customer_name'],
            'cphone': row['phone'], 'companyName': row['company_name'],
            'companyAddress': row['company_address']}


def recent_items(conn: sqlite3.Connection, tax_id: str = '', limit: int = 200) -> List[Dict[str, str]]:
    """報價過的品項和最近一次的單價，最常用的排前面；有統編時優先用報給這個客戶的價格。

    折扣列（單價為負）不列入。
    """
    rows = conn.execute(
        """WITH ranked AS (
               SELECT i.name, i.price, q.tax_id,
                      ROW_NUMBER() OVER (PARTITION BY i.name ORDER BY q.id DESC) AS global_rank,
                      ROW_NUMBER() OVER (PARTITION BY i.name, q.tax_id = ? ORDER BY q.id DESC) AS customer_rank,
                      COUNT(*) OVER (PARTITION BY i.name) AS uses,
                      MAX(q.id) OVER (PARTITION BY i.name) AS last_id
               FROM quote_items i JOIN quotes q ON q.id = i.quote_id
               WHERE q.status != 'void' AND CAST(i.price AS REAL) >= 0
           )
           SELECT g.name, g.price, c.price AS customer_price
           FROM ranked g
           LEFT JOIN ranked c ON c.name = g.name AND c.customer_rank = 1 AND c.tax_id = ? AND ? != ''
           WHERE g.global_rank = 1
           ORDER BY g.uses DESC, g.last_id DESC
           LIMIT ?""", (tax_id, tax_id, tax_id, limit)).fetchall()
    return [{'name': r['name'], 'price': r['customer_price'] or r['price']} for r in rows]


def backup(conn: sqlite3.Connection, destination: Path) -> None:
    """用 SQLite 的線上備份，資料庫使用中也能安全複製（Windows 不需要另外裝 sqlite3）。"""
    destination.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(str(destination))) as target:
        conn.backup(target)
