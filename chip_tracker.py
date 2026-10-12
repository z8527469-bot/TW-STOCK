import os
import json
import time
import requests
import pandas as pd
from datetime import datetime

# ================= 參數設定 =================
# 從環境變數讀取 FinMind Token，突破免費 API 呼叫限制
FINMIND_TOKEN = os.environ.get("FINMIND_TOKEN", "") 
DATA_DIR = "data"
HOLDERS_FILE = f"{DATA_DIR}/holders.json"
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
                else: 
                    for stock in data.get('stocks', []):
                        target_codes.add(str(stock.get('代碼', '')))
        except Exception as e:
            print(f"讀取 {filename} 失敗: {e}")
            
    return sorted(list(filter(None, target_codes)))

def fetch_tdcc_open_data():
    """下載集保戶股權分散表 Open Data (每週更新)"""
    print("📥 正在下載集保結算所最新 Open Data...")
    url = "https://opendata.tdcc.com.tw/getOD.ashx?id=1-5"
    df = pd.read_csv(url)
    
    df.columns = [col.strip() for col in df.columns]
    
    latest_date_raw = str(df['資料日期'].max())
    latest_date = f"{latest_date_raw[:4]}-{latest_date_raw[4:6]}-{latest_date_raw[6:]}"
    print(f"✅ 集保最新資料日期: {latest_date}")
    
    return df, latest_date

def fetch_finmind_history(code):
    """冷啟動：透過官方 API 抓取 2025 年以來的歷史資料 (取代易被擋的網頁爬蟲)"""
    print(f"🔄 觸發冷啟動：透過 FinMind 獲取 {code} 歷史大戶籌碼...")
    url = f"https://api.finmindtrade.com/api/v4/data?dataset=TaiwanStockHoldingSharesPer&data_id={code}&start_date=2025-01-01"
    if FINMIND_TOKEN:
        url += f"&token={FINMIND_TOKEN}"
    
    try:
        time.sleep(0.5) # 禮貌性延遲
        res = requests.get(url, timeout=15).json()
        
        if 'msg' in res and 'limit' in res['msg'].lower():
            print(f"⚠️ {code} 失敗: FinMind API 額度已滿，請確認 Token 設定。")
            return None
        if 'data' not in res or not res['data']:
            print(f"⚠️ {code} 無歷史資料")
            return None
        
        df = pd.DataFrame(res['data'])
        df['HoldingSharesLevel'] = df['HoldingSharesLevel'].astype(int)
        df['percent'] = df['percent'].astype(float)
        
        # 集保分級：13(400-600), 14(600-800), 15(800-1000), 16(>1000)
        # 400張以上 = 級距 13 ~ 16 的總和
        df_400 = df[df['HoldingSharesLevel'] >= 13]
        # 1000張以上 = 級距 16
        df_1000 = df[df['HoldingSharesLevel'] == 16]
        
        p400_series = df_400.groupby('date')['percent'].sum().round(2)
        p1000_series = df_1000.groupby('date')['percent'].sum().round(2)
        
        merged = pd.DataFrame({'p400': p400_series, 'p1000': p1000_series}).fillna(0).reset_index()
        merged = merged.sort_values('date')
        
        return {
            'dates': merged['date'].tolist(),
            'p400': merged['p400'].tolist(),
            'p1000': merged['p1000'].tolist()
        }
    except Exception as e:
        print(f"⚠️ {code} FinMind 獲取失敗: {e}")
        return None

def update_holders_database():
    os.makedirs(DATA_DIR, exist_ok=True)
    
    holders_data = {}
    if os.path.exists(HOLDERS_FILE):
        with open(HOLDERS_FILE, 'r', encoding='utf-8') as f:
            holders_data = json.load(f)
            
    target_list = get_target_list()
    print(f"🎯 目標追蹤清單共 {len(target_list)} 檔股票")
    
    try:
        tdcc_df, latest_date = fetch_tdcc_open_data()
    except Exception as e:
        print(f"❌ 集保資料下載失敗: {e}")
        return

    # 批次處理每檔股票
    for code in target_list:
        code_str = str(code)
        
        # --- 步驟 1：檢查是否需要冷啟動 ---
        if code_str not in holders_data or not holders_data[code_str].get('dates'):
            history = fetch_finmind_history(code_str)
            if history:
                holders_data[code_str] = history
            else:
                holders_data[code_str] = {'dates': [], 'p400': [], 'p1000': []}
            
        # --- 步驟 2：集保 Open Data 增量更新 ---
        if len(holders_data[code_str]['dates']) == 0 or holders_data[code_str]['dates'][-1] != latest_date:
            stock_tdcc = tdcc_df[tdcc_df['證券代號'] == code_str]
            if not stock_tdcc.empty:
                p400_sum = stock_tdcc[stock_tdcc['持股分級'].isin([13, 14, 15, 16])]['占集保庫存數比例%'].sum()
                p1000_sum = stock_tdcc[stock_tdcc['持股分級'] == 16]['占集保庫存數比例%'].sum()
                
                holders_data[code_str]['dates'].append(latest_date)
                holders_data[code_str]['p400'].append(round(float(p400_sum), 2))
                holders_data[code_str]['p1000'].append(round(float(p1000_sum), 2))
                print(f"➕ {code_str} 增量更新完成：{latest_date}")

    with open(HOLDERS_FILE, 'w', encoding='utf-8') as f:
        json.dump(holders_data, f, ensure_ascii=False, indent=4)
    print("✅ 大戶籌碼資料庫更新完畢！")

if __name__ == "__main__":
    update_holders_database()
