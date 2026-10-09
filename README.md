# quotation-generator

聖大國際的報價單產生器：在網頁表單填好客戶與品項，產生套用 `template.xlsx` 的報價單，提供 PDF 與 Excel 下載。

- 輸入統一編號會自動向[經濟部商業司公開資料](https://data.gcis.nat.gov.tw/)查公司名稱與地址；手動填寫的欄位優先。
- 金額由範本內的公式計算：選「未稅」時應稅欄標 `T`，另加 5% 稅額；選「含稅」則不另外加稅。
- 品項最多 10 項（範本只有 10 列），格式為一行一項 `商品名稱,數量,單價`，數量與單價可以有小數；名稱和金額裡不能再有逗號（避免 `1,000` 被讀成 1）。
- 品名或備註太長時會自動換行，超過報價單放得下的長度會直接提示，不會在 PDF 上被截掉。

## 安裝

需要 Python 3.9 以上。

```bash
python -m venv venv
source venv/bin/activate        # Windows：venv\Scripts\activate
pip install -r requirements.txt
```

PDF 轉檔：

- **Windows**：用本機的 Microsoft Excel 轉檔（`pywin32` 會自動安裝），和以前的做法一樣。
- **macOS / Linux**：安裝 [LibreOffice](https://www.libreoffice.org/)，程式會呼叫 `soffice --headless` 轉檔。
  範本用的是 Windows 的「新細明體」，Linux 上要另外裝中文字型，否則 PDF 裡的中文會變成方框，例如
  `sudo apt install libreoffice-calc fonts-noto-cjk`。
- 轉檔失敗時仍可下載 Excel 檔。

## 執行

```bash
flask --app main run            # 開發時加 --debug
```

打開 <http://127.0.0.1:5000/>。正式環境可用 `et_test.wsgi`（mod_wsgi）或任何 WSGI 伺服器載入 `main:app`。

### 設定（環境變數）

| 變數 | 預設 | 說明 |
| --- | --- | --- |
| `QUOTATION_OUTPUT_DIR` | `./output` | 產生的檔案放這裡，每張報價單一個資料夾，24 小時後自動清除 |
| `PDF_BACKEND` | `auto` | `excel`、`libreoffice` 或 `auto`（Windows 用 Excel，其他用 LibreOffice） |
| `SOFFICE_PATH` | 自動尋找 | LibreOffice `soffice` 執行檔路徑 |
| `QUOTATION_USERNAME`、`QUOTATION_PASSWORD` | 未設定 | 兩個都設定時，整個網站要先登入（HTTP Basic Auth）。報價單上有公司發票章，放到網路上時務必開啟並使用 HTTPS |

銷售員、發貨方式、付款方式等下拉選單的選項在 `quotation.py` 開頭修改。

## 專案結構

| 檔案 | 用途 |
| --- | --- |
| `main.py` | Flask 路由：表單、產生、下載 |
| `quotation.py` | 驗證表單資料、填寫 Excel 範本 |
| `company.py` | 用統編查公司資料 |
| `pdf_export.py` | xlsx 轉 PDF（Excel 或 LibreOffice） |
| `template.xlsx` | 報價單範本（工作表「报价单」） |

## 測試

```bash
pip install -r requirements-dev.txt
pytest
```

有安裝 LibreOffice 時會多跑一個實際轉 PDF、檢查金額的測試。
