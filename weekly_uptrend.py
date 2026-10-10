import os
import re
import json
import logging
import yfinance as yf
import pandas as pd
import requests
import warnings
from concurrent.futures import ThreadPoolExecutor, as_completed
import datetime
import time
from tqdm import tqdm

logging.getLogger('yfinance').setLevel(logging.CRITICAL)
warnings.filterwarnings('ignore')

STOCK_INFO = {}

def get_tw_stocks():
    headers = {"User-Agent": "Mozilla/5.0"}
    tickers = []
    markets = {'2': ('.TW', '上市'), '4': ('.TWO', '上櫃')}
    for mode, (suffix, market_name) in markets.items():
        try:
            res = requests.get(f"https://isin.twse.com.tw/isin/C_public.jsp?strMode={mode}", headers=headers, timeout=10)
            res.encoding = 'big5'
            matches = re.findall(r'>([1-9][0-9]{3})\u3000([^<]+)</td>', res.text)
            for code, name in matches:
                ticker = f"{code}{suffix}"
                tickers.append(ticker)
                STOCK_INFO[ticker] = {'代碼': code, '名稱': name.strip(), '市場': market_name}
        except:
            pass
    return sorted(list(set(tickers)))

def check_stage2_trend(ticker):
    try:
        stock = yf.Ticker(ticker)
        df = stock.history(period="2y")
        if df.empty or len(df) < 252: return None
        df = df.dropna()

        # 計算趨勢模板所需均線
        df['MA50'] = df['Close'].rolling(window=50).mean()
        df['MA150'] = df['Close'].rolling(window=150).mean()
        df['MA200'] = df['Close'].rolling(window=200).mean()
        df['Vol_50MA'] = df['Volume'].rolling(window=50).mean()

        today = df.iloc[-1]
        
        # 排除流動性過低或極低價的水餃股
        if today['Close'] < 10.0 or today['Vol_50MA'] < 500000:
            return None

        # 計算 52 週 (約 252 個交易日) 的高低點
        high_1y = df['High'].iloc[-252:].max()
        low_1y = df['Low'].iloc[-252:].min()

        # =====================================================================
        # 《超級績效》第2階段趨勢模板 8 大嚴格準則
        # =====================================================================
        # 1. 股價處於50天均線以上
        rule_1 = today['Close'] > today['MA50']
        # 2. 股價處於150天及200天均線以上
        rule_2 = today['Close'] > today['MA150'] and today['Close'] > today['MA200']
        # 3. 50天均線處於150天及200天均線以上
        rule_3 = today['MA50'] > today['MA150'] and today['MA50'] > today['MA200']
        # 4. 150天均線處於200天均線以上
        rule_4 = today['MA150'] > today['MA200']
        # 5. 200天均線趨勢向上至少一個月 (20個交易日)
        rule_5 = today['MA200'] > df['MA200'].iloc[-20]
        # 6. 股價至少高於52周新低點達25%以上
        rule_6 = today['Close'] >= low_1y * 1.25
        # 7 & 8. 台股無IBD RS Ranking，將第7點收窄為不低於新高點15%
        rule_7_8 = today['Close'] >= high_1y * 0.85

        if rule_1 and rule_2 and rule_3 and rule_4 and rule_5 and rule_6 and rule_7_8:
            info = STOCK_INFO.get(ticker, {})
            drop_pct = round((1 - today['Close'] / high_1y) * 100, 2)
            rebound_pct = round((today['Close'] / low_1y - 1) * 100, 2)
            
            return {
                '代碼': info.get('代碼', ''),
                '名稱': info.get('名稱', ''),
                '市場': info.get('市場', ''),
                '收盤價': round(today['Close'], 2),
                '成交量(張)': int(today['Volume'] / 1000),
                '距年高點跌幅': f"{drop_pct}%",
                '自低點反彈': f"+{rebound_pct}%"
            }
        return None
    except:
        return None

if __name__ == "__main__":
    print("⏳ 獲取全市場股票清單...")
    all_tickers = get_tw_stocks()
    passed_stocks = []

    print("\n🚀 啟動多執行緒掃描：超級績效第2階段趨勢模板...")
    with ThreadPoolExecutor(max_workers=5) as executor:
        futures = {executor.submit(check_stage2_trend, ticker): ticker for ticker in all_tickers}
        for future in tqdm(as_completed(futures), total=len(all_tickers)):
            result = future.result()
            if result: passed_stocks.append(result)

    # 依照距離高點最近(跌幅最小)排序，選出最強勢的標的
    passed_stocks.sort(key=lambda x: float(x['距年高點跌幅'].strip('%')))

    # 存入獨立的 JSON 檔案
    os.makedirs("data", exist_ok=True)
    today_str = datetime.datetime.now().strftime("%Y-%m-%d")
    output_data = {"date": today_str, "count": len(passed_stocks), "stocks": passed_stocks}
    
    with open("data/weekly_uptrend.json", "w", encoding="utf-8") as f:
        json.dump(output_data, f, ensure_ascii=False, indent=4)

    print(f"\n✅ 掃描完成！共有 {len(passed_stocks)} 檔股票處於完美的第二階段上升趨勢，已存入 weekly_uptrend.json")
