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
        time.sleep(random.uniform(0.1, 0.3))
        stock = yf.Ticker(ticker)
        df = stock.history(period="2y")

        if df.empty or len(df) < 252: return None
        df = df.dropna()
        df.index = pd.to_datetime(df.index).tz_localize(None).normalize()

        df['MA5'] = df['Close'].rolling(window=5).mean()
        df['MA10'] = df['Close'].rolling(window=10).mean()
        df['MA20'] = df['Close'].rolling(window=20).mean()
        df['MA50'] = df['Close'].rolling(window=50).mean()
        df['MA60'] = df['Close'].rolling(window=60).mean()
        df['MA150'] = df['Close'].rolling(window=150).mean()
        df['MA200'] = df['Close'].rolling(window=200).mean()
        df['Vol_5MA'] = df['Volume'].rolling(window=5).mean()
        df['Vol_20MA'] = df['Volume'].rolling(window=20).mean()
        df['Vol_50MA'] = df['Volume'].rolling(window=50).mean()

        today = df.iloc[-1]
        yesterday = df.iloc[-2]
        
        # 濾除低流動性與極端水餃股
        if today['Close'] < 20.0 or today['Vol_20MA'] < 600000 or today['Volume'] < 500000:
            return None

        high_1y = df['High'].iloc[-252:].max()
        low_1y = df['Low'].iloc[-252:].min()

        # 1. 計算真實台股相對強度線 (RS Line = 個股收盤 / 加權收盤)
        rs_strong = True
        rs_new_high = False
        if not TWII_SERIES.empty:
            aligned_twii = TWII_SERIES.reindex(df.index).ffill()
            if not aligned_twii.isna().all():
                df['RS_Line'] = df['Close'] / aligned_twii
                df['RS_MA20'] = df['RS_Line'].rolling(window=20).mean()
                rs_strong = bool((df['RS_Line'].iloc[-1] > df['RS_MA20'].iloc[-1]) and (df['RS_Line'].iloc[-1] > df['RS_Line'].iloc[-10]))
                rs_new_high = bool(df['RS_Line'].iloc[-1] >= (df['RS_Line'].iloc[-60:].max() * 0.98))

        # 2. 當日 K 棒收盤位階 (Close Position)，避免長上影線隔日沖陷阱
        day_range = today['High'] - today['Low']
        close_pos = float((today['Close'] - today['Low']) / day_range) if day_range > 0 else 0.0

        # Stage 2 趨勢樣板
        stage2_trend = (
            today['Close'] > today['MA50'] > today['MA150'] > today['MA200'] and
            today['MA200'] > df['MA200'].iloc[-20] and
            today['Close'] >= (low_1y * 1.25) and
            today['Close'] >= 25.0
        )

        passed_strats = []

        # S1：底部突破 (含大盤濾網 + RS領先 + 收盤位階>=65%)
        past_40_days_close = df['Close'].iloc[-41:-1]
        box_high, box_low = past_40_days_close.max(), past_40_days_close.min()
        ma_yest = [yesterday['MA5'], yesterday['MA20'], yesterday['MA60']]
        ma_max, ma_min = max(ma_yest), min(ma_yest)

        if (MARKET_BULL and rs_strong and
            box_low <= (high_1y * 0.75) and
            (box_high - box_low) / box_low <= 0.20 and
            (ma_max - ma_min) / ma_min <= 0.10 and
            (today['Close'] - yesterday['Close']) / yesterday['Close'] >= 0.04 and
            close_pos >= 0.65 and
            today['Close'] > ma_max and
            today['MA5'] > yesterday['MA5'] and
            today['Volume'] >= 1000000 and
            today['Volume'] >= (1.5 * today['Vol_20MA'])):
            passed_strats.append("S1_底部突破")

        # S2：創高動能 (Stage 2 + RS強勢 + 收盤位階>=65%)
        if (stage2_trend and rs_strong and
            today['Close'] > today['MA20'] and
            today['Close'] >= (high_1y * 0.85) and
            today['Close'] <= (today['MA200'] * 2.0) and
            today['Volume'] >= (2 * today['Vol_20MA']) and
            today['Close'] > today['Open'] and
            close_pos >= 0.65 and
            today['Volume'] >= 1000000):
            passed_strats.append("S2_創高動能")

        # S3：投信認養 (RS轉強 + 實體紅K收高>=65%)
        if (rs_strong and
            today['Close'] > today['MA20'] and today['MA20'] > yesterday['MA20'] and
            (today['Close'] - today['MA20']) / today['MA20'] <= 0.10 and
            today['Volume'] >= 1000000 and 
            (today['Close'] - today['Open']) / today['Open'] > 0.02 and
            close_pos >= 0.65):
            passed_strats.append("S3_投信認養")

        # S4：強勢回測網球 (Stage 2 龍頭回測 10MA/20MA 彈起)
        high_120d = df['High'].iloc[-120:].max()
        recent_15d_high = df['High'].iloc[-16:-1].max()
        was_leader = recent_15d_high >= (high_120d * 0.95)

        pullback_to_ma = (
            ( yesterday['Low'] <= yesterday['MA10'] * 1.02 or yesterday['Low'] <= yesterday['MA20'] * 1.025 or
              today['Low'] <= today['MA10'] * 1.02 or today['Low'] <= today['MA20'] * 1.025 ) and
            today['Close'] > today['MA20'] and yesterday['Close'] >= yesterday['MA20'] * 0.98
        )
        vol_contracted = yesterday['Volume'] < yesterday['Vol_20MA']
        tennis_bounce = (
            today['Close'] > yesterday['High'] and
            today['Close'] > today['Open'] and
            close_pos >= 0.60 and
            today['Volume'] >= yesterday['Volume'] * 1.15 and
            today['Volume'] >= 800000
        )

        if stage2_trend and rs_strong and was_leader and pullback_to_ma and vol_contracted and tennis_bounce:
            passed_strats.append("S4_強勢回測網球")

        # S5：投信作帳VCP (真VCP：波幅收斂 T2 <= T1*0.75 且 T2 <= 10%)
        t1_df = df.iloc[-31:-16]
        t2_df = df.iloc[-16:-1]
        if not t1_df.empty and not t2_df.empty:
            t1_high, t1_low = t1_df['High'].max(), t1_df['Low'].min()
            t2_high, t2_low = t2_df['High'].max(), t2_df['Low'].min()
            t1_range = (t1_high - t1_low) / t1_low if t1_low > 0 else 1.0
            t2_range = (t2_high - t2_low) / t2_low if t2_low > 0 else 1.0

            true_vcp = (t2_range <= t1_range * 0.75) and (t2_range <= 0.10)
            vdu_pass = (today['Volume'] <= 0.65 * today['Vol_50MA']) or (today['Vol_5MA'] <= 0.70 * today['Vol_50MA'])

            if (stage2_trend and rs_strong and
                today['Close'] >= (high_1y * 0.85) and
                today['Close'] >= today['MA20'] * 0.98 and
                true_vcp and vdu_pass and
                today['Vol_50MA'] >= 800000):
                passed_strats.append("S5_投信作帳VCP")

        # S6：營收口袋樞紐
        past_10_days = df.iloc[-11:-1]
        down_days = past_10_days[past_10_days['Close'] < past_10_days['Open']]
        max_down_vol = down_days['Volume'].max() if not down_days.empty else 0

        stage2_pocket_base = (
            today['Close'] > today['MA150'] > today['MA200'] and
            today['MA50'] > today['MA150'] and
            today['MA200'] > df['MA200'].iloc[-20] and
            today['Close'] >= (high_1y * 0.85) and
            today['Close'] >= 25.0
        )

        if (stage2_pocket_base and rs_strong and
            today['Close'] > today['MA20'] and
            (today['Close'] - today['MA20']) / today['MA20'] <= 0.06 and
            (today['Close'] - yesterday['Close']) / yesterday['Close'] >= 0.03 and
            close_pos >= 0.65 and
            today['Volume'] > max_down_vol and
            today['Volume'] >= 1.5 * today['Vol_50MA'] and
            today['Volume'] >= 1200000):
            passed_strats.append("S6_營收口袋樞紐")

        # S7：漲停強勢旗形
        past_40_days_df = df.iloc[-41:-1]
        low_40 = past_40_days_df['Low'].min()
        high_40 = past_40_days_df['High'].max()
        past_40_returns = df['Close'].pct_change().iloc[-41:-1]
        has_limit_up = (past_40_returns >= 0.095).any()

        past_10_flag = df.iloc[-10:]
        flag_high = past_10_flag['High'].max()
        flag_low = past_10_flag['Low'].min()
        flag_tightness = (flag_high - flag_low) / flag_low if flag_low > 0 else 1.0

        if (stage2_trend and rs_strong and
            low_40 > 0 and high_40 >= low_40 * 1.55 and
            has_limit_up and
            today['Close'] >= high_40 * 0.92 and
            today['Close'] >= today['MA20'] and
            flag_tightness <= 0.12 and
            today['Volume'] <= today['Vol_20MA'] and
            today['Vol_20MA'] >= 1500000):
            passed_strats.append("S7_漲停強勢旗形")

        if not passed_strats: return None

        info = STOCK_INFO.get(ticker, {})
        drop_pct = round((1 - today['Close'] / high_1y) * 100, 2)

        return {
            '代碼': str(info.get('代碼', '')),
            '名稱': str(info.get('名稱', '')),
            '市場': str(info.get('市場', '')),
            '收盤價': round(float(today['Close']), 2),
            '成交量(張)': int(today['Volume'] / 1000),
            '距年高點跌幅': f"{drop_pct}%",
            'RS創高': bool(rs_new_high),
            'strats': passed_strats
        }
    except Exception as e:
        if error_count < 3:
            print(f"\n⚠️ 股票 {ticker} 發生錯誤: {e}")
            error_count += 1
        return None


