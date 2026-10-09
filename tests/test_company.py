import pytest
import requests

import company


class FakeResponse:
    def __init__(self, content=b'', status=200, payload=None):
        self.content = content
        self.status_code = status
        self._payload = payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f'{self.status_code}')

    def json(self):
        if self._payload is None:
            raise ValueError('not json')
        return self._payload


COMPANY_JSON = [{
    'Business_Accounting_NO': '24268597',
    'Company_Name': '芯成科技股份有限公司',
    'Company_Location': '新竹縣竹北市泰和里中和街62巷17弄1號2樓',
}]


@pytest.fixture
def fake_get(monkeypatch):
    calls = []

    def install(response):
        def fake(url, timeout):
            calls.append((url, timeout))
            if isinstance(response, Exception):
                raise response
            return response
        monkeypatch.setattr(company.requests, 'get', fake)
        return calls
    return install


def test_lookup_success(fake_get):
    calls = fake_get(FakeResponse(b'[...]', payload=COMPANY_JSON))
    info = company.lookup_company('24268597')
    assert info == company.CompanyInfo('24268597', '芯成科技股份有限公司', '新竹縣竹北市泰和里中和街62巷17弄1號2樓')
    url, timeout = calls[0]
    assert url.endswith('$format=json&$filter=Business_Accounting_NO%20eq%2024268597')
    assert timeout == company.TIMEOUT_SECONDS


def test_lookup_success_is_cached(fake_get):
    calls = fake_get(FakeResponse(b'[...]', payload=COMPANY_JSON))
    company.lookup_company('24268597')
    company.lookup_company('24268597')
    assert len(calls) == 1


@pytest.mark.parametrize('response', [
    FakeResponse(b''),                      # 查無資料時 API 回傳空白
    FakeResponse(b'  \n'),
    FakeResponse(b'[]', payload=[]),
    FakeResponse(b'{}', payload={}),
    FakeResponse(b'["x"]', payload=['x']),
    FakeResponse(b'<html>', payload=None),  # 不是 JSON
    FakeResponse(b'oops', status=500),
    requests.Timeout('timeout'),
    requests.ConnectionError('down'),
])
def test_lookup_failures_return_none_and_are_not_cached(fake_get, response):
    calls = fake_get(response)
    assert company.lookup_company('24268597') is None
    assert company.lookup_company('24268597') is None
    assert len(calls) == 2


@pytest.mark.parametrize('tax_id', ['', '1234567', '12345678 or 1 eq 1', '１２３４５６７８'])
def test_invalid_tax_id_never_hits_api(fake_get, tax_id):
    calls = fake_get(FakeResponse(b'[...]', payload=COMPANY_JSON))
    assert company.lookup_company(tax_id) is None
    assert calls == []
