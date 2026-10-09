from datetime import date
from decimal import Decimal

import pytest
from openpyxl import load_workbook

import quotation as q

TODAY = date(2021, 7, 16)


def parse(form):
    return q.parse_form(form, today=TODAY)


def errors_of(form):
    with pytest.raises(q.QuotationError) as e:
        parse(form)
    return e.value.errors


def test_parse_valid_form(valid_form):
    quote = parse(valid_form)
    assert quote.customer_name == '江美志'
    assert quote.valid_until == date(2021, 8, 15)
    assert quote.ship_date == date(2021, 7, 22)
    assert [(p.name, p.quantity, p.price) for p in quote.products] == [
        ('電腦主機', 100, 5500), ('加購記憶體', 100, 400)]
    assert quote.subtotal == 590000
    assert quote.tax == 29500
    assert quote.total == 619500


def test_tax_included_adds_no_tax(valid_form):
    quote = parse(dict(valid_form, tax='y'))
    assert quote.tax == 0
    assert quote.total == quote.subtotal == 590000


@pytest.mark.parametrize('raw, expected', [
    ('A,1,2', [('A', 1, 2)]),
    ('A，1，2', [('A', 1, 2)]),
    ('A|B 規格,1,100', [('A|B 規格', 1, 100)]),
    ('  A , 1 , 2  ', [('A', 1, 2)]),
    ('A,1.5,12.25', [('A', Decimal('1.5'), Decimal('12.25'))]),
    ('A,1,$100', [('A', 1, 100)]),
    ('A,1,0', [('A', 1, 0)]),
    ('A,2,100\n折扣,1,-50', [('A', 2, 100), ('折扣', 1, -50)]),   # 單價負數 = 折扣列
    ('A,1,-0', [('A', 1, 0)]),
    ('A,1,999999999999.9999', [('A', 1, Decimal('999999999999.9999'))]),
    ('A,1,2\n\n   \nB,3,4\n', [('A', 1, 2), ('B', 3, 4)]),
    ('A,1,2\r\nB,3,4', [('A', 1, 2), ('B', 3, 4)]),
])
def test_parse_products(raw, expected):
    errors = []
    products = q.parse_products(raw, errors)
    assert errors == []
    assert [(p.name, p.quantity, p.price) for p in products] == expected


@pytest.mark.parametrize('raw, message', [
    ('', '請至少輸入一個品項'),
    ('\n \n', '請至少輸入一個品項'),
    ('only name', '第 1 行'),
    ('A,1', '第 1 行'),
    ('A,1,2\nB,x,2', '第 2 行的數量'),
    ('A,0,2', '數量'),
    ('A,-1,2', '數量'),
    ('A,1,abc', '價格'),
    ('A,1,-5', '合計不可小於 0'),
    ('A,1e400,1', '數量'),
    ('A,1,1e999999', '價格'),
    ('A,1,1000000000000', '價格'),
    ('A,1,0.00001', '價格'),
    ('A,NaN,5', '數量'),
    ('A,1,Infinity', '價格'),
    ('筆電,2,1,000', '不要寫成 1,000'),     # 千分位逗號不能默默變成單價 1
    ('主機, 含螢幕,2,100', '第 1 行'),
    (',1,2', '第 1 行'),
    ('中' * 43 + ',1,1', '商品名稱太長'),
])
def test_parse_products_errors(raw, message):
    errors = []
    q.parse_products(raw, errors)
    assert len(errors) == 1
    assert message in errors[0]


def test_too_many_products(valid_form):
    rows = '\n'.join(f'品項{i},1,1' for i in range(q.MAX_PRODUCTS + 1))
    assert any('最多' in e for e in errors_of(dict(valid_form, product=rows)))
    rows = '\n'.join(f'品項{i},1,1' for i in range(q.MAX_PRODUCTS))
    assert len(parse(dict(valid_form, product=rows)).products) == q.MAX_PRODUCTS


def test_missing_products_reported_with_other_errors(valid_form):
    errors = errors_of(dict(valid_form, cname='', product=''))
    assert '請填寫客戶姓名' in errors
    assert '請至少輸入一個品項' in errors


