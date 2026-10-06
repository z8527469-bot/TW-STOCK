import os
import re
import logging
import yfinance as yf
import pandas as pd
import requests
import warnings
from concurrent.futures import ThreadPoolExecutor, as_completed
import datetime
import time
import random
from tqdm import tqdm  # 改為標準版終端機進度條

# 屏蔽 yfinance 預設的紅色報錯訊息
logging.getLogger('yfinance').setLevel(logging.CRITICAL)
warnings.filterwarnings('ignore')

# ================= 參數設定區 =================
# 改由 GitHub Secrets 讀取，避免金鑰外洩
DISCORD_WEBHOOK_URL = os.environ.get("DISCORD_WEBHOOK_URL", "")
FINMIND_TOKEN = os.environ.get("FINMIND_TOKEN", "")
# ============================================

STOCK_INFO = {}
REVENUE_DATA = {}
error_count = 0
DEBUG_CHIPS_COUNT = 0
MARKET_BULL = True  

def check_market_regime():
    global MARKET_BULL
    print("⏳ 正在判斷大盤趨勢環境...")
    try:
        twii = yf.Ticker('^TWII').history(period="1y")
        if twii.empty:
            twii = yf.Ticker('0050.TW').history(period="1y")

        if len(twii) > 200:
            twii['MA50'] = twii['Close'].rolling(window=50).mean()
            twii['MA200'] = twii['Close'].rolling(window=200).mean()
            MARKET_BULL = twii['MA50'].iloc[-1] > twii['MA200'].iloc[-1]
    except Exception as e:
        pass

    status = "🟢 多頭 (允許 S1 底部突破)" if MARKET_BULL else "🔴 空頭或盤整 (濾除 S1，防禦假突破)"
    print(f"📊 大盤狀態: {status}\n")

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
                print(f"🔄 正在進行第 {attempt + 2} 次重試...")
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
            res = requests.get(url, headers=headers, timeout=10)
            res.encoding = 'big5'
            matches = re.findall(r'>([1-9][0-9]{3})\u3000([^<]+)</td>', res.text)
            for code, name in matches:
                ticker = f"{code}{suffix}"
                tickers.append(ticker)
                STOCK_INFO[ticker] = {'代碼': code, '名稱': name.strip(), '市場': market_name}
        except Exception as e:
            pass
    return sorted(list(set(tickers)))

