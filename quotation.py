"""報價單：解析表單資料、套用 Excel 範本。"""
from __future__ import annotations

import math
import re
import unicodedata
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import List, Mapping, Optional, Union

from openpyxl import load_workbook
from openpyxl.cell.cell import ILLEGAL_CHARACTERS_RE
from openpyxl.styles import Alignment
from openpyxl.workbook import Workbook
from openpyxl.worksheet.properties import PageSetupProperties

TEMPLATE_PATH = Path(__file__).with_name('template.xlsx')
SHEET_NAME = '报价单'
TAIPEI = timezone(timedelta(hours=8))  # 台灣沒有日光節約時間，固定 UTC+8 即可

# 表單選項：同時用來產生下拉選單與驗證送進來的值
VALID_DAYS = (30, 14, 7, 5, 3, 1)
SELLERS = ('陳聖尹', '陳紹雲')
DELIVERY_METHODS = ('郵局', '自取', '新竹貨運', '超商取貨', '其他')
PAYMENT_METHODS = ('支票', '現金', '匯款')
TAX_OPTIONS = (('n', '未稅(另加5%)'), ('y', '含稅'))

# 範本版面：品項在第 24~33 列，B=數量、C=說明、E=單價、F=應稅(T)、G=金額(公式)
FIRST_PRODUCT_ROW = 24
MAX_PRODUCTS = 10
TAX_RATE = Decimal('0.05')  # 要和範本 G35 的稅率一致

# 備註寫在合併後的 C16:G18；品名寫在 C:D 合併儲存格，太長時自動換行加高該列。
# 寬度以半形字元計（中文字算 2），抓得比實際保守，避免 PDF 上被截掉。
NOTE_RANGE = 'C16:G18'
NOTE_LINE_WIDTH = 70
PRODUCT_NAME_LINE_WIDTH = 28
MAX_WRAPPED_LINES = 3

MAX_TEXT_LENGTH = 200
MAX_NOTE_LENGTH = 300

# 只接受剛好三欄；千分位逗號（1,000）或名稱裡的逗號都會被擋下，不會默默算錯
_PRODUCT_SEPARATOR = re.compile('[,，]')
_UNSAFE_FILENAME_CHARS = re.compile(r'[\\/:*?"<>|\x00-\x1f]')

Number = Union[int, float]


class QuotationError(ValueError):
    """表單資料有誤；errors 內的訊息可以直接顯示給使用者。"""

    def __init__(self, errors: List[str]):
        super().__init__('；'.join(errors))
        self.errors = errors


@dataclass
class Product:
    name: str
    quantity: Decimal
    price: Decimal

    @property
    def amount(self) -> Decimal:
        return self.quantity * self.price


@dataclass
class Quotation:
    customer_name: str
    phone: str
    products: List[Product]
    tax_included: bool
    seller: str
    delivery_method: str
    payment_method: str
    valid_days: int
    quote_date: date
    tax_id: str = ''
    company_name: str = ''
    company_address: str = ''
    ship_date: Optional[date] = None
    note: str = ''

    @property
    def valid_until(self) -> date:
        return self.quote_date + timedelta(days=self.valid_days)

    # 以下金額與範本公式一致（G34 小計、G36 稅額、G38 總計），用來在網頁上顯示
    @property
    def subtotal(self) -> Decimal:
        return sum((p.amount for p in self.products), Decimal(0))

    @property
    def tax(self) -> Decimal:
        return Decimal(0) if self.tax_included else self.subtotal * TAX_RATE

    @property
    def total(self) -> Decimal:
        return self.subtotal + self.tax

    def file_stem(self) -> str:
        """例如「聖大國際有限公司報價單1009」，已移除檔名不允許的字元。"""
        who = _UNSAFE_FILENAME_CHARS.sub('', self.company_name or self.customer_name).strip(' .')
        return f'{who[:50] or "客戶"}報價單{self.quote_date:%m%d}'


def today_in_taiwan() -> date:
    return datetime.now(TAIPEI).date()


def clean_text(value: str) -> str:
    """移除 Excel 不接受的控制字元（例如從 Word 貼上的 \\v 換行），否則存檔會失敗。"""
    value = value.replace('\v', '\n').replace('\f', '\n')
    return ILLEGAL_CHARACTERS_RE.sub('', value)


def display_width(text: str) -> int:
    return sum(2 if unicodedata.east_asian_width(ch) in 'WF' else 1 for ch in text)


