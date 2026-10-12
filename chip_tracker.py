import os
import json
import time
import requests
import pandas as pd
from datetime import datetime

# ================= 參數設定 =================
# 讀取環境變數中的 Token，並去除可能的空白字元
FINMIND_TOKEN = os.environ.get("FINMIND_TOKEN", "").strip()
DATA_DIR = "data"
HOLDERS_FILE = f"{DATA_DIR}/holders.json"
SOURCE_FILES = ['daily_picks.json', 'history.json', 'weekly_uptrend.json', 'weekly_turn.json']

def get_target_list():
    """提取所有正在追蹤的股票代碼"""
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
    """下載集保戶股權分散表 Open Data (每週官方更新)"""
    print("📥 正在下載集保結算所最新 Open Data...")
    url = "https://opendata.tdcc.com.tw/getOD.ashx?id=1-5"
    df = pd.read_csv(url)
    df.columns = [col.strip() for col in df.columns]
    
    latest_date_raw = str(df['資料日期'].max())
    latest_date = f"{latest_date_raw[:4]}-{latest_date_raw[4:6]}-{latest_date_raw[6:]}"
    print(f"✅ 集保最新資料日期: {latest_date}")
    return df, latest_date

def fetch_finmind_history(code):
    """冷啟動：透過官方 API 抓取 2025 年以來的歷史資料"""
    url = f"https://api.finmindtrade.com/api/v4/data?dataset=TaiwanStockHoldingSharesPer&data_id={code}&start_date=2025-01-01"
    
    # 【關鍵修正】使用官方建議的 Bearer Header 來傳遞 Token
    headers = {"User-Agent": "Mozilla/5.0"}
    if FINMIND_TOKEN:
        headers["Authorization"] = f"Bearer {FINMIND_TOKEN}"
    
    try:
        time.sleep(0.6) # 禮貌性延遲，避免瞬間併發過高
        res = requests.get(url, headers=headers, timeout=15)
        res_json = res.json()
        
        # 除錯訊息：檢查是否有權限或額度問題
        if res.status_code != 200 or res_json.get('status') != 200:
            msg = res_json.get('msg', '未知錯誤')
            print(f"⚠️ {code} API 拒絕連線: {msg}")
            return None
            
        if 'data' not in res_json or not res_json['data']:
            print(f"⚠️ {code} 查無歷史資料")
            return None
        
        df = pd.DataFrame(res_json['data'])
        df['HoldingSharesLevel'] = df['HoldingSharesLevel'].astype(int)
        df['percent'] = df['percent'].astype(float)
        
        # 400張以上 = 級距 13 ~ 16
        df_400 = df[df['HoldingSharesLevel'] >= 13]
        # 1000張以上 = 級距 16
        df_1000 = df[df['HoldingSharesLevel'] == 16]
        
        p400_series = df_400.groupby('date')['percent'].sum().round(2)
        p1000_series = df_1000.groupby('date')['percent'].sum().round(2)
        
        merged = pd.DataFrame({'p400': p400_series, 'p1000': p1000_series}).fillna(0).reset_index()
        merged = merged.sort_values('date')
        
        print(f"✅ {code} 成功取得 {len(merged)} 筆歷史資料")
        return {
            'dates': merged['date'].tolist(),
            'p400': merged['p400'].tolist(),
            'p1000': merged['p1000'].tolist()
        }
    except Exception as e:
        print(f"⚠️ {code} 網路請求失敗: {e}")
        return None

def update_holders_database():
    print("=======================================")
    if FINMIND_TOKEN:
        print(f"🔑 已成功讀取到 Token (長度: {len(FINMIND_TOKEN)} 字元)")
    else:
        print("⚠️ 未讀取到 FINMIND_TOKEN！將使用免登入額度 (極易失敗)")
    print("=======================================\n")

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
        print(f"❌ 集保官方資料下載失敗: {e}")
        return

    # 批次處理
    for code in target_list:
        code_str = str(code)
        
        # --- 步驟 1：冷啟動歷史資料 ---
        if code_str not in holders_data or not holders_data[code_str].get('dates'):
            history = fetch_finmind_history(code_str)
            if history:
                holders_data[code_str] = history
            else:
                holders_data[code_str] = {'dates': [], 'p400': [], 'p1000': []}
            
        # --- 步驟 2：每週官方集保增量更新 (最穩定) ---
        if len(holders_data[code_str]['dates']) == 0 or holders_data[code_str]['dates'][-1] != latest_date:
            stock_tdcc = tdcc_df[tdcc_df['證券代號'] == code_str]
            if not stock_tdcc.empty:
                p400_sum = stock_tdcc[stock_tdcc['持股分級'].isin([13, 14, 15, 16])]['占集保庫存數比例%'].sum()
                p1000_sum = stock_tdcc[stock_tdcc['持股分級'] == 16]['占集保庫存數比例%'].sum()
                
                holders_data[code_str]['dates'].append(latest_date)
                holders_data[code_str]['p400'].append(round(float(p400_sum), 2))
                holders_data[code_str]['p1000'].append(round(float(p1000_sum), 2))
                print(f"➕ {code_str} 增量更新完成：{latest_date} (400張: {round(float(p400_sum), 2)}%)")

    with open(HOLDERS_FILE, 'w', encoding='utf-8') as f:
        json.dump(holders_data, f, ensure_ascii=False, indent=4)
    print("\n✅ 大戶籌碼資料庫更新完畢！")

if __name__ == "__main__":
    update_holders_database()
