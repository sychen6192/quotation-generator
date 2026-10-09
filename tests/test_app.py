import re
import sqlite3
from datetime import date
from decimal import Decimal

import pytest

import company
import main
import store
from pdf_export import PdfExportError

TODAY = date(2021, 7, 16)


@pytest.fixture
def data_dir(tmp_path):
    return tmp_path / 'data'


@pytest.fixture
def client(data_dir, monkeypatch):
    monkeypatch.setitem(main.app.config, 'DATA_DIR', str(data_dir))
    monkeypatch.setitem(main.app.config, 'TESTING', True)
    monkeypatch.setattr(main.q, 'today_in_taiwan', lambda: TODAY)
    return main.app.test_client()


@pytest.fixture
def fake_pdf(monkeypatch):
    calls = []

    def fake_export(xlsx_path, pdf_path):
        calls.append((xlsx_path, pdf_path))
        pdf_path.write_bytes(b'%PDF-1.4 fake')
    monkeypatch.setattr(main, 'export_pdf', fake_export)
    return calls


@pytest.fixture
def fake_lookup(monkeypatch):
    calls = []

    def install(result):
        def fake(tax_id):
            calls.append(tax_id)
            return result
        monkeypatch.setattr(main, 'lookup_company', fake)
        return calls
    return install


def generate(client, form, **kwargs):
    """送出表單，成功時回傳報價單號。"""
    response = client.post('/generate', data=form, **kwargs)
    assert response.status_code == 303, response.get_data(as_text=True)
    return re.search(r'/quotes/([^/?]+)', response.headers['Location']).group(1)


def html_of(response):
    return response.get_data(as_text=True)


def db(data_dir):
    conn = sqlite3.connect(str(data_dir / 'quotes.sqlite3'))
    conn.row_factory = sqlite3.Row
    return conn


# ---- 表單 ----

def test_index(client):
    response = client.get('/')
    assert response.status_code == 200
    html = html_of(response)
    assert '聖大國際報價單產生器' in html
    assert 'name="product"' in html
    assert '<option value="陳聖尹"' in html
    assert 'data-note-preset="運費另計"' in html
    assert 'data-blank="true"' in html


def test_get_generate_redirects_to_form(client):
    response = client.get('/generate')
    assert response.status_code == 302
    assert response.headers['Location'].endswith('/')


def test_invalid_form_shows_errors_and_keeps_input(client, fake_pdf, valid_form, data_dir):
    response = client.post('/generate', data=dict(valid_form, taxid='123', product='壞掉的一行'))
    assert response.status_code == 400
    html = html_of(response)
    assert '公司統編必須是 8 位數字' in html
    assert '壞掉的一行' in html            # textarea 保留原本輸入
    assert 'value="江美志"' in html
    assert 'data-blank="false"' in html
    assert fake_pdf == []
    assert not (data_dir / 'archive').exists()


def test_user_input_is_escaped(client, fake_pdf, valid_form):
    quote_no = generate(client, dict(valid_form, cname='<script>alert(1)</script>'))
    html = html_of(client.get(f'/quotes/{quote_no}'))
    assert '<script>alert(1)</script>' not in html
    assert '&lt;script&gt;' in html


def test_textarea_keeps_leading_blank_line(client, valid_form):
    response = client.post('/generate', data=dict(valid_form, product='\r\n壞掉的一行'))
    html = html_of(response)
    assert '品項第 2 行' in html
    assert 'placeholder="電腦主機,2,5500&#10;加購記憶體,2,400">\n\r\n壞掉的一行</textarea>' in html


def test_request_too_large(client):
    response = client.post('/generate', data={'note': 'x' * (main.app.config['MAX_CONTENT_LENGTH'] + 1)})
    assert response.status_code == 413


# ---- 產生報價單 ----

def test_generate_assigns_number_and_redirects(client, fake_pdf, valid_form, data_dir):
    response = client.post('/generate', data=valid_form)
    assert response.status_code == 303
    assert response.headers['Location'].endswith('/quotes/SD202107-001?created=1')

    detail = client.get('/quotes/SD202107-001?created=1')
    html = html_of(detail)
    assert detail.status_code == 200
    assert '報價單 SD202107-001 已經產生' in html
    assert '619,500.00' in html  # 總計 590,000 + 稅 29,500
    assert '2021/08/15' in html  # 有效期至

    folder = data_dir / 'archive' / 'SD202107-001'
    assert sorted(p.name for p in folder.iterdir()) == ['quotation.pdf', 'quotation.xlsx']
    assert len(fake_pdf) == 1