def wrapped_lines(text: str, line_width: int) -> int:
    """在 line_width 寬的儲存格裡換行後大約會佔幾行。"""
    return sum(max(1, math.ceil(display_width(line) / line_width)) for line in text.splitlines() or [''])


def parse_form(form: Mapping[str, str], today: Optional[date] = None) -> Quotation:
    """驗證並轉換表單；有任何錯誤就一次全部丟出 QuotationError。"""
    errors: List[str] = []

    def text(key: str, label: str, required: bool = False, max_length: int = MAX_TEXT_LENGTH) -> str:
        value = clean_text(form.get(key) or '').strip()
        if required and not value:
            errors.append(f'請填寫{label}')
        elif len(value) > max_length:
            errors.append(f'{label}不可超過 {max_length} 個字')
        return value

    def choice(key: str, label: str, options) -> str:
        value = (form.get(key) or '').strip()
        if value not in options:
            errors.append(f'請選擇有效的{label}')
        return value

    customer_name = text('cname', '客戶姓名', required=True)
    phone = text('cphone', '公司電話', required=True)
    tax_id = unicodedata.normalize('NFKC', text('taxid', '公司統編'))  # 全形數字轉半形
    if tax_id and not re.fullmatch(r'[0-9]{8}', tax_id):
        errors.append('公司統編必須是 8 位數字')
    company_name = text('companyName', '公司名稱')
    company_address = text('companyAddress', '公司地址')
    note = text('note', '備註', max_length=MAX_NOTE_LENGTH)
    if wrapped_lines(note, NOTE_LINE_WIDTH) > MAX_WRAPPED_LINES:
        errors.append(f'備註太長，報價單上最多只能放 {MAX_WRAPPED_LINES} 行')

    seller = choice('seller', '銷售員', SELLERS)
    delivery_method = choice('delivery', '發貨方式', DELIVERY_METHODS)
    payment_method = choice('cash', '付款方式', PAYMENT_METHODS)
    tax = choice('tax', '稅金選項', dict(TAX_OPTIONS))

    valid_days = 0
    try:
        valid_days = int(form.get('vday') or '')
    except ValueError:
        pass
    if not 1 <= valid_days <= 365:
        errors.append('請選擇有效的報價有效天數')

    ship_date = None
    ship_date_raw = (form.get('dday') or '').strip()
    if ship_date_raw:
        try:
            ship_date = date.fromisoformat(ship_date_raw.replace('/', '-'))
        except ValueError:
            errors.append('預計出貨日格式錯誤，請用 YYYY-MM-DD')

    products = parse_products(clean_text(form.get('product') or ''), errors)

    if errors:
        raise QuotationError(errors)
    return Quotation(
        customer_name=customer_name,
        phone=phone,
        products=products,
        tax_included=(tax == 'y'),
        seller=seller,
        delivery_method=delivery_method,
        payment_method=payment_method,
        valid_days=valid_days,
        quote_date=today or today_in_taiwan(),
        tax_id=tax_id,
        company_name=company_name,
        company_address=company_address,
        ship_date=ship_date,
        note=note,
    )


def parse_products(raw: str, errors: List[str]) -> List[Product]:
    """每行一項「商品名稱,數量,價格」；空白行會略過，錯誤附上行號寫進 errors。"""
    products: List[Product] = []
    error_count = len(errors)
    for line_no, line in enumerate(raw.splitlines(), start=1):
        line = line.strip()
        if not line:
            continue
        fields = [field.strip() for field in _PRODUCT_SEPARATOR.split(line)]
        if len(fields) != 3 or not fields[0]:
            errors.append(f'品項第 {line_no} 行「{line}」格式錯誤，請用「商品名稱,數量,價格」，'
                          '名稱和金額裡不要再用逗號（例如 1000 不要寫成 1,000）')
            continue
        name, quantity_raw, price_raw = fields
        quantity = _parse_number(quantity_raw)
        price = _parse_number(price_raw)
        if wrapped_lines(name, PRODUCT_NAME_LINE_WIDTH) > MAX_WRAPPED_LINES:
            errors.append(f'品項第 {line_no} 行的商品名稱太長，報價單上最多只能放 {MAX_WRAPPED_LINES} 行')
        elif quantity is None or quantity <= 0:
            errors.append(f'品項第 {line_no} 行的數量「{quantity_raw}」必須是大於 0 的數字')
        elif price is None or price < 0:
            errors.append(f'品項第 {line_no} 行的價格「{price_raw}」必須是數字')
        else:
            products.append(Product(name, quantity, price))

    if not products and len(errors) == error_count:
        errors.append('請至少輸入一個品項')
    elif len(products) > MAX_PRODUCTS:
        errors.append(f'品項最多 {MAX_PRODUCTS} 項（範本只有 {MAX_PRODUCTS} 列），目前有 {len(products)} 項')
    return products