def test_all_errors_reported_together(valid_form):
    errors = errors_of(dict(valid_form, cname=' ', cphone='', taxid='1234', vday='abc',
                            seller='路人', delivery='飛機', cash='比特幣', tax='x', dday='明天'))
    assert len(errors) == 9


@pytest.mark.parametrize('taxid', ['1234567', '123456789', 'abcdefgh', '1234 5678'])
def test_invalid_tax_id(valid_form, taxid):
    assert '公司統編必須是 8 位數字' in errors_of(dict(valid_form, taxid=taxid))


def test_missing_optional_fields(valid_form):
    form = {k: v for k, v in valid_form.items() if k not in ('taxid', 'companyName', 'companyAddress', 'dday', 'note')}
    quote = parse(form)
    assert quote.ship_date is None and quote.tax_id == '' and quote.note == ''


def test_ship_date_accepts_slashes(valid_form):
    assert parse(dict(valid_form, dday='2021/07/22')).ship_date == date(2021, 7, 22)


def test_text_too_long(valid_form):
    assert any('不可超過' in e for e in errors_of(dict(valid_form, cname='x' * 201)))


@pytest.mark.parametrize('company, customer, expected', [
    ('測試股份有限公司', '江美志', '測試股份有限公司報價單0716'),
    ('', '江美志', '江美志報價單0716'),
    ('A/B\\C:D*E?F"G<H>I|J', '', 'ABCDEFGHIJ報價單0716'),
    ('../../etc', '', 'etc報價單0716'),
    ('...', '', '客戶報價單0716'),
])
def test_file_stem(valid_form, company, customer, expected):
    quote = parse(dict(valid_form, companyName=company, cname=customer or 'x'))
    quote.customer_name = customer
    assert quote.file_stem() == expected


def test_template_tax_rate_matches_constant():
    sheet = load_workbook(q.TEMPLATE_PATH)[q.SHEET_NAME]
    assert Decimal(str(sheet['G35'].value)) == q.TAX_RATE


def build_sheet(form, tmp_path):
    path = tmp_path / 'out.xlsx'
    q.build_workbook(parse(form)).save(path)
    wb = load_workbook(path)
    return wb, wb[q.SHEET_NAME]


def test_build_workbook_fills_template(valid_form, tmp_path):
    wb, sheet = build_sheet(dict(valid_form, taxid='24268597', note='貨到付款'), tmp_path)
    assert sheet['G3'].value == '2021/07/16'
    assert sheet['G8'].value == '2021/08/15'
    assert sheet['B9'].value == '姓名：江美志'
    assert sheet['B10'].value == '公司名稱：測試股份有限公司'
    assert sheet['B11'].value == '統一編號：24268597'
    assert sheet['B12'].value == '公司地址：新竹縣竹北市中和街1號'
    assert sheet['B13'].value == '公司電話：03-5520981#1688'
    assert sheet['C16'].value == '貨到付款'
    assert [sheet[c].value for c in ('B20', 'C20', 'D20', 'F20')] == ['陳聖尹', '2021/07/22', '郵局', '匯款']
    assert wb.calculation.fullCalcOnLoad


def test_build_workbook_products_are_numbers_and_formulas_kept(valid_form, tmp_path):
    _, sheet = build_sheet(valid_form, tmp_path)
    assert [sheet[f'{c}24'].value for c in 'BCEF'] == [100, '電腦主機', 5500, 'T']
    assert [sheet[f'{c}25'].value for c in 'BCEF'] == [100, '加購記憶體', 400, 'T']
    # 範本的範例列被清掉，沒用到的列是空的
    assert [sheet[f'{c}26'].value for c in 'BCEF'] == [None] * 4
    assert sheet['G24'].value == '=B24*E24'
    assert sheet['G34'].value == '=SUM(G24:G33)'
    assert sheet['G36'].value == '=G35*SUMIF(F24:F33,"T",G24:G33)'
    assert sheet['G38'].value == '=G34+G36+G37'


