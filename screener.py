import os
import yfinance as yf
import pandas as pd
import requests
import warnings
from concurrent.futures import ThreadPoolExecutor, as_completed
import datetime
import time
import random

# 忽略警告訊息
warnings.filterwarnings('ignore')

# ================= 參數設定區 =================
# 優先讀取 GitHub Secrets 環境變數
DISCORD_WEBHOOK_URL = os.environ.get("DISCORD_WEBHOOK_URL", "請在此貼上你的_DISCORD_WEBHOOK_URL")
# ============================================

STOCK_INFO = {}
error_count = 0

def send_discord_webhook(webhook_url, msg):
    if not webhook_url or webhook_url == "請在此貼上你的_DISCORD_WEBHOOK_URL":
        print("⚠️ 未設定 Discord Webhook，跳過發送。")
        return
    try:
        res = requests.post(webhook_url, json={"content": msg})
        if res.status_code not in [200, 204]:
            print(f"❌ Discord 發送失敗：{res.status_code}")
    except Exception as e:
        print(f"❌ Discord 錯誤：{e}")

def get_tw_stocks():
    """獲取上市櫃純數字普通股清單與名稱"""
    headers = {"User-Agent": "Mozilla/5.0"}
    tickers = []
    markets = {'2': '.TW', '4': '.TWO'}
    for mode, suffix in markets.items():
        try:
            res = requests.get(f"https://isin.twse.com.tw/isin/C_public.jsp?strMode={mode}", headers=headers)
            res.encoding = 'big5'
            df = pd.read_html(res.text)[0]
            df.columns = df.iloc[0]
            df = df.iloc[1:]
            if '有價證券代號及名稱' in df.columns:
                for item in df['有價證券代號及名稱'].dropna():
                    parts = item.split('\u3000')
                    code = parts[0].strip()
                    name = parts[1].strip() if len(parts) > 1 else ""
                    if len(code) == 4 and code.isdigit():
                        ticker = f"{code}{suffix}"
                        tickers.append(ticker)
                        STOCK_INFO[ticker] = {
                            '代碼': code,
                            '名稱': name,
                            '市場': "上市" if mode == '2' else "上櫃"
                        }
        except:
            pass
    return sorted(list(set(tickers)))

def check_technical(ticker):
    global error_count
    try:
        # ✅ 降速機制：隨機延遲 0.5 到 1.2 秒，避免被 Yahoo 判定為惡意爬蟲
        time.sleep(random.uniform(0.5, 1.2))
        
        stock = yf.Ticker(ticker)
        df = stock.history(period="2y")
        
        if df.empty or len(df) < 252: return None
        df = df.dropna()

        # 均線與指標
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
        past_60_days = df['Close'].iloc[-61:-1]
        box_high, box_low = past_60_days.max(), past_60_days.min()
        ma_yest = [yesterday['MA5'], yesterday['MA20'], yesterday['MA60']]
        ma_max, ma_min = max(ma_yest), min(ma_yest)

        if (box_high <= (high_1y * 0.70) and (box_high - box_low) / box_low <= 0.20 and
            (ma_max - ma_min) / ma_min <= 0.05 and (today['Close'] - today['Open']) / today['Open'] >= 0.03 and
            today['Close'] > ma_max and today['MA5'] > yesterday['MA5'] and
            today['Volume'] >= 1000000 and today['Close'] > today['MA200']):
            passed_strats.append("S1_底部突破")

        # --- S2：創高動能 ---
        if (today['Close'] > today['MA20'] > today['MA60'] > today['MA200'] and
            today['MA200'] > df['MA200'].iloc[-20] and today['Close'] >= (high_1y * 0.85) and
            today['Volume'] >= (2 * today['Vol_20MA']) and today['Close'] > today['Open'] and
            today['Volume'] >= 1000000):
            passed_strats.append("S2_創高動能")

        # --- S3：投信認養 ---
        if (today['Close'] > today['MA20'] and today['MA20'] > yesterday['MA20'] and
            (today['Close'] - today['MA20']) / today['MA20'] <= 0.10 and
            today['Volume'] >= 2000000 and (today['Close'] - today['Open']) / today['Open'] > 0.02):
            passed_strats.append("S3_投信認養")

        # --- S4：恐慌抄底 ---
        real_body = abs(today['Close'] - today['Open'])
        lower_shadow = min(today['Open'], today['Close']) - today['Low']
        if (today['RSI'] < 25 and today['K'] < 20 and today['Close'] <= (today['MA20'] * 0.85) and
            today['Volume'] >= (1.5 * today['Vol_20MA']) and lower_shadow >= (2 * real_body) and
            today['Volume'] >= 1000000):
            passed_strats.append("S4_恐慌抄底")

        if not passed_strats: return None
        
        info = STOCK_INFO.get(ticker, {})
        return {
            '代碼': info.get('代碼', ''),
            '名稱': info.get('名稱', ''),
            '市場': info.get('市場', ''),
            '收盤價': round(today['Close'], 2),
            '成交量(張)': int(today['Volume'] / 1000),
            '距年高點跌幅': f"{round((1 - today['Close']/high_1y)*100, 2)}%",
            'strats': passed_strats
        }
    except Exception as e:
        if error_count < 3:
            print(f"⚠️ 股票 {ticker} 發生錯誤: {e}")
            error_count += 1
        return None