def _parse_number(raw: str) -> Optional[Decimal]:
    try:
        value = Decimal(raw.strip().replace('$', ''))
    except InvalidOperation:
        return None
    return value if value.is_finite() else None


def _excel_number(value: Decimal) -> Number:
    return int(value) if value == value.to_integral_value() else float(value)


def build_workbook(quotation: Quotation, template: Path = TEMPLATE_PATH) -> Workbook:
    """把報價資料填進範本。金額欄位保留範本裡的公式，交給 Excel / LibreOffice 計算。"""
    if len(quotation.products) > MAX_PRODUCTS:
        raise QuotationError([f'品項最多 {MAX_PRODUCTS} 項'])

    wb = load_workbook(template)
    sheet = wb[SHEET_NAME]

    def put_text(coordinate: str, value: str) -> None:
        # 強制當成文字寫入：使用者輸入「=...」時不會變成 Excel 公式被執行
        cell = sheet[coordinate]
        cell.value = value
        cell.data_type = 's'

    put_text('G3', f'{quotation.quote_date:%Y/%m/%d}')
    put_text('G8', f'{quotation.valid_until:%Y/%m/%d}')
    put_text('B9', f'姓名：{quotation.customer_name}')
    if quotation.company_name:
        put_text('B10', f'公司名稱：{quotation.company_name}')
    put_text('B11', f'統一編號：{quotation.tax_id or "無"}')
    if quotation.company_address:
        put_text('B12', f'公司地址：{quotation.company_address}')
    put_text('B13', f'公司電話：{quotation.phone}')
    put_text('C16', quotation.note or '無')
    # 備註可能多行：合併 C16:G18 並自動換行，原本單一儲存格只會擠成一行、超出列印範圍
    sheet.merge_cells(NOTE_RANGE)
    sheet['C16'].alignment = Alignment(wrap_text=True, vertical='top')
    for row in range(16, 19):
        sheet.row_dimensions[row].height = 15  # 預設 12.75 放三行字會壓到下面的表格框線

    put_text('B20', quotation.seller)
    put_text('C20', f'{quotation.ship_date:%Y/%m/%d}' if quotation.ship_date else '-')
    put_text('D20', quotation.delivery_method)
    put_text('F20', quotation.payment_method)

    # 先清掉範本裡的範例列，G 欄的「=B*E」公式保留
    for row in range(FIRST_PRODUCT_ROW, FIRST_PRODUCT_ROW + MAX_PRODUCTS):
        for column in 'BCEF':
            sheet[f'{column}{row}'].value = None
    for row, product in enumerate(quotation.products, start=FIRST_PRODUCT_ROW):
        sheet[f'B{row}'] = _excel_number(product.quantity)
        put_text(f'C{row}', product.name)
        lines = wrapped_lines(product.name, PRODUCT_NAME_LINE_WIDTH)
        if lines > 1:
            # 合併儲存格不會自動調整列高，要自己加高
            name_cell = sheet[f'C{row}']
            name_cell.alignment = Alignment(horizontal='left', vertical='center', wrap_text=True)
            dimension = sheet.row_dimensions[row]
            dimension.height = (dimension.height or 20.1) * lines
        sheet[f'E{row}'] = _excel_number(product.price)
        # 範本的稅額公式只對 F 欄標 T 的列加 5%；含稅價格就不標
        if not quotation.tax_included:
            put_text(f'F{row}', 'T')

    # 品名換行加高後仍然縮印在一頁內（只會縮小、不會放大）
    sheet.sheet_properties.pageSetUpPr = PageSetupProperties(fitToPage=True)
    sheet.page_setup.fitToWidth = 1
    sheet.page_setup.fitToHeight = 1

    # openpyxl 不會計算公式，要求開啟檔案時重新計算
    wb.calculation.fullCalcOnLoad = True
    return wb