def test_numbers_increase(client, fake_pdf, valid_form):
    assert [generate(client, valid_form) for _ in range(3)] == ['SD202107-001', 'SD202107-002', 'SD202107-003']


def test_download_files(client, fake_pdf, valid_form):
    quote_no = generate(client, valid_form)
    pdf = client.get(f'/quotes/{quote_no}/download/pdf')
    assert pdf.status_code == 200
    assert pdf.data == b'%PDF-1.4 fake'
    disposition = pdf.headers['Content-Disposition']
    assert 'attachment' in disposition
    # 下載檔名是「測試股份有限公司報價單SD202107-001.pdf」
    assert "filename*=UTF-8''%E6%B8%AC%E8%A9%A6" in disposition and 'SD202107-001.pdf' in disposition

    xlsx = client.get(f'/quotes/{quote_no}/download/xlsx')
    assert xlsx.status_code == 200
    assert xlsx.data[:2] == b'PK'


def test_refreshing_result_page_does_not_create_another_quote(client, fake_pdf, valid_form, data_dir):
    quote_no = generate(client, valid_form)
    for _ in range(3):
        assert client.get(f'/quotes/{quote_no}?created=1').status_code == 200
    with db(data_dir) as conn:
        assert conn.execute('SELECT COUNT(*) FROM quotes').fetchone()[0] == 1


def test_snapshot_stored(client, fake_pdf, valid_form, data_dir):
    quote_no = generate(client, dict(valid_form, product='A,1,1234\n折扣,1,-34'))
    with db(data_dir) as conn:
        row = conn.execute('SELECT * FROM quotes WHERE quote_no = ?', (quote_no,)).fetchone()
        items = conn.execute('SELECT name, quantity, price FROM quote_items ORDER BY line_no').fetchall()
    assert (row['subtotal_cents'], row['tax_cents'], row['total_cents']) == (120000, 6000, 126000)
    assert [tuple(i) for i in items] == [('A', '1', '1234'), ('折扣', '1', '-34')]
    assert row['status'] == 'open'


def test_tax_id_lookup_fills_company(client, fake_pdf, fake_lookup, valid_form):
    calls = fake_lookup(company.CompanyInfo('24268597', '芯成科技股份有限公司', '新竹縣竹北市'))
    quote_no = generate(client, dict(valid_form, taxid='24268597', companyName='', companyAddress=''))
    assert calls == ['24268597']
    html = html_of(client.get(f'/quotes/{quote_no}'))
    assert '芯成科技股份有限公司' in html and '新竹縣竹北市' in html


def test_manual_company_overrides_lookup(client, fake_pdf, fake_lookup, valid_form):
    fake_lookup(company.CompanyInfo('24268597', '芯成科技股份有限公司', '新竹縣竹北市'))
    quote_no = generate(client, dict(valid_form, taxid='24268597', companyName='手動公司', companyAddress=''))
    html = html_of(client.get(f'/quotes/{quote_no}'))
    assert '手動公司' in html and '芯成科技' not in html and '新竹縣竹北市' in html


def test_no_lookup_when_company_fully_given(client, fake_pdf, fake_lookup, valid_form):
    calls = fake_lookup(None)
    generate(client, dict(valid_form, taxid='24268597'))
    assert calls == []


def test_previous_quote_fills_company_before_gcis(client, fake_pdf, fake_lookup, valid_form):
    calls = fake_lookup(None)
    generate(client, dict(valid_form, taxid='24268597', companyName='上次的公司', companyAddress='上次的地址'))
    quote_no = generate(client, dict(valid_form, taxid='24268597', companyName='', companyAddress=''))
    assert calls == []  # 從報價紀錄找到了，不用查政府 API
    html = html_of(client.get(f'/quotes/{quote_no}'))
    assert '上次的公司' in html and '上次的地址' in html


def test_lookup_failure_without_company_name_asks_user(client, fake_pdf, fake_lookup, valid_form, data_dir):
    fake_lookup(None)
    response = client.post('/generate', data=dict(valid_form, taxid='24268597', companyName='', companyAddress=''))
    assert response.status_code == 400
    assert '請手動填寫公司名稱' in html_of(response)
    assert not (data_dir / 'archive').exists()


