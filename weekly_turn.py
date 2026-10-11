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

def check_weekly_turn(ticker):
    try:
        stock = yf.Ticker(ticker)
        # 抓取一年日線資料
        df = stock.history(period="1y")
        if df.empty or len(df) < 50: return None
        
        # 移除時區資訊以便進行重採樣
        df.index = pd.to_datetime(df.index).tz_localize(None)
        
        # 將日K線重採樣為「週K線 (W-FRI 代表以週五為結算日)」
        df_w = df.resample('W-FRI').agg({
            'Open': 'first',
            'High': 'max',
            'Low': 'min',
            'Close': 'last',
            'Volume': 'sum'
        }).dropna()

        # 確保至少有 8 週的歷史資料來計算扣抵
        if len(df_w) < 8: return None

        # 當週 (最新一週) 收盤價與週成交量
        current_close = df_w['Close'].iloc[-1]
        current_vol = df_w['Volume'].iloc[-1]
        
        # 基礎流動性過濾：當週成交量小於 2000 張則跳過
        if current_vol < 2000000:
            return None

        # 抓取過去 1~6 週的收盤價
        c_w1 = df_w['Close'].iloc[-2] # 1週前
        c_w2 = df_w['Close'].iloc[-3] # 2週前
        c_w3 = df_w['Close'].iloc[-4] # 3週前
        c_w4 = df_w['Close'].iloc[-5] # 4週前
        c_w5 = df_w['Close'].iloc[-6] # 5週前
        c_w6 = df_w['Close'].iloc[-7] # 6週前

        # ==============================================================
        # 未來三週轉折價計算 (對應：(前N期 * 2) - 前N+3期)
        # ==============================================================
        # 明週轉折價 (W+1) = (3週前收盤價 * 2) - 6週前收盤價
        turn_w1 = (c_w3 * 2) - c_w6
        
        # 後週轉折價 (W+2) = (2週前收盤價 * 2) - 5週前收盤價
        turn_w2 = (c_w2 * 2) - c_w5
        
        # 大後週轉折價 (W+3) = (1週前收盤價 * 2) - 4週前收盤價
        turn_w3 = (c_w1 * 2) - c_w4

        # 核心篩選條件：當週收盤價 > 未來三週轉折價
        if current_close > turn_w1 and current_close > turn_w2 and current_close > turn_w3:
            info = STOCK_INFO.get(ticker, {})
            return {
                '代碼': info.get('代碼', ''),
                '名稱': info.get('名稱', ''),
                '市場': info.get('市場', ''),
                '本週收盤價': round(current_close, 2),
                '本週成交量(張)': int(current_vol / 1000),
                'W1轉折價': round(turn_w1, 2),
                'W2轉折價': round(turn_w2, 2),
                'W3轉折價': round(turn_w3, 2)
            }
        return None
    except:
        return None

if __name__ == "__main__":
    print("⏳ 獲取全市場股票清單...")
    all_tickers = get_tw_stocks()
    passed_stocks = []

    print("\n🚀 啟動多執行緒掃描：當週收盤大於未來三週轉折價...")
    with ThreadPoolExecutor(max_workers=5) as executor:
        futures = {executor.submit(check_weekly_turn, ticker): ticker for ticker in all_tickers}
        for future in tqdm(as_completed(futures), total=len(all_tickers)):
            result = future.result()
            if result: passed_stocks.append(result)

    # 以成交量由大到小排序
    passed_stocks.sort(key=lambda x: x['本週成交量(張)'], reverse=True)

    # 存入獨立的 JSON 檔案
    os.makedirs("data", exist_ok=True)
    today_str = datetime.datetime.now().strftime("%Y-%m-%d")
    output_data = {"date": today_str, "count": len(passed_stocks), "stocks": passed_stocks}
    
    with open("data/weekly_turn.json", "w", encoding="utf-8") as f:
        json.dump(output_data, f, ensure_ascii=False, indent=4)

    print(f"\n✅ 掃描完成！共有 {len(passed_stocks)} 檔股票高於未來三週轉折價，已存入 weekly_turn.json")
