import os
import json
import re
import requests
import pandas as pd
from datetime import datetime

# ================= 參數設定 =================
DATA_DIR = "data"
HOLDERS_FILE = f"{DATA_DIR}/holders.json"

def get_all_tw_stocks():
    """直接從台灣證交所/櫃買中心抓取所有上市櫃股票代碼"""
    print("🔍 正在獲取全市場上市櫃股票代碼...")
    headers = {"User-Agent": "Mozilla/5.0"}
    target_codes = set()
    markets = {'2': '上市', '4': '上櫃'}
    
    for mode in markets.keys():
        try:
            res = requests.get(f"https://isin.twse.com.tw/isin/C_public.jsp?strMode={mode}", headers=headers, timeout=10)
            res.encoding = 'big5'
            # 利用正規表達式，只抓取長度為 4 碼的正常股票 (排除權證、牛熊證等)
            matches = re.findall(r'>([1-9][0-9]{3})\u3000([^<]+)</td>', res.text)
            for code, name in matches:
                target_codes.add(code)
        except Exception as e:
            print(f"抓取股票代碼失敗: {e}")
            
    return sorted(list(target_codes))

def fetch_tdcc_open_data():
    """下載集保戶股權分散表 Open Data (完全免費、無限制)"""
    print("📥 正在從政府集保中心下載本週最新大戶籌碼...")
    url = "https://opendata.tdcc.com.tw/getOD.ashx?id=1-5"
    df = pd.read_csv(url)
    df.columns = [col.strip() for col in df.columns]
    
    # 強制將證券代號轉為字串並去除空白
    df['證券代號'] = df['證券代號'].astype(str).str.strip()
    
    latest_date_raw = str(df['資料日期'].max())
    latest_date = f"{latest_date_raw[:4]}-{latest_date_raw[4:6]}-{latest_date_raw[6:]}"
    print(f"✅ 集保最新資料日期: {latest_date}")
    return df, latest_date

def update_holders_database():
    os.makedirs(DATA_DIR, exist_ok=True)
    
    holders_data = {}
    if os.path.exists(HOLDERS_FILE):
        try:
            with open(HOLDERS_FILE, 'r', encoding='utf-8') as f:
                holders_data = json.load(f)
        except json.JSONDecodeError:
            print(f"⚠️ 發現空白或損毀的 {HOLDERS_FILE}，將重新初始化資料庫。")
            holders_data = {}
            
    # 改為獲取全市場名單
    target_list = get_all_tw_stocks()
    print(f"🎯 本次更新將涵蓋全市場共 {len(target_list)} 檔標的")
    
    try:
        tdcc_df, latest_date = fetch_tdcc_open_data()
    except Exception as e:
        print(f"❌ 官方集保資料下載失敗: {e}")
        return

    # 批次處理全市場股票
    for code_str in target_list:
        
        # 如果資料庫沒有這檔股票，初始化空清單
        if code_str not in holders_data:
            holders_data[code_str] = {'dates': [], 'p400': [], 'p1000': []}
            
        # 檢查該最新日期是否已經寫入過了，避免重複寫入
        if len(holders_data[code_str]['dates']) == 0 or holders_data[code_str]['dates'][-1] != latest_date:
            stock_tdcc = tdcc_df[tdcc_df['證券代號'] == code_str]
            if not stock_tdcc.empty:
                # 【修正】：400張以上 = 集保級距 12, 13, 14, 15 的加總
                p400_sum = stock_tdcc[stock_tdcc['持股分級'].isin([12, 13, 14, 15])]['占集保庫存數比例%'].sum()
                
                # 【修正】：1000張以上 = 集保級距 15
                p1000_sum = stock_tdcc[stock_tdcc['持股分級'] == 15]['占集保庫存數比例%'].sum()
                
                holders_data[code_str]['dates'].append(latest_date)
                holders_data[code_str]['p400'].append(round(float(p400_sum), 2))
                holders_data[code_str]['p1000'].append(round(float(p1000_sum), 2))

    print("💾 正在將全市場資料寫入 JSON 檔案 (資料量較大，請稍候)...")
    with open(HOLDERS_FILE, 'w', encoding='utf-8') as f:
        # 將全市場 1700 檔資料寫入 (檔案約 2~3 MB)
        json.dump(holders_data, f, ensure_ascii=False, indent=4)
    print("\n✅ 全市場大戶籌碼庫更新完畢！")

if __name__ == "__main__":
    update_holders_database()
