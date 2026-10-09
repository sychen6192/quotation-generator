"""把填好的 xlsx 轉成 PDF。

- Windows：透過 Excel COM（需要安裝 Excel 與 pywin32），與原本的做法相同。
- 其他平台：用 LibreOffice headless（soffice）。

可用環境變數 PDF_BACKEND=excel|libreoffice 強制指定，SOFFICE_PATH 指定 soffice 路徑。
"""
from __future__ import annotations

import logging
import os
import shutil
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

    with tempfile.TemporaryDirectory(prefix='quotation-pdf-') as tmp:
        tmp_dir = Path(tmp)
        # 每次轉檔用獨立的使用者設定檔，多個請求同時轉檔才不會互相卡住
        profile = (tmp_dir / 'profile').as_uri()
        cmd = [
            soffice, f'-env:UserInstallation={profile}',
            '--headless', '--norestore', '--nolockcheck',
            '--convert-to', 'pdf', '--outdir', str(tmp_dir), str(xlsx_path.resolve()),
        ]
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True,
                                  timeout=LIBREOFFICE_TIMEOUT_SECONDS)
        except (OSError, subprocess.TimeoutExpired) as e:
            raise PdfExportError(f'LibreOffice 轉 PDF 失敗：{e}') from e

        produced = tmp_dir / f'{xlsx_path.stem}.pdf'
        if not produced.is_file():
            logger.error('soffice exit=%s stdout=%s stderr=%s',
                         proc.returncode, proc.stdout, proc.stderr)
            raise PdfExportError('LibreOffice 沒有產生 PDF')
        shutil.move(str(produced), str(pdf_path))
