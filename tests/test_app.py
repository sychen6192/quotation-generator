import os
import re
import time
from datetime import date

import pytest

import main
import company
from pdf_export import PdfExportError


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setitem(main.app.config, 'OUTPUT_DIR', tmp_path)
    monkeypatch.setitem(main.app.config, 'TESTING', True)
    monkeypatch.setattr(main.q, 'today_in_taiwan', lambda: date(2021, 7, 16))
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


def download_links(response):
    return re.findall(r'href="(/download/[^"]+)"', response.get_data(as_text=True))


def test_index(client):
    response = client.get('/')
    assert response.status_code == 200
    html = response.get_data(as_text=True)
    assert '聖大國際報價單產生器' in html
    assert 'name="product"' in html
    assert '<option value="陳聖尹"' in html


def test_get_generate_redirects_to_form(client):
    response = client.get('/generate')
    assert response.status_code == 302
    assert response.headers['Location'].endswith('/')


def test_generate_and_download(client, fake_pdf, valid_form, tmp_path):
    response = client.post('/generate', data=valid_form)
    assert response.status_code == 200
    html = response.get_data(as_text=True)
    assert '619,500.00' in html  # 總計
    links = download_links(response)
    assert [link.rsplit('/', 1)[1] for link in links] == ['pdf', 'xlsx']

    pdf = client.get(links[0])
    assert pdf.status_code == 200
    assert pdf.data == b'%PDF-1.4 fake'
    assert 'attachment' in pdf.headers['Content-Disposition']
    assert "filename*=UTF-8''" in pdf.headers['Content-Disposition']

    xlsx = client.get(links[1])
    assert xlsx.status_code == 200
    assert xlsx.data[:2] == b'PK'

    job_dir, = tmp_path.iterdir()
    assert sorted(p.name for p in job_dir.iterdir()) == ['測試股份有限公司報價單0716.pdf', '測試股份有限公司報價單0716.xlsx']


def test_each_request_gets_its_own_files(client, fake_pdf, valid_form, tmp_path):
    first = download_links(client.post('/generate', data=valid_form))
    second = download_links(client.post('/generate', data=dict(valid_form, cname='別人')))
    assert first != second
    assert len(list(tmp_path.iterdir())) == 2


def test_invalid_form_shows_errors_and_keeps_input(client, fake_pdf, valid_form, tmp_path):
    response = client.post('/generate', data=dict(valid_form, taxid='123', product='壞掉的一行'))
    assert response.status_code == 400
    html = response.get_data(as_text=True)
    assert '公司統編必須是 8 位數字' in html
    assert '壞掉的一行' in html            # textarea 保留原本輸入
    assert 'value="江美志"' in html
    assert fake_pdf == []
    assert list(tmp_path.iterdir()) == []


def test_user_input_is_escaped(client, fake_pdf, valid_form):
    response = client.post('/generate', data=dict(valid_form, cname='<script>alert(1)</script>'))
    html = response.get_data(as_text=True)
    assert '<script>alert(1)</script>' not in html
    assert '&lt;script&gt;' in html


def test_tax_id_lookup_fills_company(client, fake_pdf, fake_lookup, valid_form):
    calls = fake_lookup(company.CompanyInfo('24268597', '芯成科技股份有限公司', '新竹縣竹北市'))
    response = client.post('/generate', data=dict(valid_form, taxid='24268597', companyName='', companyAddress=''))
    assert response.status_code == 200
    assert calls == ['24268597']
    html = response.get_data(as_text=True)
    assert '芯成科技股份有限公司' in html and '新竹縣竹北市' in html


def test_manual_company_overrides_lookup(client, fake_pdf, fake_lookup, valid_form):
    fake_lookup(company.CompanyInfo('24268597', '芯成科技股份有限公司', '新竹縣竹北市'))
    response = client.post('/generate', data=dict(valid_form, taxid='24268597', companyName='手動公司', companyAddress=''))
    html = response.get_data(as_text=True)
    assert '手動公司' in html and '芯成科技' not in html and '新竹縣竹北市' in html


def test_no_lookup_when_company_fully_given(client, fake_pdf, fake_lookup, valid_form):
    calls = fake_lookup(None)
    client.post('/generate', data=dict(valid_form, taxid='24268597'))
    assert calls == []


def test_lookup_failure_without_company_name_asks_user(client, fake_pdf, fake_lookup, valid_form, tmp_path):
    fake_lookup(None)
    response = client.post('/generate', data=dict(valid_form, taxid='24268597', companyName='', companyAddress=''))
    assert response.status_code == 400
    assert '請手動填寫公司名稱' in response.get_data(as_text=True)
    assert list(tmp_path.iterdir()) == []


def test_lookup_failure_with_company_name_warns(client, fake_pdf, fake_lookup, valid_form):
    fake_lookup(None)
    response = client.post('/generate', data=dict(valid_form, taxid='24268597', companyAddress=''))
    assert response.status_code == 200
    assert '報價單上會留空' in response.get_data(as_text=True)


def test_pdf_failure_still_offers_excel(client, monkeypatch, valid_form):
    def broken(xlsx_path, pdf_path):
        raise PdfExportError('boom')
    monkeypatch.setattr(main, 'export_pdf', broken)
    response = client.post('/generate', data=valid_form)
    assert response.status_code == 200
    assert 'PDF 轉檔失敗' in response.get_data(as_text=True)
    links = download_links(response)
    assert [link.rsplit('/', 1)[1] for link in links] == ['xlsx']
    assert client.get(links[0]).status_code == 200
    assert client.get(links[0].replace('/xlsx', '/pdf')).status_code == 404


@pytest.mark.parametrize('path', [
    '/download/../main.py',
    '/download/..%2Fmain.py',
    '/download/' + '0' * 32 + '/pdf',
    '/download/' + '0' * 32 + '/py',
    '/download/not-a-job-id/pdf',
    '/download/' + '0' * 31 + '/../../main.py',
])
def test_download_rejects_unknown_or_unsafe_paths(client, path):
    assert client.get(path).status_code == 404


def test_expired_outputs_are_removed(client, fake_pdf, valid_form, tmp_path):
    old = tmp_path / ('a' * 32)
    old.mkdir()
    (old / 'x.pdf').write_bytes(b'old')
    long_ago = time.time() - main.app.config['OUTPUT_TTL_SECONDS'] - 60
    os.utime(old, (long_ago, long_ago))
    unrelated = tmp_path / 'keep-me'
    unrelated.mkdir()
    os.utime(unrelated, (long_ago, long_ago))

    client.post('/generate', data=valid_form)
    assert not old.exists()
    assert unrelated.exists()
    assert len([p for p in tmp_path.iterdir() if p.name != 'keep-me']) == 1


def test_request_too_large(client):
    response = client.post('/generate', data={'note': 'x' * (main.app.config['MAX_CONTENT_LENGTH'] + 1)})
    assert response.status_code == 413


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
    # 下載頁也一樣受保護
    assert client.get('/download/' + '0' * 32 + '/pdf', auth=credentials).status_code == (401 if expected == 401 else 404)
