import os
import json
import logging
import requests
import re
import time
from datetime import datetime
from bs4 import BeautifulSoup
import firebase_admin
from firebase_admin import credentials, firestore
from google.cloud.firestore_v1.base_query import FieldFilter
import google.generativeai as genai
import urllib.parse

# ==========================================
# 0. 系統日誌設定
# ==========================================
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# ==========================================
# 1. 環境變數載入
# ==========================================
FIREBASE_CREDENTIALS = os.environ.get("FIREBASE_CREDENTIALS")
FIREBASE_UID = os.environ.get("FIREBASE_UID")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")
APP_ID = "land-dev-app"

if not all([FIREBASE_CREDENTIALS, FIREBASE_UID, GEMINI_API_KEY]):
    logger.error("❌ 嚴重錯誤：找不到必要的環境變數")
    exit(1)

# ==========================================
# 2. 初始化服務
# ==========================================
try:
    cred_dict = json.loads(FIREBASE_CREDENTIALS)
    cred = credentials.Certificate(cred_dict)
    if not firebase_admin._apps:
        firebase_admin.initialize_app(cred)
    db = firestore.client()
    logger.info("✅ Firebase 初始化成功")
except Exception as e:
    logger.error(f"❌ Firebase 初始化失敗: {e}")
    exit(1)

try:
    genai.configure(api_key=GEMINI_API_KEY)
    model = genai.GenerativeModel('gemini-2.5-flash')
    logger.info("✅ Gemini AI 初始化成功")
except Exception as e:
    logger.error(f"❌ Gemini AI 初始化失敗: {e}")
    exit(1)

# ==========================================
# 3. 爬蟲輔助函式
# ==========================================

def fetch_content(url):
    """抓取內容：若是 RSS 則解析清單，若是網頁則抓純文字"""
    try:
        headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'}
        if not url.startswith('http'): url = 'https://' + url
        res = requests.get(url, headers=headers, timeout=20)
        res.raise_for_status()
        
        content_type = res.headers.get('Content-Type', '').lower()
        if 'xml' in content_type or url.endswith('.xml') or url.endswith('.rss') or '<rss' in res.text[:200]:
            soup = BeautifulSoup(res.content, 'xml')
            items = soup.find_all(['item', 'entry'])[:10]
            rss_text = ""
            for item in items:
                title = item.find(['title']).text if item.find(['title']) else ""
                rss_text += f"- [RSS項目] {title}\n"
            return rss_text
        else:
            soup = BeautifulSoup(res.content, 'html.parser')
            for script in soup(["script", "style"]): script.extract()
            text = soup.get_text(separator='\n')
            lines = (line.strip() for line in text.splitlines())
            return '\n'.join(chunk for chunk in lines if chunk)[:3000] # 限制單一網頁字數
    except Exception as e:
        logger.warning(f"   ⚠️ 無法抓取 {url}: {e}")
        return ""

def fetch_google_news_text(query):
    """Google 新聞搜尋內容彙整"""
    try:
        encoded_query = urllib.parse.quote(query)
        url = f"https://news.google.com/rss/search?q={encoded_query}&hl=zh-TW&gl=TW&ceid=TW:zh-Hant"
        res = requests.get(url, timeout=15)
        res.raise_for_status()
        soup = BeautifulSoup(res.content, 'xml')
        items = soup.find_all('item')[:5]
        news_summary = ""
        for item in items:
            title = item.title.text if item.title else ""
            news_summary += f"- [新聞搜尋] {title}\n"
        return news_summary, (items[0].link.text if items else url)
    except:
        return "", ""

def call_gemini_with_retry(prompt, max_retries=5):
    """處理 AI 限速與重試"""
    for attempt in range(max_retries):
        try:
            time.sleep(2)
            response = model.generate_content(prompt)
            return response.text.strip()
        except Exception as e:
            error_msg = str(e)
            if "429" in error_msg or "quota" in error_msg.lower():
                match = re.search(r'retry in (\d+\.?\d*)s', error_msg)
                wait_time = max(60, (float(match.group(1)) + 15) if match else 60)
                logger.warning(f"   ⏳ API 限速中，暫停 {wait_time:.1f} 秒後重試...")
                time.sleep(wait_time)
            else:
                return None
    return None

