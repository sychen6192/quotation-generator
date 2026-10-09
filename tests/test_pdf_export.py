import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import types
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

import pdf_export
import quotation as q


class FakeExcel:
    def __init__(self, fail_on_export=False):
        self.calls = []
        self.fail_on_export = fail_on_export
        self.Workbooks = self

    def Open(self, path):  # noqa: N802 (COM 介面的命名)
        self.calls.append(('open', path))
        return self

    @property
    def ActiveSheet(self):  # noqa: N802
        return self

    def ExportAsFixedFormat(self, file_type, path):  # noqa: N802
        self.calls.append(('export', file_type, path))
        if self.fail_on_export:
            raise RuntimeError('COM error')

    def Close(self, save):  # noqa: N802
        self.calls.append(('close', save))

    def Quit(self):  # noqa: N802
        self.calls.append(('quit',))


@pytest.fixture
def fake_win32(monkeypatch):
    def install(excel):
        pythoncom = types.ModuleType('pythoncom')
        pythoncom.calls = []
        pythoncom.CoInitialize = lambda: pythoncom.calls.append('init')
        pythoncom.CoUninitialize = lambda: pythoncom.calls.append('uninit')
        client = types.ModuleType('win32com.client')
        client.DispatchEx = lambda name: excel
        win32com = types.ModuleType('win32com')
        win32com.client = client
        monkeypatch.setitem(sys.modules, 'pythoncom', pythoncom)
        monkeypatch.setitem(sys.modules, 'win32com', win32com)
        monkeypatch.setitem(sys.modules, 'win32com.client', client)
        return pythoncom
    return install


def test_excel_backend_uses_absolute_paths_and_cleans_up(fake_win32, tmp_path, monkeypatch):
    excel = FakeExcel()
    pythoncom = fake_win32(excel)
    monkeypatch.chdir(tmp_path)
    pdf_export.export_pdf(pdf_export.Path('a.xlsx'), pdf_export.Path('a.pdf'), backend='excel')
    assert excel.calls == [
        ('open', str(tmp_path / 'a.xlsx')),
        ('export', 0, str(tmp_path / 'a.pdf')),
        ('close', False),
        ('quit',),
    ]
    assert excel.Visible is False and excel.DisplayAlerts is False
    assert pythoncom.calls == ['init', 'uninit']


def test_excel_backend_failure_still_quits_excel(fake_win32, tmp_path):
    excel = FakeExcel(fail_on_export=True)
    pythoncom = fake_win32(excel)
    with pytest.raises(pdf_export.PdfExportError):
        pdf_export.export_pdf(tmp_path / 'a.xlsx', tmp_path / 'a.pdf', backend='excel')
    assert excel.calls[-2:] == [('close', False), ('quit',)]
    assert pythoncom.calls == ['init', 'uninit']


def test_excel_backend_without_pywin32(monkeypatch, tmp_path):
    monkeypatch.setitem(sys.modules, 'pythoncom', None)  # import 會失敗
    with pytest.raises(pdf_export.PdfExportError, match='pywin32'):
        pdf_export.export_pdf(tmp_path / 'a.xlsx', tmp_path / 'a.pdf', backend='excel')


def test_unknown_backend(tmp_path):
    with pytest.raises(pdf_export.PdfExportError):
        pdf_export.export_pdf(tmp_path / 'a.xlsx', tmp_path / 'a.pdf', backend='word')


def test_backend_from_environment(monkeypatch, tmp_path):
    used = []
    monkeypatch.setattr(pdf_export, '_export_with_libreoffice', lambda x, p: used.append('libreoffice'))
    monkeypatch.setattr(pdf_export, '_export_with_excel', lambda x, p: used.append('excel'))
    monkeypatch.setenv('PDF_BACKEND', 'libreoffice')
    pdf_export.export_pdf(tmp_path / 'a.xlsx', tmp_path / 'a.pdf')
    monkeypatch.setenv('PDF_BACKEND', 'auto')
    monkeypatch.setattr(pdf_export.sys, 'platform', 'win32')
    pdf_export.export_pdf(tmp_path / 'a.xlsx', tmp_path / 'a.pdf')
    assert used == ['libreoffice', 'excel']