def check_revenue(stock_dict):
    code = stock_dict['代碼']
    strats = stock_dict['strats'].copy()

    def evaluate_strats(mom, yoy, has_data=True):
        if has_data:
            stock_dict['MoM'] = f"{mom:.1f}%"
            stock_dict['YoY'] = f"{yoy:.1f}%"
        else:
            stock_dict['MoM'] = "-"
            stock_dict['YoY'] = "-"

        new_strats = []
        for s in strats:
            if s in ["S1_底部突破", "S7_漲停強勢旗形"]:
                new_strats.append(s)
            elif s == "S6_營收口袋樞紐":
                if has_data and yoy > 30.0 and mom > 5.0:
                    new_strats.append(s)
            else:
                if has_data and mom > 0 and yoy > 10.0:
                    new_strats.append(s)

        if new_strats:
            stock_dict['strats'] = new_strats
            return stock_dict
        return None

    if code in REVENUE_DATA:
        rev = REVENUE_DATA[code]
        return evaluate_strats(rev['mom'], rev['yoy'], True)

    start_date = (datetime.datetime.now() - datetime.timedelta(days=400)).strftime('%Y-%m-%d')
    url = f"https://api.finmindtrade.com/api/v4/data?dataset=TaiwanStockMonthRevenue&data_id={code}&start_date={start_date}"
    headers = {"User-Agent": "Mozilla/5.0"}
    if FINMIND_TOKEN:
        headers["Authorization"] = f"Bearer {FINMIND_TOKEN}"

    try:
        time.sleep(0.3)
        res = requests.get(url, headers=headers, timeout=5).json()
        if 'data' not in res or len(res['data']) < 13:
            return evaluate_strats(0, 0, False)
        df = pd.DataFrame(res['data'])
        latest_rev = df.iloc[-1]['revenue']
        prev_rev = df.iloc[-2]['revenue']
        latest_month = df.iloc[-1]['revenue_month']
        latest_year = df.iloc[-1]['revenue_year']
        last_year_data = df[(df['revenue_year'] == latest_year - 1) & (df['revenue_month'] == latest_month)]

        if last_year_data.empty:
            return evaluate_strats(0, 0, False)
        prev_yoy = last_year_data.iloc[0]['revenue']
        mom = (latest_rev / prev_rev - 1) * 100 if prev_rev else 0
        yoy = (latest_rev / prev_yoy - 1) * 100 if prev_yoy else 0

        return evaluate_strats(mom, yoy, True)
    except:
        return evaluate_strats(0, 0, False)


