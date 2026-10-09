import threading
from contextlib import closing
from datetime import date
from decimal import Decimal

import pytest

import quotation as q
import store


def make_quote(quote_date=date(2026, 10, 9), tax_id='', products=(('A', '1', '100'),), **kwargs):
    fields = dict(
        customer_name='江美志', phone='03-1234567', tax_included=False, seller='陳聖尹',
        delivery_method='郵局', payment_method='匯款', valid_days=30, quote_date=quote_date,
        tax_id=tax_id, company_name='測試公司', company_address='新竹市',
        products=[q.Product(n, Decimal(qty), Decimal(price)) for n, qty, price in products],
    )
    fields.update(kwargs)
    return q.Quotation(**fields)


@pytest.fixture
def conn(tmp_path):
    connection = store.connect(tmp_path / 'db' / 'quotes.sqlite3')
    yield connection
    connection.close()


def no_files(_quote):
    pass


def test_numbers_are_sequential_per_month(conn):
    numbers = [store.create_quote(conn, make_quote(d), no_files) for d in (
        date(2026, 10, 1), date(2026, 10, 31), date(2026, 11, 1), date(2026, 11, 2))]
    assert numbers == ['SD202610-001', 'SD202610-002', 'SD202611-001', 'SD202611-002']


def test_numbers_restart_after_year_end(conn):
    numbers = [store.create_quote(conn, make_quote(d), no_files) for d in (
        date(2026, 12, 31), date(2026, 12, 31), date(2027, 1, 1))]
    assert numbers == ['SD202612-001', 'SD202612-002', 'SD202701-001']


def test_failure_rolls_back_and_frees_the_number(conn):
    quote = make_quote()

    def broken(numbered):
        assert numbered.quote_no == 'SD202610-001'
        raise OSError('disk full')
    with pytest.raises(OSError):
        store.create_quote(conn, quote, broken)
    assert quote.quote_no == ''
    assert conn.execute('SELECT COUNT(*) FROM quotes').fetchone()[0] == 0
    assert store.create_quote(conn, make_quote(), no_files) == 'SD202610-001'


def test_concurrent_creation_gets_distinct_numbers(tmp_path):
    path = tmp_path / 'quotes.sqlite3'
    store.connect(path).close()
    numbers, errors = [], []

    def worker():
        try:
            with closing(store.connect(path)) as connection:
                numbers.append(store.create_quote(connection, make_quote(), no_files))
        except Exception as e:  # pragma: no cover - 失敗時要看到原因
            errors.append(e)
    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == []
    assert sorted(numbers) == [f'SD202610-{n:03d}' for n in range(1, 9)]


def test_snapshot_round_trip(conn):
    quote = make_quote(products=(('A', '1.50', '12.25'), ('折扣', '1', '-5')), tax_included=True,
                       ship_date=date(2026, 10, 20), note='一\n二', tax_id='24268597')
    quote_no = store.create_quote(conn, quote, no_files)
    record = store.get_quote(conn, quote_no)
    assert record.quote == quote
    assert record.status == 'open' and record.status_label == '已報價'
    assert store.get_quote(conn, 'SD999999-999') is None


def test_snapshot_does_not_change_when_options_change(conn, monkeypatch):
    quote_no = store.create_quote(conn, make_quote(seller='已離職的業務'), no_files)
    monkeypatch.setattr(q, 'SELLERS', ('新業務',))
    assert store.get_quote(conn, quote_no).quote.seller == '已離職的業務'


def test_amounts_stored_in_cents(conn):
    quote_no = store.create_quote(conn, make_quote(products=(('A', '1', '1234'), ('B', '3', '0.333'))), no_files)
    row = conn.execute('SELECT subtotal_cents, tax_cents, total_cents FROM quotes WHERE quote_no = ?',
                       (quote_no,)).fetchone()
    # 1234 + 1.00（3 × 0.333 = 0.999 → 1.00），稅 61.75 → 62
    assert tuple(row) == (123500, 6200, 129700)


def test_search(conn):
    store.create_quote(conn, make_quote(company_name='甲公司', products=(('螢幕', '1', '1'),)), no_files)
    store.create_quote(conn, make_quote(company_name='乙公司', tax_id='24268597', products=(('100%棉_布', '1', '1'),)), no_files)

    def found(keywords):
        records, total = store.search_quotes(conn, keywords)
        assert total == len(records)
        return [r.quote.quote_no for r in records]
    assert found('') == ['SD202610-002', 'SD202610-001']
    assert found('甲') == ['SD202610-001']
    assert found('螢幕') == ['SD202610-001']
    assert found('2426') == ['SD202610-002']
    assert found('100%') == ['SD202610-002']   # % 照字面比對
    assert found('%') == ['SD202610-002']
    assert found('棉_布') == ['SD202610-002']
    assert found('棉x布') == []
    assert found('公司 不存在') == []


