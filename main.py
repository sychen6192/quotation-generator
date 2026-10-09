import logging
import os
import re
import shutil
import time
import uuid
from decimal import Decimal
from pathlib import Path

from flask import Flask, abort, redirect, render_template, request, send_file, url_for

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


@app.template_filter('money')
def money(value: Decimal) -> str:
    return f'{value:,.2f}'


@app.template_filter('number')
def number(value: Decimal) -> str:
    return f'{value.normalize():,f}'


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
    job_dir = app.config['OUTPUT_DIR'] / job_id
    job_dir.mkdir(parents=True)
    stem = quote.file_stem()
    xlsx_path = job_dir / f'{stem}.xlsx'
    q.build_workbook(quote).save(xlsx_path)

    has_pdf = True
    try:
        export_pdf(xlsx_path, job_dir / f'{stem}.pdf')
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
    job_dir = app.config['OUTPUT_DIR'] / job_id
    files = sorted(job_dir.glob(f'*.{file_type}')) if job_dir.is_dir() else []
    if not files:
        abort(404)
    return send_file(files[0], as_attachment=True, download_name=files[0].name)


@app.errorhandler(413)
def request_too_large(_e):
    return render_form(errors=['送出的資料太大'], status=413)


def remove_expired_outputs() -> None:
    output_dir = app.config['OUTPUT_DIR']
    deadline = time.time() - app.config['OUTPUT_TTL_SECONDS']
    for job_dir in output_dir.iterdir():
        try:
            if job_dir.is_dir() and _JOB_ID.fullmatch(job_dir.name) and job_dir.stat().st_mtime < deadline:
                shutil.rmtree(job_dir)
        except OSError:
            logger.warning('無法清除過期檔案 %s', job_dir, exc_info=True)


if __name__ == '__main__':
    app.run(debug=os.environ.get('FLASK_DEBUG') == '1')