def check_chips(stock_dict):
    global DEBUG_CHIPS_COUNT
    code = stock_dict['代碼']
    today_vol_lots = stock_dict.get('成交量(張)', 1)
    strats = stock_dict['strats'].copy()

    chip_strats = ["S1_底部突破", "S3_投信認養", "S5_投信作帳VCP"]
    if not any(s in strats for s in chip_strats):
        return stock_dict

    start_date = (datetime.datetime.now() - datetime.timedelta(days=20)).strftime('%Y-%m-%d')
    url = f"https://api.finmindtrade.com/api/v4/data?dataset=TaiwanStockInstitutionalInvestorsBuySell&data_id={code}&start_date={start_date}"
    headers = {"User-Agent": "Mozilla/5.0"}
    if FINMIND_TOKEN:
        headers["Authorization"] = f"Bearer {FINMIND_TOKEN}"

    def fail_chip():
        rem_strats = [s for s in strats if s not in chip_strats]
        stock_dict['strats'] = rem_strats
        return stock_dict if rem_strats else None

    try:
        time.sleep(0.4)
        res = requests.get(url, headers=headers, timeout=10)
        res_json = res.json()

        if 'msg' in res_json and 'limit' in res_json['msg'].lower():
            if DEBUG_CHIPS_COUNT < 3:
                print(f"\n🔍 [Debug] {code} 額度達到限制，跳過籌碼深入檢驗。")
                DEBUG_CHIPS_COUNT += 1
            return fail_chip()
        if 'data' not in res_json or not res_json['data']:
            return fail_chip()

        df_chip = pd.DataFrame(res_json['data'])
        df_chip['net'] = (pd.to_numeric(df_chip['buy'], errors='coerce').fillna(0) -
                          pd.to_numeric(df_chip['sell'], errors='coerce').fillna(0)) / 1000
        pivot = df_chip.pivot_table(index='date', columns='name', values='net', aggfunc='sum').fillna(0)
        dates = pivot.index.sort_values().tolist()
        if len(dates) < 3:
            return fail_chip()

        t0, t1 = dates[-1], dates[-2]
        recent_3_dates = dates[-3:]

        foreign_cols = [c for c in pivot.columns if '外資' in c or 'Foreign' in c or 'foreign' in c]
        trust_cols = [c for c in pivot.columns if '投信' in c or 'Investment' in c or 'Trust' in c]

        f_t0 = sum(pivot.loc[t0, c] for c in foreign_cols) if foreign_cols else 0
        t_t0 = sum(pivot.loc[t0, c] for c in trust_cols) if trust_cols else 0
        t_t1 = sum(pivot.loc[t1, c] for c in trust_cols) if trust_cols else 0
        t_3d_sum = pivot.loc[recent_3_dates, trust_cols].sum().sum() if trust_cols else 0

        trust_t0_ratio = (t_t0 / today_vol_lots) * 100 if today_vol_lots > 0 else 0
        trust_3d_ratio = (t_3d_sum / today_vol_lots) * 100 if today_vol_lots > 0 else 0

        s1_pass = (f_t0 > 0) or (t_t0 > 0)
        if "S1_底部突破" in strats and not s1_pass:
            strats.remove("S1_底部突破")

        s3_pass = (t_t0 > 0 and t_t1 > 0) and (t_t0 >= 100 or trust_t0_ratio >= 4.0)
        if "S3_投信認養" in strats and not s3_pass:
            strats.remove("S3_投信認養")

        s5_pass = (t_t0 >= 0) and (t_3d_sum >= 200 or trust_3d_ratio >= 8.0)
        if "S5_投信作帳VCP" in strats and not s5_pass:
            strats.remove("S5_投信作帳VCP")

        stock_dict['外資動向'] = f"今:{int(f_t0)}張"
        stock_dict['投信動向'] = f"今:{int(t_t0)}張({trust_t0_ratio:.1f}%)"
        stock_dict['strats'] = strats

        return stock_dict if strats else None
    except Exception as e:
        return fail_chip()