def check_technical(ticker):
    global error_count
    try:
        time.sleep(random.uniform(0.1, 0.4))
        stock = yf.Ticker(ticker)
        df = stock.history(period="2y")

        if df.empty or len(df) < 252: return None
        df = df.dropna()

        df['MA5'] = df['Close'].rolling(window=5).mean()
        df['MA20'] = df['Close'].rolling(window=20).mean()
        df['MA60'] = df['Close'].rolling(window=60).mean()
        df['MA200'] = df['Close'].rolling(window=200).mean()
        df['Vol_20MA'] = df['Volume'].rolling(window=20).mean()

        delta = df['Close'].diff()
        gain = (delta.where(delta > 0, 0)).ewm(alpha=1/14, adjust=False).mean()
        loss = (-delta.where(delta < 0, 0)).ewm(alpha=1/14, adjust=False).mean()
        rs = gain / loss
        df['RSI'] = 100 - (100 / (1 + rs))

        lowest_low = df['Low'].rolling(window=9).min()
        highest_high = df['High'].rolling(window=9).max()
        rsv = 100 * (df['Close'] - lowest_low) / (highest_high - lowest_low)
        df['K'] = rsv.fillna(50).ewm(com=2, adjust=False).mean()

        today = df.iloc[-1]
        yesterday = df.iloc[-2]
        high_1y = df['High'].iloc[-252:].max()

        passed_strats = []

        # --- S1：底部突破 ---
        past_40_days = df['Close'].iloc[-41:-1]
        box_high, box_low = past_40_days.max(), past_40_days.min()
        ma_yest = [yesterday['MA5'], yesterday['MA20'], yesterday['MA60']]
        ma_max, ma_min = max(ma_yest), min(ma_yest)

        if (MARKET_BULL and
            box_low <= (high_1y * 0.75) and
            (box_high - box_low) / box_low <= 0.25 and
            (ma_max - ma_min) / ma_min <= 0.10 and
            (today['Close'] - yesterday['Close']) / yesterday['Close'] >= 0.04 and
            today['Close'] > ma_max and
            today['MA5'] > yesterday['MA5'] and
            today['Volume'] >= 1000000 and
            today['Volume'] >= (1.5 * today['Vol_20MA'])):
            passed_strats.append("S1_底部突破")

        # --- S2：創高動能 ---
        if (today['Close'] > today['MA20'] > today['MA60'] > today['MA200'] and
            today['MA200'] > df['MA200'].iloc[-20] and
            today['Close'] >= (high_1y * 0.85) and
            today['Close'] <= (today['MA200'] * 2.0) and
            today['Volume'] >= (2 * today['Vol_20MA']) and
            today['Close'] > today['Open'] and
            today['Volume'] >= 1000000):
            passed_strats.append("S2_創高動能")

        # --- S3：投信認養 ---
        if (today['Close'] > today['MA20'] and today['MA20'] > yesterday['MA20'] and
            (today['Close'] - today['MA20']) / today['MA20'] <= 0.10 and
            today['Volume'] >= 2000000 and (today['Close'] - today['Open']) / today['Open'] > 0.02):
            passed_strats.append("S3_投信認養")

        # --- S4：恐慌抄底 ---
        yest_real_body = abs(yesterday['Close'] - yesterday['Open'])
        yest_lower_shadow = min(yesterday['Open'], yesterday['Close']) - yesterday['Low']

        if (yesterday['RSI'] < 30 and yesterday['K'] < 20 and
            yesterday['Close'] <= (yesterday['MA20'] * 0.90) and
            yesterday['Volume'] >= (1.3 * yesterday['Vol_20MA']) and
            yest_lower_shadow >= (1.5 * yest_real_body) and
            yesterday['Volume'] >= 1000000 and
            today['Close'] > yesterday['High']):
            passed_strats.append("S4_恐慌抄底")

        if not passed_strats: return None

        info = STOCK_INFO.get(ticker, {})
        drop_pct = round((1 - today['Close'] / high_1y) * 100, 2)

        return {
            '代碼': info.get('代碼', ''),
            '名稱': info.get('名稱', ''),
            '市場': info.get('市場', ''),
            '收盤價': round(today['Close'], 2),
            '成交量(張)': int(today['Volume'] / 1000),
            '距年高點跌幅': f"{drop_pct}%",
            'strats': passed_strats
        }
    except Exception as e:
        if error_count < 3:
            print(f"\n⚠️ 股票 {ticker} 發生錯誤: {e}")
            error_count += 1
        return None

def check_revenue(stock_dict):
    ticker = stock_dict['代碼']
    code = stock_dict['代碼']
    strats = stock_dict['strats'].copy()

    def handle_fail(mom_val="-", yoy_val="-"):
        if "S1_底部突破" in strats:
            stock_dict['strats'] = ["S1_底部突破"]
            stock_dict['MoM'] = f"{mom_val}" if mom_val != "-" else "-"
            stock_dict['YoY'] = f"{yoy_val}" if yoy_val != "-" else "-"
            return stock_dict
        return None

    if code in REVENUE_DATA:
        rev = REVENUE_DATA[code]
        mom, yoy = rev['mom'], rev['yoy']
        if mom > 0 and yoy > 10:
            stock_dict['MoM'] = f"{mom:.1f}%"
            stock_dict['YoY'] = f"{yoy:.1f}%"
            return stock_dict
        else:
            return handle_fail(f"{mom:.1f}%", f"{yoy:.1f}%")

    start_date = (datetime.datetime.now() - datetime.timedelta(days=400)).strftime('%Y-%m-%d')
    token_param = f"&token={FINMIND_TOKEN}" if FINMIND_TOKEN else ""
    url = f"https://api.finmindtrade.com/api/v4/data?dataset=TaiwanStockMonthRevenue&data_id={ticker}&start_date={start_date}{token_param}"

    try:
        time.sleep(0.3)
        res = requests.get(url, timeout=5).json()
        if 'data' not in res or len(res['data']) < 13: return handle_fail()
        df = pd.DataFrame(res['data'])
        latest_rev = df.iloc[-1]['revenue']
        prev_rev = df.iloc[-2]['revenue']
        latest_month = df.iloc[-1]['revenue_month']
        latest_year = df.iloc[-1]['revenue_year']
        last_year_data = df[(df['revenue_year'] == latest_year - 1) & (df['revenue_month'] == latest_month)]

        if last_year_data.empty: return handle_fail()
        prev_yoy = last_year_data.iloc[0]['revenue']
        mom = (latest_rev / prev_rev - 1) * 100 if prev_rev else 0
        yoy = (latest_rev / prev_yoy - 1) * 100 if prev_yoy else 0

        if mom > 0 and yoy > 10:
            stock_dict['MoM'] = f"{mom:.1f}%"
            stock_dict['YoY'] = f"{yoy:.1f}%"
            return stock_dict
        else:
            return handle_fail(f"{mom:.1f}%", f"{yoy:.1f}%")
    except:
        return handle_fail()