def test_lookup_failure_with_company_name_warns(client, fake_pdf, fake_lookup, valid_form):
    fake_lookup(None)
    response = client.post('/generate', data=dict(valid_form, taxid='24268597', companyAddress=''))
    assert 'address_missing=1' in response.headers['Location']
    assert '公司地址，報價單上留空' in html_of(client.get(response.headers['Location']))


def test_pdf_failure_still_offers_excel_and_retries(client, monkeypatch, valid_form, data_dir):
    def broken(xlsx_path, pdf_path):
        raise PdfExportError('boom')
    monkeypatch.setattr(main, 'export_pdf', broken)
    response = client.post('/generate', data=valid_form)
    assert response.status_code == 303
    assert 'pdf_failed=1' in response.headers['Location']
    html = html_of(client.get(response.headers['Location']))
    assert 'PDF 轉檔失敗' in html and '產生並下載 PDF' in html

    assert client.get('/quotes/SD202107-001/download/xlsx').status_code == 200
    retry = client.get('/quotes/SD202107-001/download/pdf')
    assert retry.status_code == 302 and 'pdf_failed=1' in retry.headers['Location']

    def working(xlsx_path, pdf_path):
        pdf_path.write_bytes(b'%PDF-1.4 later')
    monkeypatch.setattr(main, 'export_pdf', working)
    later = client.get('/quotes/SD202107-001/download/pdf')
    assert later.status_code == 200 and later.data == b'%PDF-1.4 later'


def test_missing_archive_is_rebuilt_from_snapshot(client, fake_pdf, valid_form, data_dir):
    quote_no = generate(client, valid_form)
    folder = data_dir / 'archive' / quote_no
    for path in folder.iterdir():
        path.unlink()
    xlsx = client.get(f'/quotes/{quote_no}/download/xlsx')
    assert xlsx.status_code == 200 and xlsx.data[:2] == b'PK'
    assert client.get(f'/quotes/{quote_no}/download/pdf').status_code == 200


def test_workbook_failure_shows_form_and_uses_no_number(client, fake_pdf, valid_form, data_dir, monkeypatch):
    real_build = main.q.build_workbook

    def broken(quote):
        raise OSError('disk full')
    monkeypatch.setattr(main.q, 'build_workbook', broken)
    response = client.post('/generate', data=valid_form)
    assert response.status_code == 500
    assert '產生報價單失敗' in html_of(response)
    assert 'value="江美志"' in html_of(response)
    assert list((data_dir / 'archive').iterdir()) == []

    monkeypatch.setattr(main.q, 'build_workbook', real_build)
    assert generate(client, valid_form) == 'SD202107-001'  # 失敗的那次沒有用掉單號