def test_build_workbook_tax_included_has_no_taxable_rows(valid_form, tmp_path):
    _, sheet = build_sheet(dict(valid_form, tax='y'), tmp_path)
    assert [sheet[f'F{r}'].value for r in range(24, 34)] == [None] * 10


def test_build_workbook_decimal_numbers(valid_form, tmp_path):
    _, sheet = build_sheet(dict(valid_form, product='A,1.5,12.25'), tmp_path)
    assert (sheet['B24'].value, sheet['E24'].value) == (1.5, 12.25)


def test_build_workbook_without_company(valid_form, tmp_path):
    _, sheet = build_sheet(dict(valid_form, companyName='', companyAddress='', dday='', note=''), tmp_path)
    assert sheet['B10'].value is None
    assert sheet['B11'].value == '統一編號：無'
    assert sheet['B12'].value is None
    assert sheet['C16'].value == '無'
    assert sheet['C20'].value == '-'


def test_user_input_never_becomes_a_formula(valid_form, tmp_path):
    payload = '=HYPERLINK("http://evil")'
    _, sheet = build_sheet(dict(valid_form, note=payload, product=f'{payload},1,1'), tmp_path)
    for cell in (sheet['C16'], sheet['C24']):
        assert cell.value == payload
        assert cell.data_type == 's'


def test_build_workbook_keeps_stamp_image(valid_form, tmp_path):
    pytest.importorskip('PIL')  # 沒有 Pillow 時 openpyxl 會直接丟掉範本裡的圖片
    _, sheet = build_sheet(valid_form, tmp_path)
    assert len(sheet._images) == 1


def test_negative_zero_becomes_zero():
    errors = []
    product, = q.parse_products('A,1,-0.00', errors)
    assert not product.price.is_signed() and not product.amount.is_signed()


def test_control_characters_are_removed(valid_form):
    quote = parse(dict(valid_form, cname='江\x00美\uffff志\ufffe', note='第一行\x0b第二行\x0c第三行', product='A\x01B,1,2'))
    assert quote.customer_name == '江美志'
    assert quote.note == '第一行\n第二行\n第三行'
    assert quote.products[0].name == 'AB'


def test_note_must_fit_three_lines(valid_form):
    assert parse(dict(valid_form, note='一\n二\n三')).note == '一\n二\n三'
    assert any('備註太長' in e for e in errors_of(dict(valid_form, note='一\n二\n三\n四')))
    assert any('備註太長' in e for e in errors_of(dict(valid_form, note='字' * 120)))


def test_wrapped_lines():
    assert q.display_width('ab中文') == 6
    assert q.wrapped_lines('', 10) == 1
    assert q.wrapped_lines('a' * 10, 10) == 1
    assert q.wrapped_lines('a' * 11, 10) == 2
    assert q.wrapped_lines('中' * 6 + '\n\nx', 10) == 4


def test_build_workbook_note_and_long_names_wrap(valid_form, tmp_path):
    long_name = '工業用不鏽鋼六角螺絲 M3x10mm 304材質 (100入/包)'
    _, sheet = build_sheet(dict(valid_form, note='一\n二', product=f'{long_name},1,1\n短,1,1'), tmp_path)
    assert 'C16:G18' in {str(r) for r in sheet.merged_cells.ranges}
    assert sheet['C16'].value == '一\n二'
    assert sheet['C16'].alignment.wrap_text
    assert sheet['C24'].alignment.wrap_text
    assert sheet.row_dimensions[24].height == pytest.approx(20.1 * q.wrapped_lines(long_name, q.PRODUCT_NAME_LINE_WIDTH))
    assert sheet.row_dimensions[25].height == pytest.approx(20.1)
    assert not sheet['C25'].alignment.wrap_text
    assert sheet.sheet_properties.pageSetUpPr.fitToPage
    assert (sheet.page_setup.fitToWidth, sheet.page_setup.fitToHeight) == (1, 1)


def test_full_width_tax_id_is_normalized(valid_form):
    assert parse(dict(valid_form, taxid='２４２６８５９７')).tax_id == '24268597'