def test_libreoffice_missing(monkeypatch, tmp_path):
    monkeypatch.delenv('SOFFICE_PATH', raising=False)
    monkeypatch.setattr(pdf_export.shutil, 'which', lambda name: None)
    with pytest.raises(pdf_export.PdfExportError, match='LibreOffice'):
        pdf_export.export_pdf(tmp_path / 'a.xlsx', tmp_path / 'a.pdf', backend='libreoffice')


@pytest.mark.skipif(os.name != 'posix', reason='用 shell script 模擬 soffice')
def test_libreoffice_timeout_kills_child_processes(monkeypatch, tmp_path):
    # 模擬 soffice 啟動腳本再開出真正做事的子程序（soffice.bin）
    child_pid_file = tmp_path / 'child.pid'
    fake = tmp_path / 'soffice'
    fake.write_text(f'#!/bin/sh\nsleep 300 &\necho $! > {child_pid_file}\nwait\n')
    fake.chmod(0o755)
    monkeypatch.setenv('SOFFICE_PATH', str(fake))
    monkeypatch.setattr(pdf_export, 'LIBREOFFICE_TIMEOUT_SECONDS', 1)
    before = set(Path(tempfile.gettempdir()).glob('quotation-pdf-*'))

    with pytest.raises(pdf_export.PdfExportError, match='超過'):
        pdf_export.export_pdf(tmp_path / 'a.xlsx', tmp_path / 'a.pdf', backend='libreoffice')

    child_pid = int(child_pid_file.read_text())
    for _ in range(50):
        try:
            os.kill(child_pid, 0)
        except ProcessLookupError:
            break
        time.sleep(0.1)
    else:
        pytest.fail('soffice 的子程序沒有被結束')
    assert set(Path(tempfile.gettempdir()).glob('quotation-pdf-*')) == before  # 暫存資料夾有清掉


@pytest.mark.skipif(os.name != 'posix', reason='用 shell script 模擬 soffice')
def test_libreoffice_not_producing_pdf(monkeypatch, tmp_path):
    fake = tmp_path / 'soffice'
    fake.write_text('#!/bin/sh\necho broken\nexit 1\n')
    fake.chmod(0o755)
    monkeypatch.setenv('SOFFICE_PATH', str(fake))
    with pytest.raises(pdf_export.PdfExportError, match='沒有產生 PDF'):
        pdf_export.export_pdf(tmp_path / 'a.xlsx', tmp_path / 'a.pdf', backend='libreoffice')


@pytest.mark.skipif(not pdf_export.find_soffice(), reason='沒有安裝 LibreOffice')
def test_libreoffice_end_to_end(tmp_path, valid_form):
    quote = q.parse_form(valid_form, today=date(2021, 7, 16))
    xlsx = tmp_path / f'{quote.file_stem()}.xlsx'
    q.build_workbook(quote).save(xlsx)
    pdf = tmp_path / f'{quote.file_stem()}.pdf'
    pdf_export.export_pdf(xlsx, pdf, backend='libreoffice')
    assert pdf.read_bytes()[:5] == b'%PDF-'

    if shutil.which('pdftotext'):
        text = subprocess.run(['pdftotext', '-layout', str(pdf), '-'],
                              capture_output=True, text=True, check=True).stdout
        # 公式有被重新計算：小計 590,000、稅額 29,500、總計 619,500
        for expected in ('590,000.00', '29,500.00', '619,500.00', '江美志', '電腦主機'):
            assert expected in text


needs_soffice = pytest.mark.skipif(not pdf_export.find_soffice(), reason='沒有安裝 LibreOffice')
needs_poppler = pytest.mark.skipif(not (shutil.which('pdfinfo') and shutil.which('pdffonts')),
                                   reason='沒有安裝 poppler-utils')


