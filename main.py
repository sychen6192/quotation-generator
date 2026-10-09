import hmac
import logging
import os
import re
import shutil
import unicodedata
from contextlib import closing
from datetime import datetime
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path
from urllib.parse import urlsplit

import click
from flask import (Flask, Response, abort, g, jsonify, redirect, render_template, request,
                   send_file, url_for)

import quotation as q
import store
from company import TAX_ID_PATTERN, lookup_company
from pdf_export import PdfExportError, export_pdf

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(name)s: %(message)s')
logger = logging.getLogger(__name__)

app = Flask(__name__)
app.config.update(
    # 資料庫（quotes.sqlite3）和每張報價單的 Excel / PDF（archive/）都放這裡，記得定期備份
    DATA_DIR=os.environ.get('QUOTATION_DATA_DIR') or str(Path(__file__).with_name('data')),
    MAX_CONTENT_LENGTH=64 * 1024,
    # 兩個都設定時整個網站需要帳號密碼（HTTP Basic Auth，請搭配 HTTPS）
    USERNAME=os.environ.get('QUOTATION_USERNAME'),
    PASSWORD=os.environ.get('QUOTATION_PASSWORD'),
)
if not (app.config['USERNAME'] and app.config['PASSWORD']):
    logger.warning('沒有設定 QUOTATION_USERNAME / QUOTATION_PASSWORD：任何連得到的人都能看到報價紀錄')

# 下拉選單的 (value, 顯示文字)
FORM_OPTIONS = dict(
    valid_days=[(d, f'{d} 天') for d in q.VALID_DAYS],
    sellers=[(s, s) for s in q.SELLERS],
    delivery_methods=[(s, s) for s in q.DELIVERY_METHODS],
    payment_methods=[(s, s) for s in q.PAYMENT_METHODS],
    tax_options=q.TAX_OPTIONS,
    max_products=q.MAX_PRODUCTS,
    note_presets=q.NOTE_PRESETS,
)
DOWNLOAD_TYPES = {'pdf', 'xlsx'}
_QUOTE_NO = re.compile(r'[A-Za-z0-9-]{1,40}')
# 磁碟上一律用 ASCII 檔名（LANG=C 的 Linux/mod_wsgi 存不了中文檔名），下載時才用中文名稱
FILE_STEM = 'quotation'


@app.template_filter('money')
def money(value: Decimal) -> str:
    # 和 Excel 一樣四捨五入（Decimal 預設是銀行家捨入，0.125 會變 0.12）
    return f'{value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP):,.2f}'


@app.template_filter('number')
def number(value: Decimal) -> str:
    return f'{value.normalize():,f}'


@app.before_request
def require_login():
    username, password = app.config['USERNAME'], app.config['PASSWORD']
    if not (username and password):
        return None
    auth = request.authorization
    if (auth and auth.type == 'basic'
            and hmac.compare_digest((auth.username or '').encode(), username.encode())
            and hmac.compare_digest((auth.password or '').encode(), password.encode())):
        return None
    return Response('需要登入', 401, {'WWW-Authenticate': 'Basic realm="quotation", charset="UTF-8"'})


@app.before_request
def require_same_origin():
    # 瀏覽器在跨站送出表單時也會自動帶上 Basic Auth 帳密，所以 POST 要確認是從本站送出的
    if request.method != 'POST':
        return None
    source = request.headers.get('Origin') or request.headers.get('Referer')
    if source and urlsplit(source).netloc != request.host:
        abort(403)
    return None


def data_dir() -> Path:
    # 轉成絕對路徑：send_file 會把相對路徑接在 app.root_path 後面，和寫檔時的工作目錄不同
    return Path(app.config['DATA_DIR']).resolve()


def get_db():
    if 'db' not in g:
        g.db = store.connect(data_dir() / 'quotes.sqlite3')
    return g.db


@app.teardown_appcontext
def close_db(_exc):
    db = g.pop('db', None)
    if db is not None:
        db.close()


def quote_dir(quote_no: str) -> Path:
    return data_dir() / 'archive' / quote_no