def test_search_pagination(conn, monkeypatch):
    monkeypatch.setattr(store, 'PAGE_SIZE', 2)
    for _ in range(5):
        store.create_quote(conn, make_quote(), no_files)
    records, total = store.search_quotes(conn, '', page=3)
    assert total == 5 and [r.quote.quote_no for r in records] == ['SD202610-001']
    assert store.search_quotes(conn, '', page=0)[0][0].quote.quote_no == 'SD202610-005'


@pytest.mark.parametrize('current, new', [('open', 'won'), ('open', 'lost'), ('open', 'void')])
def test_status_transitions(conn, current, new):
    quote_no = store.create_quote(conn, make_quote(), no_files)
    store.set_status(conn, quote_no, current, new)
    assert store.get_quote(conn, quote_no).status == new
    store.set_status(conn, quote_no, new, 'open')
    assert store.get_quote(conn, quote_no).status == 'open'


def test_status_guards(conn):
    quote_no = store.create_quote(conn, make_quote(), no_files)
    with pytest.raises(ValueError):
        store.set_status(conn, quote_no, 'open', 'open')
    with pytest.raises(ValueError):
        store.set_status(conn, quote_no, 'open', 'paid')
    store.set_status(conn, quote_no, 'open', 'won')
    with pytest.raises(ValueError):
        store.set_status(conn, quote_no, 'won', 'lost')
    with pytest.raises(store.StatusConflict):
        store.set_status(conn, quote_no, 'open', 'void')  # 已經不是「已報價」了


def test_expired_is_derived(conn):
    quote_no = store.create_quote(conn, make_quote(valid_days=7), no_files)  # 有效到 10/16
    record = store.get_quote(conn, quote_no)
    assert not record.is_expired(date(2026, 10, 16))
    assert record.is_expired(date(2026, 10, 17))
    store.set_status(conn, quote_no, 'open', 'won')
    assert not store.get_quote(conn, quote_no).is_expired(date(2027, 1, 1))  # 成交了就不算過期


def test_find_customer_returns_latest(conn):
    assert store.find_customer(conn, '24268597') is None
    store.create_quote(conn, make_quote(tax_id='24268597', customer_name='舊聯絡人'), no_files)
    store.create_quote(conn, make_quote(tax_id='24268597', customer_name='新聯絡人', phone='0911'), no_files)
    store.create_quote(conn, make_quote(tax_id='12345678', customer_name='別家'), no_files)
    found = store.find_customer(conn, '24268597')
    assert (found['cname'], found['cphone'], found['quote_no']) == ('新聯絡人', '0911', 'SD202610-002')


def test_recent_items(conn):
    store.create_quote(conn, make_quote(products=(('螢幕', '1', '3000'), ('滑鼠', '1', '300'))), no_files)
    store.create_quote(conn, make_quote(products=(('螢幕', '1', '2900'), ('折扣', '1', '-100'))), no_files)
    store.create_quote(conn, make_quote(tax_id='24268597', products=(('螢幕', '1', '2500'),)), no_files)
    void_no = store.create_quote(conn, make_quote(products=(('作廢品', '1', '1'),)), no_files)
    store.set_status(conn, void_no, 'open', 'void')

    items = store.recent_items(conn)
    # 螢幕用最多次排第一，價格是最近一次；折扣列和作廢單的品項不列入
    assert items == [{'name': '螢幕', 'price': '2500'}, {'name': '滑鼠', 'price': '300'}]
    # 其他客戶不會拿到 24268597 的議價
    assert store.recent_items(conn, '99999999')[0] == {'name': '螢幕', 'price': '2500'}


def test_recent_items_prefers_customer_price(conn):
    store.create_quote(conn, make_quote(tax_id='24268597', products=(('螢幕', '1', '2500'),)), no_files)
    store.create_quote(conn, make_quote(tax_id='11111111', products=(('螢幕', '1', '3200'),)), no_files)
    assert store.recent_items(conn)[0]['price'] == '3200'
    assert store.recent_items(conn, '24268597')[0]['price'] == '2500'
    assert store.recent_items(conn, '22222222')[0]['price'] == '3200'


def test_backup(conn, tmp_path):
    quote_no = store.create_quote(conn, make_quote(), no_files)
    target = tmp_path / 'backup' / 'quotes.sqlite3'
    store.backup(conn, target)
    with closing(store.connect(target)) as copy:
        assert store.get_quote(copy, quote_no) is not None


def test_find_customer_skips_void(conn):
    store.create_quote(conn, make_quote(tax_id='24268597', customer_name='正確的人'), no_files)
    wrong = store.create_quote(conn, make_quote(tax_id='24268597', customer_name='打錯的人'), no_files)
    store.set_status(conn, wrong, 'open', 'void')
    assert store.find_customer(conn, '24268597')['cname'] == '正確的人'


def test_search_ignores_duplicate_and_excess_keywords(conn):
    store.create_quote(conn, make_quote(company_name='甲公司'), no_files)
    assert store.search_quotes(conn, ' '.join(['甲'] * 500))[1] == 1
    many = '甲 ' + ' '.join(f'k{i}' for i in range(500))
    store.search_quotes(conn, many)  # 不會超過 SQLite 的運算式上限