def enrich_with_resonance(final_stocks):
    """跨資料庫加值：RS創高、週轉折與集保大戶增持共振"""
    for stock in final_stocks:
        code = str(stock['代碼'])
        resonance_tags = []

        if stock.get('RS創高'):
            resonance_tags.append("⚡RS創高")

        if code in WEEKLY_TURN_SET:
            resonance_tags.append("👑週轉折共振")

        if code in HOLDERS_DATA:
            h_info = HOLDERS_DATA[code]
            p400_list = h_info.get('p400', [])
            p1000_list = h_info.get('p1000', [])
            if len(p400_list) >= 2:
                diff_400 = round(float(p400_list[-1]) - float(p400_list[-2]), 2)
                diff_1000 = round(float(p1000_list[-1]) - float(p1000_list[-2]), 2) if len(p1000_list) >= 2 else 0.0
                max_diff = max(diff_400, diff_1000)
                if max_diff > 0:
                    resonance_tags.append(f"🐳大戶增持(+{max_diff}%)")
            elif len(p400_list) == 1 and p400_list[0] >= 60.0:
                resonance_tags.append(f"🐳高籌碼集中({p400_list[0]}%)")

        stock['共振標記'] = resonance_tags
        for tag in resonance_tags:
            if tag not in stock['strats']:
                stock['strats'].append(tag)

    return final_stocks


