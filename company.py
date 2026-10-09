"""用統一編號向經濟部商業司（GCIS）公開資料 API 查詢公司名稱與地址。"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Dict, Optional

import requests

logger = logging.getLogger(__name__)

# 公司登記基本資料（應用一）
GCIS_API = 'https://data.gcis.nat.gov.tw/od/data/api/5F64D864-61CB-4D0D-8AD9-492047CC1EA6'
TAX_ID_PATTERN = re.compile(r'[0-9]{8}')  # 不用 \d：它也會比對到全形數字
TIMEOUT_SECONDS = 10
CACHE_SIZE = 256


@dataclass(frozen=True)
class CompanyInfo:
    tax_id: str
    name: str
    address: str


# 只快取查到的結果；查無資料或連線失敗下次會重查
_cache: Dict[str, CompanyInfo] = {}


def lookup_company(tax_id: str) -> Optional[CompanyInfo]:
    """查無資料、API 連不上或回傳格式不對時回傳 None，不會丟例外。"""
    if not TAX_ID_PATTERN.fullmatch(tax_id):
        return None
    if tax_id in _cache:
        return _cache[tax_id]
    try:
        info = _fetch(tax_id)
    except (requests.RequestException, ValueError) as e:
        logger.warning('查詢統編 %s 失敗：%s', tax_id, e)
        return None
    if info is None:
        logger.info('查無統編 %s 的公司登記資料', tax_id)
        return None
    if len(_cache) >= CACHE_SIZE:
        _cache.clear()
    _cache[tax_id] = info
    return info


def _fetch(tax_id: str) -> Optional[CompanyInfo]:
    # 統編已驗證為 8 位數字，可以安全地放進 $filter；
    # 刻意不用 params=，避免 requests 把 $ 與空白編碼成 API 不認得的形式。
    url = f'{GCIS_API}?$format=json&$filter=Business_Accounting_NO%20eq%20{tax_id}'
    res = requests.get(url, timeout=TIMEOUT_SECONDS)
    res.raise_for_status()
    if not res.content.strip():  # 查無資料時 API 回傳空白內容
        return None
    data = res.json()
    if not isinstance(data, list) or not data or not isinstance(data[0], dict):
        return None
    item = data[0]
    return CompanyInfo(
        tax_id=str(item.get('Business_Accounting_NO') or tax_id),
        name=str(item.get('Company_Name') or '').strip(),
        address=str(item.get('Company_Location') or '').strip(),
    )