# ==========================================
# 4. 主程式邏輯 (合併來源判讀)
# ==========================================
def main():
    logger.info("🚀 啟動合併來源判讀加速版機器人...")
    
    try:
        user_ref = db.collection('artifacts').document(APP_ID).collection('users').document(FIREBASE_UID)
        projects = list(user_ref.collection('projects').where(filter=FieldFilter('isArchived', '==', False)).stream())
    except Exception as e:
        logger.error(f"❌ 讀取資料庫失敗: {e}")
        return

    now = datetime.now()
    roc_date_str = f"{now.year - 1911}.{now.strftime('%m.%d')}"

    for doc_snap in projects:
        try:
            p_data = doc_snap.to_dict()
            p_id = doc_snap.id
            name = p_data.get('name', '未知案件')
            keywords = p_data.get('keywords', '').strip()
            sources = p_data.get('sources', [])
            
            logger.info(f"\n🔍 查核案件：【{name}】")
            
            # 1. 蒐集所有來源資訊 (不立刻問 AI)
            all_raw_data = []
            
            # A. 抓取特定來源
            for s in sources:
                url = s.get('url', '').strip()
                s_name = s.get('name', '官方來源')
                if not url: continue
                logger.info(f"   -> 蒐集來源：{s_name}")
                content = fetch_content(url)
                if content:
                    all_raw_data.append(f"【來源名稱：{s_name}】\n{content}")

            # B. 抓取 Google 新聞作為備援資訊
            search_query = keywords if keywords else f"{p_data.get('city', '')} {name}"
            logger.info(f"   -> 蒐集 Google 新聞搜尋結果")
            news_text, first_link = fetch_google_news_text(search_query)
            if news_text:
                all_raw_data.append(f"【Google 新聞搜尋結果】\n{news_text}")

            # 2. 合併資訊並呼叫 AI (僅呼叫一次)
            if not all_raw_data:
                logger.info("   平靜無波 (無任何資料可供查核)。")
                continue

            combined_info = "\n\n---\n\n".join(all_raw_data)
            
            prompt = (
                f"你是一個土地開發專業分析師。\n"
                f"請分析以下彙整的「多重來源資訊」，判斷目標專案【{name}】最近是否有「實質性」的最新進度、會議、或公告？\n"
                f"關鍵字參考：【{keywords}】\n\n"
                f"判讀規則：\n"
                f"1. 若有新進度，請合併各方資訊，用一句話總結重點(30字內)。\n"
                f"2. 若資料中全是舊聞、無關訊息、或找不到該案，請務必只回答「無更新」。\n"
                f"3. 優先採納具有明確日期的政府公告。\n\n"
                f"=== 彙整資訊內容 ===\n"
                f"{combined_info}"
            )

            logger.info("   ⏳ 正在進行合併判讀 (呼叫 AI)...")
            ans = call_gemini_with_retry(prompt)

            if ans and "無更新" not in ans and len(ans) > 2:
                logger.info(f"   🚨 發現新動態：{ans}")
                user_ref.collection('pending_updates').document(str(time.time())).set({
                    "projectId": p_id, "projectName": name, "date": roc_date_str,
                    "note": f"【AI 綜合判讀】{ans}", 
                    "source": "多重來源彙整 (含官網/RSS/新聞)",
                    "sourceUrl": first_link if not sources else sources[0].get('url'), 
                    "createdAt": firestore.SERVER_TIMESTAMP
                })
            else:
                logger.info("   平靜無波。")

            # 每個案件處理完，主動休息 10 秒，保護 API 配額
            time.sleep(10)

        except Exception as err:
            logger.error(f"❌ 查核【{name}】時出錯：{err}")
            continue

    logger.info("\n🎉 查核任務結束！")

if __name__ == "__main__":
    main()