def update_and_save_json(final_stocks):
    """
    更新選股紀錄與歷史績效：
    - 改抓 5 日以克服收盤時間戳與延遲問題
    - 新增 highest_price 與 max_return_pct 追蹤真實波段漲幅
    - 7 日內重複入選改為合併標籤，保護原始買點
    - 加入 default=str 徹底消除 JSON 序列化例外
    """
    print("\n💾 正在更新選股紀錄與歷史績效...")
    os.makedirs("data", exist_ok=True)
    today_dt = datetime.datetime.now()
    today_str = today_dt.strftime("%Y-%m-%d")

    daily_data = {"date": today_str, "picks": final_stocks}
    with open("data/daily_picks.json", "w", encoding="utf-8") as f:
        json.dump(daily_data, f, ensure_ascii=False, indent=4, default=str)

    history_file = "data/history.json"
    history_data = {}
    if os.path.exists(history_file):
        try:
            with open(history_file, "r", encoding="utf-8") as f:
                history_data = json.load(f)
        except Exception as e:
            print(f"⚠️ history.json 讀取重設: {e}")
            history_data = {}

    historical_tickers = list(history_data.keys())
    if historical_tickers:
        print(f"🔄 正在獲取 {len(historical_tickers)} 檔歷史選股的最新報價 (5日穩健窗口)...")
        def fetch_latest_price(ticker):
            try:
                hist = yf.Ticker(ticker).history(period="5d")
                if not hist.empty:
                    latest_bar = hist.iloc[-1]
                    cur_p = round(float(latest_bar['Close']), 2)
                    recent_high = round(float(hist['High'].max()), 2)
                    return ticker, cur_p, recent_high
            except:
                pass
            return ticker, None, None

        with ThreadPoolExecutor(max_workers=5) as executor:
            futures = [executor.submit(fetch_latest_price, t) for t in historical_tickers]
            for future in as_completed(futures):
                t, latest_price, recent_high = future.result()
                if latest_price:
                    for record in history_data[t]:
                        record['current_price'] = latest_price
                        entry_p = float(record['entry_price'])
                        record['return_pct'] = round((latest_price - entry_p) / entry_p * 100, 2)
                        
                        # 追蹤入選以來的最高價與最大潛在報酬 (MFE)
                        prev_highest = float(record.get('highest_price', entry_p))
                        curr_highest = max(prev_highest, latest_price, recent_high or latest_price)
                        record['highest_price'] = round(curr_highest, 2)
                        record['max_return_pct'] = round((curr_highest - entry_p) / entry_p * 100, 2)

    for stock in final_stocks:
        suffix = ".TW" if stock['市場'] == "上市" else ".TWO"
        yf_ticker = f"{stock['代碼']}{suffix}"

        if yf_ticker not in history_data:
            history_data[yf_ticker] = []

        is_recent_duplicate = False
        new_strat_str = ", ".join(stock['strats'])

        for record in history_data[yf_ticker]:
            try:
                pick_dt = datetime.datetime.strptime(record['pick_date'], "%Y-%m-%d")
                days_diff = (today_dt - pick_dt).days
                # 7 日內再度入選，更新其策略標籤，不洗板覆蓋發動價
                if 0 <= days_diff <= 7:
                    existing_strats = [s.strip() for s in record['strategies'].split(',') if s.strip()]
                    merged_strats = list(dict.fromkeys(existing_strats + stock['strats']))
                    record['strategies'] = ", ".join(merged_strats)
                    record['current_price'] = stock['收盤價']
                    record['return_pct'] = round((stock['收盤價'] - float(record['entry_price'])) / float(record['entry_price']) * 100, 2)
                    is_recent_duplicate = True
                    break
            except:
                continue

        if not is_recent_duplicate:
            history_data[yf_ticker].append({
                "name": stock['名稱'],
                "pick_date": today_str,
                "strategies": new_strat_str,
                "entry_price": stock['收盤價'],
                "current_price": stock['收盤價'],
                "highest_price": stock['收盤價'],
                "return_pct": 0.0,
                "max_return_pct": 0.0
            })

    with open(history_file, "w", encoding="utf-8") as f:
        json.dump(history_data, f, ensure_ascii=False, indent=4, default=str)
        
    print("✅ JSON 資料庫更新完成！")