def check_revenue(stock_dict):
    """階段二：月營收雙增濾網 (MoM > 0 且 YoY > 10%)"""
    ticker = stock_dict['代碼']
    start_date = (datetime.datetime.now() - datetime.timedelta(days=400)).strftime('%Y-%m-%d')
    url = f"https://api.finmindtrade.com/api/v4/data?dataset=TaiwanStockMonthRevenue&data_id={ticker}&start_date={start_date}"
    
    try:
        time.sleep(0.25) # 防火牆保護延遲
        res = requests.get(url, timeout=5).json()
        if 'data' not in res or len(res['data']) < 13: return None
            
        df = pd.DataFrame(res['data'])
        latest_rev = df.iloc[-1]['revenue']
        prev_rev = df.iloc[-2]['revenue']
        
        latest_month = df.iloc[-1]['revenue_month']
        latest_year = df.iloc[-1]['revenue_year']
        last_year_data = df[(df['revenue_year'] == latest_year - 1) & (df['revenue_month'] == latest_month)]
        
        if last_year_data.empty: return None
        prev_yoy = last_year_data.iloc[0]['revenue']
        
        mom = (latest_rev / prev_rev - 1) * 100 if prev_rev else 0
        yoy = (latest_rev / prev_yoy - 1) * 100 if prev_yoy else 0
        
        if mom > 0 and yoy > 10:
            stock_dict['MoM'] = f"{mom:.1f}%"
            stock_dict['YoY'] = f"{yoy:.1f}%"
            return stock_dict
        return None
    except:
        return None

def check_chips(stock_dict):
    """階段三：針對 S1, S3 確認籌碼面"""
    ticker = stock_dict['代碼']
    strats = stock_dict['strats'].copy()

    if "S1_底部突破" not in strats and "S3_投信認養" not in strats:
        return stock_dict

    start_date = (datetime.datetime.now() - datetime.timedelta(days=15)).strftime('%Y-%m-%d')
    url = f"https://api.finmindtrade.com/api/v4/data?dataset=TaiwanStockInstitutionalInvestorsBuySell&data_id={ticker}&start_date={start_date}"

    try:
        time.sleep(0.25)
        res = requests.get(url, timeout=5).json()
        if 'data' not in res or not res['data']:
            strats = [s for s in strats if s not in ["S1_底部突破", "S3_投信認養"]]
            stock_dict['strats'] = strats
            return stock_dict if strats else None
            
        df_chip = pd.DataFrame(res['data'])
        df_chip['net'] = (df_chip['buy'] - df_chip['sell']) / 1000
        pivot = df_chip.pivot_table(index='date', columns='name', values='net', aggfunc='sum').fillna(0)
        dates = pivot.index.sort_values().tolist()

        if len(dates) < 3:
            strats = [s for s in strats if s not in ["S1_底部突破", "S3_投信認養"]]
            stock_dict['strats'] = strats
            return stock_dict if strats else None

        t0, t1 = dates[-1], dates[-2]
        foreign_cols = [c for c in pivot.columns if '外資' in c]
        trust_cols = [c for c in pivot.columns if '投信' in c]

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
    except:
        strats = [s for s in strats if s not in ["S1_底部突破", "S3_投信認養"]]
        stock_dict['strats'] = strats
        return stock_dict if strats else None

