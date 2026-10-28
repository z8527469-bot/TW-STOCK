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
import random
from tqdm import tqdm

# 屏蔽 yfinance 預設警告與資訊
logging.getLogger('yfinance').setLevel(logging.CRITICAL)
warnings.filterwarnings('ignore')

# ================= 參數設定區 =================
DISCORD_WEBHOOK_URL = os.environ.get("DISCORD_WEBHOOK_URL", "")
FINMIND_TOKEN = os.environ.get("FINMIND_TOKEN", "").strip()
# ============================================

STOCK_INFO = {}
REVENUE_DATA = {}
TWII_SERIES = pd.Series(dtype=float)
WEEKLY_TURN_SET = set()
HOLDERS_DATA = {}
error_count = 0
DEBUG_CHIPS_COUNT = 0
MARKET_BULL = True

def check_market_regime():
    """建立大盤環境濾網，並儲存大盤收盤價序列供 RS 相對強度線計算"""
    global MARKET_BULL, TWII_SERIES
    print("⏳ 正在判斷大盤趨勢環境並建立 RS 基準線...")
    try:
        twii = yf.Ticker('^TWII').history(period="2y")
        if twii.empty:
            twii = yf.Ticker('0050.TW').history(period="2y")

        if not twii.empty:
            twii.index = pd.to_datetime(twii.index).tz_localize(None).normalize()
            TWII_SERIES = twii['Close']

        if len(twii) > 200:
            twii['MA50'] = twii['Close'].rolling(window=50).mean()
            twii['MA200'] = twii['Close'].rolling(window=200).mean()
            MARKET_BULL = twii['MA50'].iloc[-1] > twii['MA200'].iloc[-1]
    except Exception as e:
        print(f"⚠️ 大盤資料讀取異常: {e}")

    status = "🟢 多頭 (允許 S1 底部突破)" if MARKET_BULL else "🔴 空頭或盤整 (濾除 S1，防禦假突破)"
    print(f"📊 大盤狀態: {status}\n")


def load_auxiliary_databases():
    """載入週轉折與大戶籌碼資料庫，供跨維度共振比對使用"""
    global WEEKLY_TURN_SET, HOLDERS_DATA
    if os.path.exists("data/weekly_turn.json"):
        try:
            with open("data/weekly_turn.json", "r", encoding="utf-8") as f:
                data = json.load(f)
                for s in data.get("stocks", []):
                    WEEKLY_TURN_SET.add(str(s.get("代碼", "")))
            print(f"🔄 成功載入週轉折資料庫 ({len(WEEKLY_TURN_SET)} 檔強勢股)")
        except Exception as e:
            print(f"⚠️ 讀取 weekly_turn.json 失敗: {e}")

    if os.path.exists("data/holders.json"):
        try:
            with open("data/holders.json", "r", encoding="utf-8") as f:
                HOLDERS_DATA = json.load(f)
            print(f"🐳 成功載入集保大戶資料庫 ({len(HOLDERS_DATA)} 檔標的)")
        except Exception as e:
            print(f"⚠️ 讀取 holders.json 失敗: {e}")


def send_discord_webhook(webhook_url, msg):
    if not webhook_url:
        print("⚠️ 未設定 DISCORD_WEBHOOK_URL，略過發送通知。")
        return
    try:
        chunk_size = 1900
        for i in range(0, len(msg), chunk_size):
            chunk = msg[i:i+chunk_size]
            res = requests.post(webhook_url, json={"content": chunk})
            if res.status_code not in [200, 204]:
                print(f"❌ Discord 發送失敗 (狀態碼 {res.status_code})：{res.text}")
            time.sleep(1)
    except Exception as e:
        print(f"❌ Discord 程式執行錯誤：{e}")


def fetch_all_revenue():
    print("📥 正在從政府開放資料庫下載全市場月營收 (0 額度消耗)...")
    urls = {
        "上市": "https://openapi.twse.com.tw/v1/opendata/t187ap05_L",
        "上櫃": "https://www.tpex.org.tw/openapi/v1/mopsfin_t187ap05_O"
    }
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Accept": "application/json",
        "Connection": "close"
    }

    for market, url in urls.items():
        max_retries = 3
        for attempt in range(max_retries):
            try:
                res = requests.get(url, headers=headers, timeout=30)
                if res.status_code == 200:
                    try:
                        data = res.json()
                        for item in data:
                            code = item.get("公司代號")
                            if not code: continue
                            yoy_str = str(item.get("營業收入-去年同月增減(%)", "0")).replace(',', '')
                            mom_str = str(item.get("營業收入-上月比較增減(%)", "0")).replace(',', '')
                            try:
                                REVENUE_DATA[code] = {'yoy': float(yoy_str), 'mom': float(mom_str)}
                            except ValueError:
                                REVENUE_DATA[code] = {'yoy': 0.0, 'mom': 0.0}
                        break
                    except Exception as e:
                        print(f"⚠️ {market}營收解析失敗 ({e})")
                else:
                    print(f"⚠️ {market}營收連線拒絕 (狀態碼 {res.status_code})")
            except Exception as e:
                print(f"⚠️ {market}營收下載中斷: {e}")

            if attempt < max_retries - 1:
                time.sleep(2)
            else:
                print(f"❌ {market}營收下載失敗，後續將自動轉由 FinMind 補查。")

    if REVENUE_DATA:
        print(f"✅ 成功下載 {len(REVENUE_DATA)} 檔股票的月營收資料！")


def get_tw_stocks():
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
    tickers = []
    markets = {'2': ('.TW', '上市'), '4': ('.TWO', '上櫃')}
    for mode, (suffix, market_name) in markets.items():
        try:
            url = f"https://isin.twse.com.tw/isin/C_public.jsp?strMode={mode}"
            res = requests.get(url, headers=headers, timeout=1