# ================= 主程式執行 =================
if __name__ == "__main__":
# --- 新增：資料新鮮度探測 ---
    print("\n📡 正在檢查 Yahoo Finance 資料庫新鮮度...")
    test_df = yf.Ticker("2330.TW").history(period="5d")
    if not test_df.empty:
        latest_date = test_df.index[-1].strftime("%Y-%m-%d")
        print(f"📌 目前 YF 伺服器最新的台股 K 線日期為: {latest_date}")
        today_str = datetime.datetime.now().strftime("%Y-%m-%d")
        if latest_date != today_str:
            print(f"⚠️ 警告：今天已經是 {today_str}，但 YF 資料還停留在 {latest_date}！選股結果可能為昨日舊資料。")
    # --------------------------

    check_market_regime()
    load_auxiliary_databases()
    fetch_all_revenue()

    print("\n⏳ 步驟 1: 獲取全市場股票清單...")
    all_tickers = get_tw_stocks()
    print(f"✅ 共獲取 {len(all_tickers)} 檔股票。")

    print("\n🚀 步驟 2: 啟動多執行緒掃描技術面 (S1 ~ S7 大師進化版)...")
    passed_technical = []

    with ThreadPoolExecutor(max_workers=5) as executor:
        futures = {executor.submit(check_technical, ticker): ticker for ticker in all_tickers}
        for future in tqdm(as_completed(futures), total=len(all_tickers), desc="技術面掃描"):
            result = future.result()
            if result: passed_technical.append(result)

    print(f"\n🔍 【階段一：技術面初篩完成】共 {len(passed_technical)} 檔")
    print("-" * 50)

    print(f"\n📊 步驟 3: 針對 {len(passed_technical)} 檔初篩名單進行【月營收】驗證...")
    passed_revenue = []
    for stock in tqdm(passed_technical, desc="營收面掃描"):
        rev_result = check_revenue(stock)
        if rev_result: passed_revenue.append(rev_result)

    print(f"\n🔍 【階段二：營收驗證完成】進入籌碼審查前，共 {len(passed_revenue)} 檔")
    print("-" * 50)

    print(f"\n🏦 步驟 4: 針對 {len(passed_revenue)} 檔名單進行【法人籌碼濃度】驗證...")
    final_stocks = []
    for stock in tqdm(passed_revenue, desc="籌碼面掃描"):
        chip_result = check_chips(stock)
        if chip_result: final_stocks.append(chip_result)

    # 步驟 5: 進行週轉折與大戶籌碼共振加冕
    final_stocks = enrich_with_resonance(final_stocks)

    # ================= 整理結果與發送 =================
    print("\n========== 🎯 七大策略最終篩選結果 ==========")
    
    update_and_save_json(final_stocks)

    results_by_strat = {
        "S1_底部突破": [], 
        "S2_創高動能": [], 
        "S3_投信認養": [], 
        "S4_強勢回測網球": [],
        "S5_投信作帳VCP": [],
        "S6_營收口袋樞紐": [],
        "S7_漲停強勢旗形": []
    }
    for stock in final_stocks:
        for s in stock['strats']:
            if s in results_by_strat:
                results_by_strat[s].append(stock)

    today_str = datetime.datetime.now().strftime("%Y-%m-%d")
    notify_msg = f"## 📊 【{today_str} 台股七核心選股報告】\n*整合 RS相對強度、真VCP、網球回測與大戶週轉折共振*\n"

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
            orig_stock = next((x for x in stocks if x['代碼'] == row['代碼']), {})
            res_tags = " ".join([f"`{t}`" for t in orig_stock.get('共振標記', [])])
            title_line = f"**📌 {row['代碼']} {row['名稱']} ({row['市場']})** {res_tags}".strip()
            
            notify_msg += f"{title_line}\n"
            notify_msg += f"> 收盤: `{row['收盤價']}` ｜ 量: `{row['成交量(張)']}張` ｜ 距高: `{row['距年高點跌幅']}`\n"
            if 'MoM' in row and row['MoM'] != '-':
                notify_msg += f"> 營收: MoM `{row['MoM']}` ｜ YoY `{row['YoY']}`\n"
            if '外資動向' in row and row['外資動向'] != '-':
                notify_msg += f"> 籌碼: 外資 `{row['外資動向']}` ｜ 投信 `{row['投信動向']}`\n"
        notify_msg += "───────────────\n"

    send_discord_webhook(DISCORD_WEBHOOK_URL, notify_msg)
    print("\n✅ 選股作業結束，JSON 資料庫已更新，通知已發送至 Discord！")