# ================= 主程式執行 =================
if __name__ == "__main__":
    print("⏳ 步驟 1: 獲取全市場股票清單 (包含名稱)...")
    all_tickers = get_tw_stocks()
    print(f"✅ 共獲取 {len(all_tickers)} 檔股票。")

    print("\n🚀 步驟 2: 啟動多執行緒掃描技術面 (背景執行中，請耐心等候約 15-20 分鐘)...")
    passed_technical = []
    
    # ✅ 降速機制：並發數設為 2，確保 GitHub Actions 穩定運行不被封鎖
    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = {executor.submit(check_technical, ticker): ticker for ticker in all_tickers}
        # 移除 tqdm，改為單純迭代
        for future in as_completed(futures):
            result = future.result()
            if result: passed_technical.append(result)

    print(f"\n📊 步驟 3: 針對 {len(passed_technical)} 檔初篩名單進行【月營收雙增】驗證...")
    passed_revenue = []
    for stock in passed_technical:
        rev_result = check_revenue(stock)
        if rev_result: passed_revenue.append(rev_result)

    print(f"\n🏦 步驟 4: 針對 {len(passed_revenue)} 檔營收達標名單進行籌碼驗證...")
    final_stocks = []
    for stock in passed_revenue:
        chip_result = check_chips(stock)
        if chip_result: final_stocks.append(chip_result)

    # ================= 整理結果與發送 =================
    print("\n========== 🎯 四大策略最終篩選結果 ==========")

    results_by_strat = {"S1_底部突破": [], "S2_創高動能": [], "S3_投信認養": [], "S4_恐慌抄底": []}
    for stock in final_stocks:
        for s in stock['strats']:
            results_by_strat[s].append(stock)

    today_str = datetime.datetime.now().strftime("%Y-%m-%d")
    notify_msg = f"## 📊 【{today_str} 台股四核心選股報告】\n*附加條件：月營收 MoM>0 且 YoY>10%*\n"

    for strat_name, stocks in results_by_strat.items():
        print(f"\n📁 【{strat_name}】符合標的：{len(stocks)} 檔")
        notify_msg += f"\n### 🎯 【{strat_name}】\n"

        if not stocks:
            print("今日無符合標的。")
            notify_msg += "> 無符合標的\n"
            continue

        df = pd.DataFrame(stocks)
        cols_order = ['代碼', '名稱', '市場', '收盤價', '成交量(張)', '距年高點跌幅', 'MoM', 'YoY']
        if '外資動向' in df.columns: cols_order.extend(['外資動向', '投信動向'])

        df = df[cols_order].sort_values(by='成交量(張)', ascending=False).reset_index(drop=True)
        df.fillna('-', inplace=True)
        
        # 移除 display，改用標準 print 輸出到終端機 (不顯示 index 以保持整潔)
        print(df.to_string(index=False))

        for _, row in df.iterrows():
            notify_msg += f"**📌 {row['代碼']} {row['名稱']} ({row['市場']})**\n"
            notify_msg += f"> 收盤: `{row['收盤價']}` ｜ 量: `{row['成交量(張)']}張` ｜ 距高: `{row['距年高點跌幅']}`\n"
            notify_msg += f"> 營收: MoM `{row['MoM']}` ｜ YoY `{row['YoY']}`\n"
            if '外資動向' in row and row['外資動向'] != '-':
                notify_msg += f"> 籌碼: 外資 `{row['外資動向']}` ｜ 投信 `{row['投信動向']}`\n"
        notify_msg += "───────────────\n"

    send_discord_webhook(DISCORD_WEBHOOK_URL, notify_msg)
    print("\n✅ 選股作業結束，通知已發送！")
