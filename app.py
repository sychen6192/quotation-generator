from openpyxl import load_workbook
import requests, datetime, pdfkit
import pandas as pd


headers = {'user-agent': 'Mozilla/5.0 (Macintosh Intel Mac OS X 10_13_4) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/66.0.3359.181 Safari/537.36'}
api_url = "https://data.gcis.nat.gov.tw/od/data/api/5F64D864-61CB-4D0D-8AD9-492047CC1EA6?$format=json&$filter=Business_Accounting_NO%20eq%2"
datetime_dt = datetime.datetime.today()# 獲得當地時間
datetime_str = datetime_dt.strftime("%Y/%m/%d")  # 格式化日期


def getCompanyInfo(id):
    try:
        res = requests.get('https://data.gcis.nat.gov.tw/od/data/api/5F64D864-61CB-4D0D-8AD9-492047CC1EA6?$format=json&$filter=Business_Accounting_NO%20eq%20'+id)
        return res.json()
    except:
        return '404'


wb = load_workbook('uutd5-xunqs.xlsx')
sheet = wb['报价单']

sheet['G3'] = datetime_str
# deadline
dateB = int(input('報價有效天數：'))
sheet['G8'] = (datetime_dt + datetime.timedelta(days=dateB)).strftime("%Y/%m/%d")
# name
name = input('報價客戶姓名：')
sheet['B9'] = f'姓名：{name}'
cId = input('報價公司統編：')
res = getCompanyInfo(cId)[0]
sheet['B10'] = f'公司名稱：'+res['Company_Name']
sheet['B11'] = f'統一編號：'+res['Business_Accounting_NO']
sheet['B12'] = f'公司地址：'+res['Company_Location']
cPhone = input('報價公司電話：')
sheet['B13'] = f'公司電話：'+cPhone
ps = input('備註：')
if ps:
    sheet['C16'] = ps
else:
    sheet['C16'] = '無'

product = []
while True:
    pp = input('請輸入品項 (商品, 數量, 單價)，結束請按b:')
    if pp[0] == 'b':
        break
    product.append(pp.split(','))

tax = input('含稅(y/n)：')
for i in range(24, 24+len(product)):
    sheet.merge_cells(f'C{i}:D{i}')
    sheet['B'+str(i)] = product[i-24][1]
    sheet['C'+str(i)] = product[i-24][0]
    sheet['E'+str(i)] = product[i-24][2]
    if tax == 'n':
        sheet['F' + str(i)] = 'T'
    else:
        sheet['F' + str(i)] = ''

name = input('售貨員：')
sheet['B20'] = name
delivery = input('發貨日+n：')
sheet['C20'] = (datetime_dt + datetime.timedelta(days=int(delivery))).strftime("%Y/%m/%d")
delivery_way = input('發貨方式：')
sheet['D20'] = delivery_way
cash = input('付款方式：')
sheet['F20'] = cash

wb.save('out.xlsx')
df = pd.read_excel("out.xlsx")
df.to_html("out.html")
pdfkit.from_file("out.html", "out.pdf")