@needs_soffice
@needs_poppler
def test_worst_case_quote_fits_one_page_with_cjk_font(tmp_path, valid_form):
    # 10 項品名都換成 3 行、備註 3 行：還是要印在同一頁，而且中文字型有嵌入（不會變成方框）
    long_name = '工業用不鏽鋼六角螺絲 M3x10mm 304材質 (100入/包) 附彈簧墊圈'
    products = '\n'.join(f'{long_name[:30]}{i},1,1' for i in range(q.MAX_PRODUCTS))
    quote = q.parse_form(dict(valid_form, product=products, note='一行備註\n第二行\n第三行'),
                         today=date(2021, 7, 16))
    quote.quote_no = 'SD202107-001'
    xlsx = tmp_path / 'quotation.xlsx'
    q.build_workbook(quote).save(xlsx)
    pdf = tmp_path / 'quotation.pdf'
    pdf_export.export_pdf(xlsx, pdf, backend='libreoffice')

    info = subprocess.run(['pdfinfo', str(pdf)], capture_output=True, text=True, check=True).stdout
    assert re.search(r'^Pages:\s+1$', info, re.M), info
    fonts = subprocess.run(['pdffonts', str(pdf)], capture_output=True, text=True, check=True).stdout
    cjk = [line for line in fonts.splitlines()
           if re.search(r'CJK|Hei|Ming|Song|Kai|WenQuanYi|JhengHei|Mincho|Gothic', line, re.I)]
    assert cjk and any(' yes ' in line for line in cjk), fonts


# (數量, 單價) 和小計：包含二進位浮點數不精確的值（1.005、2.675）與剛好 .5 的情況
_LINE_CASES = [('1.5', '12.25'), ('3', '0.333'), ('1', '1.005'), ('0.5', '0.25'), ('2.675', '1'),
               ('1', '1.015'), ('1', '-1.005'), ('7', '0.145'), ('1.1', '1.1'), ('999', '999.9999')]
_SUBTOTALS = ['1234', '1010', '30', '29', '640.5', '1050', '105.5', '0.1', '21', '10', '9.9',
              '1000', '999999999.99', '-30', '12.3']


@needs_soffice
def test_libreoffice_rounding_matches_python(tmp_path):
    """網頁上的金額（Python Decimal）要和報價單上的公式（LibreOffice / Excel）一致。"""
    from openpyxl import Workbook
    wb = Workbook()
    sheet = wb.active
    sheet['H1'] = float(q.TAX_RATE)  # 對應範本 G35
    for row, (qty, price) in enumerate(_LINE_CASES, start=1):
        sheet[f'A{row}'], sheet[f'B{row}'] = float(qty), float(price)
        sheet[f'C{row}'] = q.LINE_AMOUNT_FORMULA.replace('B{row}', f'A{row}').replace('E{row}', f'B{row}')
    for row, subtotal in enumerate(_SUBTOTALS, start=1):
        sheet[f'D{row}'] = float(subtotal)
        sheet[f'E{row}'] = f'=ROUND($H$1*D{row},0)'                                  # 同 TAX_EXCLUDED_FORMULA
        sheet[f'F{row}'] = q.TAX_INCLUDED_FORMULA.replace('(G34+G37)', f'D{row}')
    for column in 'CEF':
        for cell in sheet[column]:
            cell.number_format = '0.00'
    xlsx = tmp_path / 'rounding.xlsx'
    wb.save(xlsx)
    profile = (tmp_path / 'profile').as_uri()
    subprocess.run([pdf_export.find_soffice(), f'-env:UserInstallation={profile}', '--headless',
                    '--convert-to', 'csv', '--outdir', str(tmp_path), str(xlsx)],
                   capture_output=True, timeout=120, check=True)
    rows = [line.split(',') for line in (tmp_path / 'rounding.csv').read_text().splitlines()]

    for row, (qty, price) in zip(rows, _LINE_CASES):
        expected = q.Product('x', Decimal(qty), Decimal(price)).amount
        assert Decimal(row[2]) == expected, (qty, price, row[2])
    for row, subtotal in zip(rows, _SUBTOTALS):
        product = [q.Product('x', Decimal(1), Decimal(subtotal))]
        assert Decimal(row[4]) == q.compute_totals(product, False).tax, (subtotal, row[4])
        assert Decimal(row[5]) == q.compute_totals(product, True).tax, (subtotal, row[5])
