import hmac
import logging
import os
import re
import shutil
import time
import uuid
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path

from flask import Flask, Response, abort, redirect, render_template, request, send_file, url_for

import quotation as q
from company import lookup_company
from pdf_export import PdfExportError, export_pdf

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(name)s: %(message)s')
logger = logging.getLogger(__name__)

app = Flask(__name__)
app.config.update(
    OUTPUT_DIR=Path(os.environ.get('QUOTATION_OUTPUT_DIR') or Path(__file__).with_name('output')),
    OUTPUT_TTL_SECONDS=24 * 60 * 60,  # 產生的檔案保留一天
    MAX_CONTENT_LENGTH=64 * 1024,
    # 兩個都設定時整個網站需要帳號密碼（HTTP Basic Auth，請搭配 HTTPS）
    USERNAME=os.environ.get('QUOTATION_USERNAME'),
    PASSWORD=os.environ.get('QUOTATION_PASSWORD'),
)

# 下拉選單的 (value, 顯示文字)
FORM_OPTIONS = dict(
    valid_days=[(d, f'{d} 天') for d in q.VALID_DAYS],
    sellers=[(s, s) for s in q.SELLERS],
    delivery_methods=[(s, s) for s in q.DELIVERY_METHODS],
    payment_methods=[(s, s) for s in q.PAYMENT_METHODS],
    tax_options=q.TAX_OPTIONS,
    max_products=q.MAX_PRODUCTS,
)
DOWNLOAD_TYPES = {'pdf', 'xlsx'}
_JOB_ID = re.compile(r'[0-9a-f]{32}')
# 磁碟上一律用 ASCII 檔名（LANG=C 的 Linux/mod_wsgi 存不了中文檔名），下載時才用中文名稱
FILE_STEM = 'quotation'
NAME_FILE = 'name.txt'


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


def output_dir() -> Path:
    # 轉成絕對路徑：send_file 會把相對路徑接在 app.root_path 後面，和寫檔時的工作目錄不同
    return Path(app.config['OUTPUT_DIR']).resolve()


def render_form(form=None, errors=(), status=200):
    return render_template('form.html', form=form or {}, errors=errors, **FORM_OPTIONS), status


@app.get('/')
def index():
    return render_form()


@app.get('/generate')
def generate_get():
    return redirect(url_for('index'))


@app.post('/generate')
def generate():
    try:
        quote = q.parse_form(request.form)
    except q.QuotationError as e:
        return render_form(request.form, e.errors, 400)

    warnings = []
    if quote.tax_id and not (quote.company_name and quote.company_address):
        company = lookup_company(quote.tax_id)
        if company:
            # 手動輸入的公司名稱、地址優先
            quote.company_name = quote.company_name or company.name
            quote.company_address = quote.company_address or company.address
        elif not quote.company_name:
            return render_form(request.form, [
                f'查不到統編 {quote.tax_id} 的公司資料（或政府 API 暫時連不上），請手動填寫公司名稱與地址'
            ], 400)
        else:
            warnings.append(f'查不到統編 {quote.tax_id} 的公司地址，報價單上會留空')

    job_id = uuid.uuid4().hex
    job_dir = output_dir() / job_id
    stem = quote.file_stem()
    xlsx_path = job_dir / f'{FILE_STEM}.xlsx'
    try:
        job_dir.mkdir(parents=True)
        (job_dir / NAME_FILE).write_text(stem, encoding='utf-8')
        q.build_workbook(quote).save(xlsx_path)
    except Exception:
        logger.exception('產生 Excel 失敗（job %s）', job_id)
        shutil.rmtree(job_dir, ignore_errors=True)
        return render_form(request.form, ['產生報價單失敗，請檢查輸入的資料或稍後再試'], 500)

    has_pdf = True
    try:
        export_pdf(xlsx_path, job_dir / f'{FILE_STEM}.pdf')
    except PdfExportError:
        logger.exception('PDF 轉檔失敗（job %s）', job_id)
        has_pdf = False
        warnings.append('PDF 轉檔失敗，請先下載 Excel 檔')

    remove_expired_outputs()
    return render_template('gen.html', quote=quote, job_id=job_id, stem=stem,
                           has_pdf=has_pdf, warnings=warnings)


@app.get('/download/<job_id>/<file_type>')
def download(job_id, file_type):
    if not _JOB_ID.fullmatch(job_id) or file_type not in DOWNLOAD_TYPES:
        abort(404)
    job_dir = output_dir() / job_id
    path = job_dir / f'{FILE_STEM}.{file_type}'
    if not path.is_file():
        abort(404)
    try:
        stem = (job_dir / NAME_FILE).read_text(encoding='utf-8').strip() or FILE_STEM
    except OSError:
        stem = FILE_STEM
    return send_file(path, as_attachment=True, download_name=f'{stem}.{file_type}')


@app.errorhandler(413)
def request_too_large(_e):
    return render_form(errors=['送出的資料太大'], status=413)


def remove_expired_outputs() -> None:
    deadline = time.time() - app.config['OUTPUT_TTL_SECONDS']
    for job_dir in output_dir().iterdir():
        try:
            if job_dir.is_dir() and _JOB_ID.fullmatch(job_dir.name) and job_dir.stat().st_mtime < deadline:
                shutil.rmtree(job_dir)
        except OSError:
            logger.warning('無法清除過期檔案 %s', job_dir, exc_info=True)


if __name__ == '__main__':
    app.run(debug=os.environ.get('FLASK_DEBUG') == '1')
