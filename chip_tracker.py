import os
import json
import time
import requests
import pandas as pd
from datetime import datetime

# ================= 參數設定 =================
DATA_DIR = "data"
HOLDERS_FILE = f"{DATA_DIR}/holders.json"
# 找出需要追蹤的目標清單來源
SOURCE_FILES = ['daily_picks.json', 'history.json', 'weekly_uptrend.json', 'weekly_turn.json']

def get_target_list():
    """從戰情室的各個 JSON 資料庫中，提取所有正在追蹤的股票代碼"""
    target_codes = set()
    for filename in SOURCE_FILES:
        filepath = os.path.join(DATA_DIR, filename)
        if not os.path.exists(filepath): continue
        
        try:
            with open(filepath, 'r', encoding='utf-8') as f:
                data = json.load(f)
                if filename == 'history.json':
                    for key in data.keys():
                        code = key.replace('.TW', '').replace('.TWO', '')
                        target_codes.add(code)
                elif filename == 'daily_picks.json':
                    for pick in data.get('picks', []):
                        target_codes.add(str(pick.get('代碼', '')))
                else: # weekly_uptrend.json, weekly_turn.json
                    for stock in data.get('stocks', []):
                        target_codes.add(str(stock.get('代碼', '')))
        except Exception as e:
            print(f"讀取 {filename} 失敗: {e}")
            
    # 過濾空值
    return sorted(list(filter(None, target_codes)))

def fetch_tdcc_open_data():
    """下載集保戶股權分散表 Open Data (每週更新)"""
    print("📥 正在下載集保結算所最新 Open Data...")
    url = "https://opendata.tdcc.com.tw/getOD.ashx?id=1-5"
    df = pd.read_csv(url)
    
    # 確保欄位名稱正確
    df.columns = [col.strip() for col in df.columns]
    
    # 取得最新資料日期
    latest_date_raw = str(df['資料日期'].max())
    latest_date = f"{latest_date_raw[:4]}-{latest_date_raw[4:6]}-{latest_date_raw[6:]}"
    print(f"✅ 集保最新資料日期: {latest_date}")
    
    return df, latest_date

def scrape_mystic_pyramid(code):
    """冷啟動爬蟲：從神秘金字塔抓取 2025 年以來的歷史資料"""
    print(f"🕷️ 觸發冷啟動爬蟲：抓取 {code} 神秘金字塔歷史資料...")
    url = f"https://norway.twsthr.info/StockHolders.aspx?stock={code}"
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
    
    try:
        res = requests.get(url, headers=headers, timeout=10)
        dfs = pd.read_html(res.text)
        
        # 尋找包含「400張」的表格
        target_df = None
        for df in dfs:
            # 將多重索引攤平或轉為字串搜尋
            cols_str = " ".join([str(c) for c in df.columns])
            if "400" in cols_str and "1000" in cols_str:
                target_df = df
                break
                
        if target_df is None: return None

        # 整理欄位
        target_df.columns = ['_'.join(str(i) for i in col) if isinstance(col, tuple) else str(col) for col in target_df.columns]
        
        # 抓出日期、大於400張比例、大於1000張比例
        date_col = [c for c in target_df.columns if '日期' in c][0]
        p400_col = [c for c in target_df.columns if '400' in c and ('比例' in c or '%' in c)][0]
        p1000_col = [c for c in target_df.columns if '1000' in c and ('比例' in c or '%' in c)][0]
        
        clean_df = target_df[[date_col, p400_col, p1000_col]].copy()
        clean_df.columns = ['date', 'p400', 'p1000']
        
        # 清洗與過濾 >= 2025-01-01
        clean_df['date'] = clean_df['date'].astype(str).str.replace('/', '')
        clean_df['date'] = pd.to_datetime(clean_df['date'], format='%Y%m%d', errors='coerce').dt.strftime('%Y-%m-%d')
        clean_df = clean_df.dropna()
        clean_df = clean_df[clean_df['date'] >= '2025-01-01']
        clean_df = clean_df.sort_values('date')
        
        return {
            'dates': clean_df['date'].tolist(),
            'p400': clean_df['p400'].astype(float).tolist(),
            'p1000': clean_df['p1000'].astype(float).tolist()
        }
    except Exception as e:
        print(f"⚠️ {code} 爬蟲失敗: {e}")
        return None

def update_holders_database():
    os.makedirs(DATA_DIR, exist_ok=True)
    
    # 載入現有資料庫
    holders_data = {}
    if os.path.exists(HOLDERS_FILE):
        with open(HOLDERS_FILE, 'r', encoding='utf-8') as f:
            holders_data = json.load(f)
            
    target_list = get_target_list()
    print(f"🎯 目標追蹤清單共 {len(target_list)} 檔股票")
    
    # 下載本週集保增量資料
    try:
        tdcc_df, latest_date = fetch_tdcc_open_data()
    except Exception as e:
        print(f"❌ 集保資料下載失敗: {e}")
        return

    # 批次處理每檔股票
    for code in target_list:
        code_str = str(code)
        
        # --- 步驟 1：檢查是否需要冷啟動 ---
        if code_str not in holders_data:
            history = scrape_mystic_pyramid(code_str)
            if history:
                holders_data[code_str] = history
            else:
                holders_data[code_str] = {'dates': [], 'p400': [], 'p1000': []}
            time.sleep(1) # 禮貌性延遲避免被鎖 IP
            
        # --- 步驟 2：集保 Open Data 增量更新 ---
        # 檢查該最新日期是否已寫入
        if len(holders_data[code_str]['dates']) == 0 or holders_data[code_str]['dates'][-1] != latest_date:
            # 從集保 DF 中抓出該股票的資料
            # 持股分級：13(400-600張), 14(600-800張), 15(800-1000張), 16(1000張以上)
            stock_tdcc = tdcc_df[tdcc_df['證券代號'] == code_str]
            if not stock_tdcc.empty:
                # 計算 > 400 張比例 (級距 13 ~ 16 的加總)
                p400_sum = stock_tdcc[stock_tdcc['持股分級'].isin([13, 14, 15, 16])]['占集保庫存數比例%'].sum()
                # 計算 > 1000 張比例 (級距 16)
                p1000_sum = stock_tdcc[stock_tdcc['持股分級'] == 16]['占集保庫存數比例%'].sum()
                
                holders_data[code_str]['dates'].append(latest_date)
                holders_data[code_str]['p400'].append(round(float(p400_sum), 2))
                holders_data[code_str]['p1000'].append(round(float(p1000_sum), 2))
                print(f"➕ {code_str} 增量更新完成：{latest_date}")

    # 儲存資料庫
    with open(HOLDERS_FILE, 'w', encoding='utf-8') as f:
        json.dump(holders_data, f, ensure_ascii=False, indent=4)
    print("✅ 大戶籌碼資料庫更新完畢！")

if __name__ == "__main__":
    update_holders_database()