def get_record_or_404(quote_no: str) -> store.QuoteRecord:
    record = store.get_quote(get_db(), quote_no) if _QUOTE_NO.fullmatch(quote_no) else None
    if record is None:
        abort(404)
    return record


def render_form(form=None, errors=(), status=200, copied_from=None):
    return render_template('form.html', form=form or {}, errors=errors, copied_from=copied_from,
                           items=store.recent_items(get_db()), **FORM_OPTIONS), status


@app.get('/')
def index():
    return render_form()


@app.get('/generate')
def generate_get():
    return redirect(url_for('index'))


@app.post('/generate')
def generate():
    copied_from = request.form.get('copied_from') or None
    try:
        quote = q.parse_form(request.form)
    except q.QuotationError as e:
        return render_form(request.form, e.errors, 400, copied_from)
    conn = get_db()

    address_missing = False
    if quote.tax_id and not (quote.company_name and quote.company_address):
        # 先用這個統編上次報價的資料，查不到再問政府 API；手動輸入的永遠優先
        known = store.find_customer(conn, quote.tax_id)
        if known:
            quote.company_name = quote.company_name or known['companyName']
            quote.company_address = quote.company_address or known['companyAddress']
    if quote.tax_id and not (quote.company_name and quote.company_address):
        company = lookup_company(quote.tax_id)
        if company:
            quote.company_name = quote.company_name or company.name
            quote.company_address = quote.company_address or company.address
        elif not quote.company_name:
            return render_form(request.form, [
                f'查不到統編 {quote.tax_id} 的公司資料（或政府 API 暫時連不上），請手動填寫公司名稱與地址'
            ], 400, copied_from)
        else:
            address_missing = True

    if copied_from and not (_QUOTE_NO.fullmatch(copied_from) and store.get_quote(conn, copied_from)):
        copied_from = None

    folders = []

    def write_files(numbered: q.Quotation) -> None:
        folder = quote_dir(numbered.quote_no)
        folders.append(folder)
        folder.mkdir(parents=True, exist_ok=True)
        q.build_workbook(numbered).save(folder / f'{FILE_STEM}.xlsx')

    try:
        quote_no = store.create_quote(conn, quote, write_files, copied_from)
    except Exception:
        logger.exception('產生報價單失敗')
        for folder in folders:
            shutil.rmtree(folder, ignore_errors=True)
        return render_form(request.form, ['產生報價單失敗，請檢查輸入的資料或稍後再試'], 500, copied_from)

    # PDF 可能要好幾秒，放在交易外面做；失敗的話 Excel 仍然可以下載，之後也能重試
    pdf_ok = export_archived_pdf(quote_no)
    params = {'created': 1}
    if address_missing:
        params['address_missing'] = 1
    if not pdf_ok:
        params['pdf_failed'] = 1
    # 產生完導到報價單頁（POST/Redirect/GET），重新整理不會再產生一張
    return redirect(url_for('quote_detail', quote_no=quote_no, **params), 303)


def export_archived_pdf(quote_no: str) -> bool:
    folder = quote_dir(quote_no)
    try:
        export_pdf(folder / f'{FILE_STEM}.xlsx', folder / f'{FILE_STEM}.pdf')
    except PdfExportError:
        logger.exception('PDF 轉檔失敗（%s）', quote_no)
        return False
    return True


