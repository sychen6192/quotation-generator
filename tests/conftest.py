import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import company  # noqa: E402

VALID_FORM = {
    'vday': '30',
    'cname': '江美志',
    'taxid': '',
    'companyName': '測試股份有限公司',
    'companyAddress': '新竹縣竹北市中和街1號',
    'cphone': '03-5520981#1688',
    'product': '電腦主機,100,5500\r\n加購記憶體,100,400',
    'tax': 'n',
    'seller': '陳聖尹',
    'dday': '2021-07-22',
    'delivery': '郵局',
    'cash': '匯款',
    'note': '',
}


@pytest.fixture
def valid_form():
    return dict(VALID_FORM)


@pytest.fixture(autouse=True)
def clear_company_cache():
    company._cache.clear()
    yield
    company._cache.clear()
