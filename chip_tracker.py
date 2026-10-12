import os
import json
import pandas as pd
from datetime import datetime

# ================= 參數設定 =================
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
                        code = key.replace('.TWO', '').replace('.TW', '')
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
    """下載集保戶股權分散表 Open Data (完全免費、無限制)"""
    print("📥 正在從政府集保中心下載本週最新大戶籌碼...")
    url = "https://opendata.tdcc.com.tw/getOD.ashx?id=1-5"
    df = pd.read_csv(url)
    df.columns = [col.strip() for col in df.columns]
    
    # 【關鍵修復】強制將證券代號轉為字串並去除空白，避免 Pandas 把 1101 當成整數
    df['證券代號'] = df['證券代號'].astype(str).str.strip()
    
    latest_date_raw = str(df['資料日期'].max())
    latest_date = f"{latest_date_raw[:4]}-{latest_date_raw[4:6]}-{latest_date_raw[6:]}"
    print(f"✅ 集保最新資料日期: {latest_date}")
    return df, latest_date

def update_holders_database():
    os.makedirs(DATA_DIR, exist_ok=True)
    
    holders_data = {}
    if os.path.exists(HOLDERS_FILE):
        with open(HOLDERS_FILE, 'r', encoding='utf-8') as f:
            holders_data = json.load(f)
            
    target_list = get_target_list()
    print(f"🎯 戰情室目前共追蹤 {len(target_list)} 檔標的")
    
    try:
        tdcc_df, latest_date = fetch_tdcc_open_data()
    except Exception as e:
        print(f"❌ 官方集保資料下載失敗: {e}")
        return

    # 批次從這包資料中，萃取出我們關注的股票
    for code in target_list:
        code_str = str(code)
        
        # 如果資料庫沒有這檔股票，初始化空清單
        if code_str not in holders_data:
            holders_data[code_str] = {'dates': [], 'p400': [], 'p1000': []}
            
        # 檢查該最新日期是否已經寫入過了，避免重複寫入
        if len(holders_data[code_str]['dates']) == 0 or holders_data[code_str]['dates'][-1] != latest_date:
            stock_tdcc = tdcc_df[tdcc_df['證券代號'] == code_str]
            if not stock_tdcc.empty:
                # 400張以上 = 集保級距 13(400-600), 14(600-800), 15(800-1000), 16(>1000)
                p400_sum = stock_tdcc[stock_tdcc['持股分級'].isin([13, 14, 15, 16])]['占集保庫存數比例%'].sum()
                # 1000張以上 = 集保級距 16
                p1000_sum = stock_tdcc[stock_tdcc['持股分級'] == 16]['占集保庫存數比例%'].sum()
                
                holders_data[code_str]['dates'].append(latest_date)
                holders_data[code_str]['p400'].append(round(float(p400_sum), 2))
                holders_data[code_str]['p1000'].append(round(float(p1000_sum), 2))
                print(f"➕ {code_str} 增量更新完成 ➔ 400張大戶: {round(float(p400_sum), 2)}%")
            else:
                # 若少數 ETF 或剛上市股票查無籌碼資料則略過
                pass

    # 寫入 JSON 儲存
    with open(HOLDERS_FILE, 'w', encoding='utf-8') as f:
        json.dump(holders_data, f, ensure_ascii=False, indent=4)
    print("\n✅ 專屬大戶籌碼庫更新完畢！")

if __name__ == "__main__":
    update_holders_database()