def check_chips(stock_dict):
    global DEBUG_CHIPS_COUNT
    ticker = stock_dict['代碼']
    strats = stock_dict['strats'].copy()

    if "S1_底部突破" not in strats and "S3_投信認養" not in strats:
        return stock_dict

    start_date = (datetime.datetime.now() - datetime.timedelta(days=20)).strftime('%Y-%m-%d')
    token_param = f"&token={FINMIND_TOKEN}" if FINMIND_TOKEN else ""
    url = f"https://api.finmindtrade.com/api/v4/data?dataset=TaiwanStockInstitutionalInvestorsBuySell&data_id={ticker}&start_date={start_date}{token_param}"

    def fail_chip():
        rem_strats = [s for s in strats if s not in ["S1_底部突破", "S3_投信認養"]]
        stock_dict['strats'] = rem_strats
        return stock_dict if rem_strats else None

    try:
        time.sleep(0.5)
        res = requests.get(url, timeout=10)
        res_json = res.json()

        if 'msg' in res_json and 'limit' in res_json['msg'].lower():
            if DEBUG_CHIPS_COUNT < 3:
                print(f"\n🔍 [Debug] {ticker} 失敗：FinMind API 額度已滿！請確認 Token。")
                DEBUG_CHIPS_COUNT += 1
            return fail_chip()
        if 'data' not in res_json or not res_json['data']: return fail_chip()

        df_chip = pd.DataFrame(res_json['data'])
        df_chip['net'] = (pd.to_numeric(df_chip['buy'], errors='coerce').fillna(0) -
                          pd.to_numeric(df_chip['sell'], errors='coerce').fillna(0)) / 1000
        pivot = df_chip.pivot_table(index='date', columns='name', values='net', aggfunc='sum').fillna(0)
        dates = pivot.index.sort_values().tolist()
        if len(dates) < 3: return fail_chip()

        t0, t1 = dates[-1], dates[-2]
        foreign_cols = [c for c in pivot.columns if '外資' in c or 'Foreign' in c or 'foreign' in c]
        trust_cols = [c for c in pivot.columns if '投信' in c or 'Investment' in c or 'Trust' in c]

        f_t0 = sum(pivot.loc[t0, c] for c in foreign_cols) if foreign_cols else 0
        t_t0 = sum(pivot.loc[t0, c] for c in trust_cols) if trust_cols else 0
        t_t1 = sum(pivot.loc[t1, c] for c in trust_cols) if trust_cols else 0

        s1_pass = (f_t0 > 0) or (t_t0 > 0)
        if "S1_底部突破" in strats and not s1_pass: strats.remove("S1_底部突破")

        s3_pass = (t_t0 > 0 and t_t1 > 0) and (t_t0 >= 100)
        if "S3_投信認養" in strats and not s3_pass: strats.remove("S3_投信認養")

        stock_dict['外資動向'] = f"今:{int(f_t0)}張"
        stock_dict['投信動向'] = f"今:{int(t_t0)}張"
        stock_dict['strats'] = strats

        return stock_dict if strats else None
    except Exception as e:
        if DEBUG_CHIPS_COUNT < 3:
            print(f"\n🔍 [Debug] {ticker} 籌碼運算異常: {e}")
            DEBUG_CHIPS_COUNT += 1
        return fail_chip()