def test_relative_data_dir_works(client, fake_pdf, valid_form, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setitem(main.app.config, 'DATA_DIR', 'relative-data')
    quote_no = generate(client, valid_form)
    assert client.get(f'/quotes/{quote_no}/download/pdf').status_code == 200
    assert (tmp_path / 'relative-data' / 'quotes.sqlite3').is_file()


@pytest.mark.parametrize('path', [
    '/quotes/SD202107-999',
    '/quotes/SD202107-999/download/pdf',
    '/quotes/..%2Fquotes.sqlite3/download/pdf',
    '/quotes/SD202107-001/download/py',
    '/quotes/SD202107-001/download/sqlite3',
    '/quotes/' + 'A' * 41,
    '/download/../main.py',
])
def test_unknown_or_unsafe_paths_are_404(client, fake_pdf, valid_form, path):
    generate(client, valid_form)
    assert client.get(path).status_code == 404


# ---- 歷史、搜尋、複製 ----

def test_history_lists_newest_first_and_searches(client, fake_pdf, valid_form):
    generate(client, dict(valid_form, companyName='甲公司', product='螢幕,1,100'))
    generate(client, dict(valid_form, companyName='乙公司', product='鍵盤,1,100', taxid='24268597'))
    html = html_of(client.get('/quotes'))
    assert html.index('SD202107-002') < html.index('SD202107-001')
    assert '共 2 筆' in html

    def found(keywords):
        return re.findall(r'>(SD\d{6}-\d{3})</a>', html_of(client.get('/quotes', query_string={'q': keywords})))
    assert found('甲公司') == ['SD202107-001']
    assert found('鍵盤') == ['SD202107-002']                 # 品名
    assert found('24268597') == ['SD202107-002']             # 統編
    assert found('SD202107-001') == ['SD202107-001']
    assert found('公司 螢幕') == ['SD202107-001']            # 多個關鍵字要全部符合
    assert found('%') == [] and found('_') == []             # 萬用字元當一般文字


def test_history_pagination(client, fake_pdf, valid_form, monkeypatch):
    monkeypatch.setattr(store, 'PAGE_SIZE', 2)
    for _ in range(5):
        generate(client, valid_form)
    page2 = html_of(client.get('/quotes?page=2'))
    assert re.findall(r'>(SD\d{6}-\d{3})</a>', page2) == ['SD202107-003', 'SD202107-002']
    assert 'aria-label="分頁"' in page2
    assert client.get('/quotes?page=abc').status_code == 200
    assert client.get('/quotes?page=-5').status_code == 200


def test_copy_prefills_form(client, fake_pdf, valid_form, data_dir):
    original = generate(client, dict(valid_form, tax='y', note='一\n二', product='A,1.50,-0\nB,2,100'))
    response = client.get(f'/quotes/{original}/copy')
    html = html_of(response)
    assert response.status_code == 200
    assert f'以 <a href="/quotes/{original}">{original}</a> 為範本' in html
    assert f'name="copied_from" value="{original}"' in html
    assert 'A,1.50,0\nB,2,100</textarea>' in html
    assert '<option value="y" selected>' in html
    assert 'name="dday" type="date" value=""' in html  # 出貨日不帶過去

    copy = generate(client, dict(valid_form, copied_from=original))
    assert copy == 'SD202107-002'
    assert '複製自' in html_of(client.get(f'/quotes/{copy}'))
    with db(data_dir) as conn:
        assert conn.execute('SELECT copied_from FROM quotes WHERE quote_no = ?', (copy,)).fetchone()[0] == original


def test_unknown_copied_from_is_ignored(client, fake_pdf, valid_form, data_dir):
    quote_no = generate(client, dict(valid_form, copied_from='../../etc'))
    with db(data_dir) as conn:
        assert conn.execute('SELECT copied_from FROM quotes WHERE quote_no = ?', (quote_no,)).fetchone()[0] is None


# ---- 狀態 ----

def test_status_changes_and_conflicts(client, fake_pdf, valid_form):
    quote_no = generate(client, valid_form)
    response = client.post(f'/quotes/{quote_no}/status', data={'current': 'open', 'status': 'won'})
    assert response.status_code == 303
    assert '已成交' in html_of(client.get(f'/quotes/{quote_no}'))

    stale = client.post(f'/quotes/{quote_no}/status', data={'current': 'open', 'status': 'lost'})
    assert stale.status_code == 409
    assert '狀態已經被改過了' in html_of(stale)

    assert client.post(f'/quotes/{quote_no}/status', data={'current': 'won', 'status': 'lost'}).status_code == 400
    assert client.post(f'/quotes/{quote_no}/status', data={'current': 'won', 'status': 'open'}).status_code == 303


def test_status_from_history_returns_to_same_page(client, fake_pdf, valid_form):
    quote_no = generate(client, valid_form)
    response = client.post(f'/quotes/{quote_no}/status',
                           data={'current': 'open', 'status': 'void', 'return_to': 'history', 'q': '測試', 'page': '1'})
    assert response.status_code == 303
    assert response.headers['Location'].endswith('/quotes?q=%E6%B8%AC%E8%A9%A6&page=1')


def test_expired_badge(client, fake_pdf, valid_form, monkeypatch):
    quote_no = generate(client, dict(valid_form, vday='7'))  # 有效到 2021/07/23
    monkeypatch.setattr(main.q, 'today_in_taiwan', lambda: date(2021, 7, 23))
    assert '已過期' not in html_of(client.get(f'/quotes/{quote_no}'))
    monkeypatch.setattr(main.q, 'today_in_taiwan', lambda: date(2021, 7, 24))
    assert '已過期' in html_of(client.get(f'/quotes/{quote_no}'))
    assert '已過期' in html_of(client.get('/quotes'))


# ---- 即時試算、客戶資料 ----

def test_preview(client):
    data = client.post('/preview', data={'product': 'A,1,1234\n壞掉', 'tax': 'n'}).get_json()
    assert data['subtotal'] == '1,234.00' and data['tax'] == '62.00' and data['total'] == '1,296.00'
    assert data['lines'] == ['1234.00']
    assert len(data['errors']) == 1 and '第 2 行' in data['errors'][0]

    included = client.post('/preview', data={'product': 'A,1,1050', 'tax': 'y'}).get_json()
    assert (included['tax'], included['total'], included['net'], included['tax_included']) == \
        ('50.00', '1,050.00', '1,000.00', True)


def test_customer_api_prefers_history(client, fake_pdf, fake_lookup, valid_form):
    calls = fake_lookup(company.CompanyInfo('24268597', 'GCIS 公司', 'GCIS 地址'))
    generate(client, dict(valid_form, taxid='24268597', product='螢幕,1,3000'))
    data = client.get('/api/customer?taxid=24268597').get_json()
    assert data['source'] == 'history'
    assert (data['cname'], data['cphone'], data['companyName']) == ('江美志', '03-5520981#1688', '測試股份有限公司')
    assert data['quote_no'] == 'SD202107-001'
    assert data['items'] == [{'name': '螢幕', 'price': '3000'}]
    assert calls == []


def test_customer_api_falls_back_to_gcis(client, fake_lookup):
    fake_lookup(company.CompanyInfo('24268597', 'GCIS 公司', 'GCIS 地址'))
    data = client.get('/api/customer?taxid=２４２６８５９７').get_json()
    assert (data['source'], data['companyName'], data['companyAddress']) == ('gcis', 'GCIS 公司', 'GCIS 地址')


def test_customer_api_not_found_and_invalid(client, fake_lookup):
    calls = fake_lookup(None)
    assert client.get('/api/customer?taxid=24268597').status_code == 404
    assert client.get('/api/customer?taxid=123').status_code == 400
    assert calls == ['24268597']


def test_item_suggestions_on_form(client, fake_pdf, valid_form):
    generate(client, dict(valid_form, product='螢幕,1,3000\n折扣,1,-100\n<b>品名</b>,1,5'))
    html = html_of(client.get('/'))
    assert '<option value="螢幕" data-price="3000">' in html
    assert 'value="折扣"' not in html            # 折扣列不當作建議
    assert '&lt;b&gt;品名&lt;/b&gt;' in html and '<b>品名</b>' not in html


# ---- 安全 ----

def test_cross_site_post_is_rejected(client, fake_pdf, valid_form):
    for header in ({'Origin': 'https://evil.example'}, {'Referer': 'https://evil.example/form'}):
        assert client.post('/generate', data=valid_form, headers=header).status_code == 403
    assert client.post('/generate', data=valid_form, headers={'Origin': 'http://localhost'}).status_code == 303


def test_login_not_required_by_default(client):
    assert client.get('/').status_code == 200


@pytest.mark.parametrize('credentials, expected', [
    (None, 401),
    (('admin', 'wrong'), 401),
    (('someone', '密碼'), 401),
    (('admin', '密碼'), 200),
])
def test_login_when_configured(client, monkeypatch, credentials, expected):
    monkeypatch.setitem(main.app.config, 'USERNAME', 'admin')
    monkeypatch.setitem(main.app.config, 'PASSWORD', '密碼')
    response = client.get('/', auth=credentials)
    assert response.status_code == expected
    if expected == 401:
        assert response.headers['WWW-Authenticate'].startswith('Basic')
    # 歷史報價和客戶資料也一樣受保護
    for path in ('/quotes', '/api/customer?taxid=24268597'):
        assert (client.get(path, auth=credentials).status_code == 401) == (expected == 401)


def test_money_rounds_half_up_like_excel():
    assert main.money(Decimal('0.125')) == '0.13'
    assert main.money(Decimal('11.025')) == '11.03'
    assert main.money(Decimal('1234567.5')) == '1,234,567.50'


# ---- 備份 ----

def test_backup_command(client, fake_pdf, valid_form, data_dir, tmp_path):
    quote_no = generate(client, valid_form)
    result = main.app.test_cli_runner().invoke(args=['backup', str(tmp_path / 'backups')])
    assert result.exit_code == 0, result.output
    backup_dir, = (tmp_path / 'backups').iterdir()
    assert (backup_dir / 'archive' / quote_no / 'quotation.xlsx').is_file()
    with sqlite3.connect(str(backup_dir / 'quotes.sqlite3')) as conn:
        assert conn.execute('SELECT quote_no FROM quotes').fetchall() == [(quote_no,)]


# ---- 第二輪審查修正 ----

def test_backup_refuses_missing_database(client, tmp_path, monkeypatch):
    monkeypatch.setitem(main.app.config, 'DATA_DIR', str(tmp_path / 'wrong-dir'))
    result = main.app.test_cli_runner().invoke(args=['backup', str(tmp_path / 'backups')])
    assert result.exit_code != 0
    assert '找不到資料庫' in result.output
    assert not (tmp_path / 'wrong-dir').exists()   # 不會順手建立一個空的資料庫
    assert not (tmp_path / 'backups').exists()


def test_reused_number_does_not_serve_old_files(client, monkeypatch, valid_form, data_dir):
    # 只還原資料庫時，archive 裡可能還有同單號的舊資料夾：不能把別的客戶的 PDF 當成新的
    stale = data_dir / 'archive' / 'SD202107-001'
    stale.mkdir(parents=True)
    (stale / 'quotation.pdf').write_bytes(b'%PDF old customer')

    def broken(xlsx_path, pdf_path):
        raise PdfExportError('boom')
    monkeypatch.setattr(main, 'export_pdf', broken)
    assert generate(client, valid_form) == 'SD202107-001'
    folder = data_dir / 'archive' / 'SD202107-001'
    assert sorted(p.name for p in folder.iterdir()) == ['quotation.xlsx']
    kept, = [p for p in (data_dir / 'archive').iterdir() if p.name.startswith('SD202107-001.orphaned-')]
    assert (kept / 'quotation.pdf').read_bytes() == b'%PDF old customer'   # 舊檔案搬到旁邊保留
    assert client.get('/quotes/SD202107-001/download/pdf').status_code == 302  # 重新轉檔（這裡會失敗）


def test_voided_quote_is_not_used_as_customer_memory(client, fake_pdf, fake_lookup, valid_form):
    fake_lookup(company.CompanyInfo('24268597', 'GCIS 公司', 'GCIS 地址'))
    good = generate(client, dict(valid_form, taxid='24268597', cname='正確的人'))
    bad = generate(client, dict(valid_form, taxid='24268597', cname='打錯的人', companyAddress='打錯的地址'))
    client.post(f'/quotes/{bad}/status', data={'current': 'open', 'status': 'void'})
    data = client.get('/api/customer?taxid=24268597').get_json()
    assert (data['cname'], data['quote_no']) == ('正確的人', good)


@pytest.mark.parametrize('query', [
    {'page': '1000000000000000000'},
    {'q': ' '.join(['a'] * 1000)},
    {'q': ' '.join(f'k{i}' for i in range(1000))},
])
def test_history_handles_extreme_queries(client, fake_pdf, valid_form, query):
    generate(client, valid_form)
    assert client.get('/quotes', query_string=query).status_code == 200


def test_huge_amounts_are_rejected_before_generating(client, valid_form, data_dir):
    response = client.post('/generate', data=dict(valid_form, product='大型專案,1000000,99999999'))
    assert response.status_code == 400
    assert '金額太大' in html_of(response)
    preview = client.post('/preview', data={'product': '大型專案,1000000,99999999', 'tax': 'n'}).get_json()
    assert any('金額太大' in e for e in preview['errors'])


def test_anti_framing_headers(client):
    for path in ('/', '/quotes'):
        response = client.get(path)
        assert response.headers['X-Frame-Options'] == 'DENY'
        assert "frame-ancestors 'none'" in response.headers['Content-Security-Policy']


def test_same_origin_behind_reverse_proxy(client, fake_pdf, valid_form, monkeypatch):
    # 代理把 Host 換成 127.0.0.1:8000，但有轉送 X-Forwarded-Host
    proxied = {'Origin': 'https://quotes.example.com', 'X-Forwarded-Host': 'quotes.example.com'}
    assert client.post('/generate', data=valid_form, headers=proxied, base_url='http://127.0.0.1:8000').status_code == 303
    # 代理什麼都沒轉送：要在 QUOTATION_ALLOWED_HOSTS 設定使用者看到的主機
    origin_only = {'Origin': 'https://quotes.example.com:8443'}
    assert client.post('/generate', data=valid_form, headers=origin_only, base_url='http://127.0.0.1:8000').status_code == 403
    monkeypatch.setitem(main.app.config, 'ALLOWED_ORIGIN_HOSTS', ['quotes.example.com:8443'])
    assert client.post('/generate', data=valid_form, headers=origin_only, base_url='http://127.0.0.1:8000').status_code == 303
    # 跨站的請求還是擋下來
    evil = {'Origin': 'https://evil.example', 'X-Forwarded-Host': 'quotes.example.com'}
    assert client.post('/generate', data=valid_form, headers=evil, base_url='http://127.0.0.1:8000').status_code == 403
