"""把填好的 xlsx 轉成 PDF。

- Windows：透過 Excel COM（需要安裝 Excel 與 pywin32），與原本的做法相同。
- 其他平台：用 LibreOffice headless（soffice）。

可用環境變數 PDF_BACKEND=excel|libreoffice 強制指定，SOFFICE_PATH 指定 soffice 路徑。
"""
from __future__ import annotations

import logging
import os
import shutil
import signal
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

LIBREOFFICE_TIMEOUT_SECONDS = 120
_SOFFICE_CANDIDATES = (
    'soffice',
    'libreoffice',
    '/Applications/LibreOffice.app/Contents/MacOS/soffice',
    r'C:\Program Files\LibreOffice\program\soffice.exe',
)


class PdfExportError(RuntimeError):
    pass


def export_pdf(xlsx_path: Path, pdf_path: Path, backend: Optional[str] = None) -> None:
    backend = (backend or os.environ.get('PDF_BACKEND') or 'auto').lower()
    if backend == 'auto':
        backend = 'excel' if sys.platform == 'win32' else 'libreoffice'
    if backend == 'excel':
        _export_with_excel(Path(xlsx_path), Path(pdf_path))
    elif backend == 'libreoffice':
        _export_with_libreoffice(Path(xlsx_path), Path(pdf_path))
    else:
        raise PdfExportError(f'不支援的 PDF_BACKEND：{backend}')


def _export_with_excel(xlsx_path: Path, pdf_path: Path) -> None:
    try:
        import pythoncom
        from win32com import client
    except ImportError as e:
        raise PdfExportError('需要安裝 pywin32 才能用 Excel 轉 PDF') from e

    pythoncom.CoInitialize()
    excel = workbook = None
    try:
        excel = client.DispatchEx('Excel.Application')
        excel.Visible = False
        excel.Interactive = False
        excel.DisplayAlerts = False
        # Excel COM 的相對路徑是以「文件」資料夾為基準，所以一律給絕對路徑
        workbook = excel.Workbooks.Open(str(xlsx_path.resolve()))
        workbook.ActiveSheet.ExportAsFixedFormat(0, str(pdf_path.resolve()))  # 0 = xlTypePDF
    except Exception as e:
        raise PdfExportError(f'Excel 轉 PDF 失敗：{e}') from e
    finally:
        # 清理失敗只記錄，不要蓋掉原本的錯誤，也不要留下 Excel 行程
        if workbook is not None:
            try:
                workbook.Close(False)
            except Exception:
                logger.exception('關閉 Excel 活頁簿失敗')
        if excel is not None:
            try:
                excel.Quit()
            except Exception:
                logger.exception('關閉 Excel 失敗')
        pythoncom.CoUninitialize()


def find_soffice() -> Optional[str]:
    configured = os.environ.get('SOFFICE_PATH')
    if configured:
        return configured
    for candidate in _SOFFICE_CANDIDATES:
        found = shutil.which(candidate)
        if found:
            return found
    return None


def _export_with_libreoffice(xlsx_path: Path, pdf_path: Path) -> None:
    soffice = find_soffice()
    if not soffice:
        raise PdfExportError('找不到 LibreOffice（soffice），請安裝或設定 SOFFICE_PATH')

    # 不用 TemporaryDirectory：逾時被砍掉的 soffice 可能還在寫檔，清理失敗不該變成 500
    tmp_dir = Path(tempfile.mkdtemp(prefix='quotation-pdf-'))
    try:
        # 每次轉檔用獨立的使用者設定檔，多個請求同時轉檔才不會互相卡住
        profile = (tmp_dir / 'profile').as_uri()
        cmd = [
            soffice, f'-env:UserInstallation={profile}',
            '--headless', '--norestore', '--nolockcheck',
            '--convert-to', 'pdf', '--outdir', str(tmp_dir), str(xlsx_path.resolve()),
        ]
        log_path = tmp_dir / 'soffice.log'
        with open(log_path, 'wb') as log:
            try:
                # soffice 只是啟動腳本，真正轉檔的是它再開的 soffice.bin；
                # 放進新的 process group，逾時才能整組結束，不會留下孤兒程序
                proc = subprocess.Popen(cmd, stdout=log, stderr=subprocess.STDOUT,
                                        start_new_session=(os.name == 'posix'))
            except OSError as e:
                raise PdfExportError(f'無法執行 LibreOffice：{e}') from e
            try:
                proc.wait(timeout=LIBREOFFICE_TIMEOUT_SECONDS)
            except subprocess.TimeoutExpired as e:
                _kill_process_tree(proc)
                raise PdfExportError(f'LibreOffice 轉 PDF 超過 {LIBREOFFICE_TIMEOUT_SECONDS} 秒') from e

        produced = tmp_dir / f'{xlsx_path.stem}.pdf'
        if not produced.is_file():
            logger.error('soffice exit=%s output=%s', proc.returncode,
                         log_path.read_text(encoding='utf-8', errors='replace'))
            raise PdfExportError('LibreOffice 沒有產生 PDF')
        try:
            shutil.move(str(produced), str(pdf_path))
        except OSError as e:
            raise PdfExportError(f'無法儲存 PDF：{e}') from e
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


def _kill_process_tree(proc: subprocess.Popen) -> None:
    try:
        if os.name == 'posix':
            os.killpg(proc.pid, signal.SIGKILL)
        else:
            subprocess.run(['taskkill', '/T', '/F', '/PID', str(proc.pid)], capture_output=True)
    except OSError:
        proc.kill()
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        logger.error('無法結束 LibreOffice 程序 %s', proc.pid)