# ================= 主程式執行 =================
if __name__ == "__main__":
    check_market_regime()
    fetch_all_revenue()

    print("\n⏳ 步驟 1: 獲取全市場股票清單 (包含名稱)...")
    all_tickers = get_tw_stocks()
    print(f"✅ 共獲取 {len(all_tickers)} 檔股票。")

    print("\n🚀 步驟 2: 啟動多執行緒掃描技術面...")
    passed_technical = []

    with ThreadPoolExecutor(max_workers=5) as executor:
        futures = {executor.submit(check_technical, ticker): ticker for ticker in all_tickers}
        for future in tqdm(as_completed(futures), total=len(all_tickers), desc="技術面掃描"):
            result = future.result()
            if result: passed_technical.append(result)

    print(f"\n🔍 【階段一：技術面初篩完成】共 {len(passed_technical)} 檔")
    if passed_technical:
        tech_df = pd.DataFrame(passed_technical)
        tech_df['符合策略'] = tech_df['strats'].apply(lambda x: ", ".join(x))
        print(tech_df[['代碼', '名稱', '市場', '收盤價', '成交量(張)', '符合策略']].sort_values(by='成交量(張)', ascending=False).to_string(index=False))
    print("-" * 50)

    print(f"\n📊 步驟 3: 針對 {len(passed_technical)} 檔初篩名單進行【月營收雙增】驗證...")
    passed_revenue = []
    for stock in tqdm(passed_technical, desc="營收面掃描"):
        rev_result = check_revenue(stock)
        if rev_result: passed_revenue.append(rev_result)

    print(f"\n🔍 【階段二：營收雙增驗證完成】進入籌碼審查前，共 {len(passed_revenue)} 檔")
    if passed_revenue:
        rev_df = pd.DataFrame(passed_revenue)
        rev_df['符合策略'] = rev_df['strats'].apply(lambda x: ", ".join(x))
        rev_df.fillna('-', inplace=True)
        cols = ['代碼', '名稱', '收盤價', '成交量(張)']
        if 'MoM' in rev_df.columns: cols.extend(['MoM', 'YoY'])
        cols.append('符合策略')
        print(rev_df[cols].sort_values(by='成交量(張)', ascending=False).to_string(index=False))
    print("-" * 50)

    print(f"\n🏦 步驟 4: 針對 {len(passed_revenue)} 檔營收達標名單進行籌碼驗證...")
    final_stocks = []
    for stock in tqdm(passed_revenue, desc="籌碼面掃描"):
        chip_result = check_chips(stock)
        if chip_result: final_stocks.append(chip_result)

    # ================= 整理結果與發送 =================
    print("\n========== 🎯 四大策略最終篩選結果 ==========")

    results_by_strat = {"S1_底部突破": [], "S2_創高動能": [], "S3_投信認養": [], "S4_恐慌抄底": []}
    for stock in final_stocks:
        for s in stock['strats']:
            results_by_strat[s].append(stock)

    today_str = datetime.datetime.now().strftime("%Y-%m-%d")
    notify_msg = f"## 📊 【{today_str} 台股四核心選股報告】\n*附加條件：月營收 MoM>0 且 YoY>10% (S1豁免)*\n"

    for strat_name, stocks in results_by_strat.items():
        print(f"\n📁 【{strat_name}】符合標的：{len(stocks)} 檔")
        notify_msg += f"\n### 🎯 【{strat_name}】\n"

        if not stocks:
            print("今日無符合標的。")
            notify_msg += "> 無符合標的\n"
            continue

        df = pd.DataFrame(stocks)
        cols_order = ['代碼', '名稱', '市場', '收盤價', '成交量(張)', '距年高點跌幅']
        if 'MoM' in df.columns: cols_order.extend(['MoM', 'YoY'])
        if '外資動向' in df.columns: cols_order.extend(['外資動向', '投信動向'])

        cols_order = [c for c in cols_order if c in df.columns]
        df = df[cols_order].sort_values(by='成交量(張)', ascending=False).reset_index(drop=True)
        df.fillna('-', inplace=True)
        print(df.to_string(index=False))

        for _, row in df.iterrows():
            notify_msg += f"**📌 {row['代碼']} {row['名稱']} ({row['市場']})**\n"
            notify_msg += f"> 收盤: `{row['收盤價']}` ｜ 量: `{row['成交量(張)']}張` ｜ 距高: `{row['距年高點跌幅']}`\n"
            if 'MoM' in row:
                notify_msg += f"> 營收: MoM `{row['MoM']}` ｜ YoY `{row['YoY']}`\n"
            if '外資動向' in row and row['外資動向'] != '-':
                notify_msg += f"> 籌碼: 外資 `{row['外資動向']}` ｜ 投信 `{row['投信動向']}`\n"
        notify_msg += "───────────────\n