@app.get('/quotes')
def quote_list():
    keywords = request.args.get('q', '').strip()
    page = max(request.args.get('page', 1, type=int) or 1, 1)
    records, total = store.search_quotes(get_db(), keywords, page)
    pages = max(1, -(-total // store.PAGE_SIZE))
    return render_template('history.html', records=records, total=total, keywords=keywords,
                           page=page, pages=pages, today=q.today_in_taiwan(), statuses=store.STATUSES)


@app.get('/quotes/<quote_no>')
def quote_detail(quote_no, status=200, notice=None):
    record = get_record_or_404(quote_no)
    return render_template('quote.html', record=record, quote=record.quote, totals=record.quote.totals,
                           has_pdf=(quote_dir(quote_no) / f'{FILE_STEM}.pdf').is_file(),
                           today=q.today_in_taiwan(), statuses=store.STATUSES, notice=notice), status


@app.get('/quotes/<quote_no>/download/<file_type>')
def download(quote_no, file_type):
    if file_type not in DOWNLOAD_TYPES:
        abort(404)
    record = get_record_or_404(quote_no)
    folder = quote_dir(quote_no)
    xlsx_path = folder / f'{FILE_STEM}.xlsx'
    if not xlsx_path.is_file():
        # 歸檔檔案不見了（例如只還原了資料庫）：用存下來的快照重建
        folder.mkdir(parents=True, exist_ok=True)
        q.build_workbook(record.quote).save(xlsx_path)
    path = folder / f'{FILE_STEM}.{file_type}'
    if file_type == 'pdf' and not path.is_file() and not export_archived_pdf(quote_no):
        return redirect(url_for('quote_detail', quote_no=quote_no, pdf_failed=1))
    return send_file(path, as_attachment=True, download_name=f'{record.quote.file_stem()}.{file_type}')


@app.get('/quotes/<quote_no>/copy')
def copy_quote(quote_no):
    record = get_record_or_404(quote_no)
    return render_form(record.quote.to_form(), copied_from=quote_no)


@app.post('/quotes/<quote_no>/status')
def change_status(quote_no):
    get_record_or_404(quote_no)
    try:
        store.set_status(get_db(), quote_no, request.form.get('current', ''), request.form.get('status', ''))
    except ValueError:
        abort(400)
    except store.StatusConflict:
        return quote_detail(quote_no, status=409, notice='狀態已經被改過了，請確認後再試一次')
    if request.form.get('return_to') == 'history':
        return redirect(url_for('quote_list', q=request.form.get('q', ''),
                                page=request.form.get('page', 1, type=int)), 303)
    return redirect(url_for('quote_detail', quote_no=quote_no), 303)


@app.post('/preview')
def preview():
    """輸入品項時即時試算；和產生報價單用同一套程式，網頁上不另外寫一份計算。"""
    result = q.preview(request.form)
    totals = result.pop('totals')
    return jsonify({**result, 'subtotal': money(totals.subtotal), 'tax': money(totals.tax),
                    'total': money(totals.total), 'net': money(totals.net),
                    'tax_included': totals.tax_included})


@app.get('/api/customer')
def api_customer():
    """填統編時帶出客戶資料：先找上次報價，沒有再查政府 API。"""
    tax_id = unicodedata.normalize('NFKC', request.args.get('taxid', '').strip())
    if not TAX_ID_PATTERN.fullmatch(tax_id):
        return jsonify(error='統編必須是 8 位數字'), 400
    conn = get_db()
    items = store.recent_items(conn, tax_id)
    known = store.find_customer(conn, tax_id)
    if known:
        return jsonify({**known, 'source': 'history', 'items': items})
    company = lookup_company(tax_id)
    if company:
        return jsonify(source='gcis', companyName=company.name, companyAddress=company.address, items=items)
    return jsonify(source=None, items=items), 404


@app.errorhandler(413)
def request_too_large(_e):
    return render_form(errors=['送出的資料太大'], status=413)


@app.cli.command('backup')
@click.argument('destination', type=click.Path(file_okay=False, path_type=Path))
def backup_command(destination: Path):
    """把資料庫和所有報價單檔案備份到 DESTINATION 底下的新資料夾。"""
    target = destination / f'quotation-backup-{datetime.now(q.TAIPEI):%Y%m%d-%H%M%S}'
    with closing(store.connect(data_dir() / 'quotes.sqlite3')) as conn:
        store.backup(conn, target / 'quotes.sqlite3')
    archive = data_dir() / 'archive'
    if archive.is_dir():
        shutil.copytree(archive, target / 'archive')
    click.echo(f'已備份到 {target}')


if __name__ == '__main__':
    app.run(debug=os.environ.get('FLASK_DEBUG') == '1')
