# quotation-generator

聖大國際的報價單產生器：在網頁表單填好客戶與品項，產生套用 `template.xlsx` 的報價單（PDF 與 Excel），並保留報價紀錄。

## 功能

- **報價單號**：自動取號，格式 `SD` + 年月 + 當月流水號（例如 `SD202610-001`），印在報價單右上角，也是下載的檔名。
- **歷史報價**：所有報價單都會保存，可以用單號、客戶、公司、統編、電話或品名搜尋（多個關鍵字要全部符合），隨時重新下載當初送出的同一份 PDF。
- **複製這張**：從舊報價單帶入客戶與品項，產生時給新單號和今天的日期。
- **狀態**：已報價 → 已成交／未成交／作廢（可以改回）；超過有效日期的「已報價」會標示「已過期」。報價單不會刪除，不要的改成作廢，單號不會重複使用。
- **統編帶入客戶資料**：先找這個統編上次報價的資料（含聯絡人、電話），沒有再查[經濟部商業司公開資料](https://data.gcis.nat.gov.tw/)；只會填空白的欄位，手動輸入的優先。
- **報價過的品項**：可以從清單加入品項並帶出上次的單價；有填統編時優先帶這個客戶上次的價格。
- **即時試算**：輸入品項時就顯示小計、稅額、總計和格式錯誤。
- **常用備註**：按鈕一按就加入「運費另計」這類常用文字（在 `quotation.py` 的 `NOTE_PRESETS` 修改）。
- **記住常用選項**：每位銷售員常用的發貨方式、付款方式、有效天數、稅別會記在瀏覽器裡，下次自動帶入。

## 金額怎麼算

- 品項一行一項 `商品名稱,數量,單價`，最多 10 項（範本只有 10 列）；數量與單價可以有小數。名稱和金額裡不能再有逗號（避免 `1,000` 被讀成 1）。
- 折扣寫成單價為負數的一列（`折扣,1,-500`），運費寫成一般品項（`運費,1,150`）。
- 每列金額四捨五入到分；營業稅和發票一樣**四捨五入到元**。網頁上的數字和報價單上 Excel 公式的算法相同，有測試確認兩者一致。
- **未稅**：應稅欄標 `T`，另加 5% 稅額。
- **含稅**：不另外加稅，改顯示「內含稅額」（總額 × 5/105），網頁上另外列出未稅金額。
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
- 轉檔失敗時仍可下載 Excel 檔，之後在報價單頁面按「產生並下載 PDF」重試。

## 執行

```bash
flask --app main run            # 開發時加 --debug
```

打開 <http://127.0.0.1:5000/>。正式環境可用 `et_test.wsgi`（mod_wsgi）或任何 WSGI 伺服器載入 `main:app`。

### 設定（環境變數）

| 變數 | 預設 | 說明 |
| --- | --- | --- |
| `QUOTATION_DATA_DIR` | 專案資料夾下的 `data/` | 報價紀錄資料庫 `quotes.sqlite3` 和每張報價單的檔案 `archive/<單號>/` 都放這裡；相對路徑以啟動時的工作目錄為準 |
| `QUOTATION_USERNAME`、`QUOTATION_PASSWORD` | 未設定 | 兩個都設定時，整個網站要先登入（HTTP Basic Auth）。報價紀錄有客戶資料、報價單上有公司發票章，放到網路上時務必開啟並使用 HTTPS |
| `PDF_BACKEND` | `auto` | `excel`、`libreoffice` 或 `auto`（Windows 用 Excel，其他用 LibreOffice） |
| `SOFFICE_PATH` | 自動尋找 | LibreOffice `soffice` 執行檔路徑 |

其他設定在 `quotation.py` 開頭：銷售員與下拉選單選項、報價單底部各銷售員的聯絡方式（`SELLER_CONTACTS`）、單號前綴（`QUOTE_NO_PREFIX`）、常用備註（`NOTE_PRESETS`）。

### 備份

`data/` 裡是公司的報價紀錄，請定期備份，並放在本機硬碟（不要放在網路磁碟或雲端同步資料夾裡直接使用）：

```bash
flask --app main backup D:\備份        # 會建立 quotation-backup-日期時間/ 資料夾
```

這個指令用 SQLite 的線上備份，網站運作中也能安全執行，Windows 不需要另外安裝 sqlite3；可以用「工作排程器」或 cron 每天執行。要還原時，把備份資料夾裡的 `quotes.sqlite3` 和 `archive/` 放回 `data/`。

## 專案結構

| 檔案 | 用途 |
| --- | --- |
| `main.py` | Flask 路由：表單、產生、歷史報價、下載、狀態、即時試算、備份指令 |
| `quotation.py` | 驗證表單、計算金額、填寫 Excel 範本、設定值 |
| `store.py` | 報價紀錄（SQLite）：取號、快照、搜尋、狀態、客戶與品項記憶 |
| `company.py` | 用統編查公司資料 |
| `pdf_export.py` | xlsx 轉 PDF（Excel 或 LibreOffice） |
| `static/form.js` | 表單上的即時試算、統編帶入、品項清單、常用備註（沒有 JavaScript 時表單照常可用） |
| `template.xlsx` | 報價單範本（工作表「报价单」） |

## 測試

```bash
pip install -r requirements-dev.txt
pytest
```

有安裝 LibreOffice 時，會多跑實際轉 PDF 的測試：金額、一頁印完、中文字型有嵌入，以及 LibreOffice 和 Python 的四捨五入結果一致。
