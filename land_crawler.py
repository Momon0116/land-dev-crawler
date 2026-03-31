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
# 3. 爬蟲與 AI 判讀輔助函式
# ==========================================

def fetch_content(url):
    """
    自動偵測 URL 類型並抓取內容：
    若是 RSS (XML) 則抓取標題清單；若是普通網頁則抓取純文字。
    """
    try:
        headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'}
        if not url.startswith('http'): url = 'https://' + url
        res = requests.get(url, headers=headers, timeout=20)
        res.raise_for_status()
        
        # 判斷是否為 RSS/XML 格式
        content_type = res.headers.get('Content-Type', '').lower()
        if 'xml' in content_type or url.endswith('.xml') or url.endswith('.rss') or '<rss' in res.text[:200]:
            logger.info(f"   -> 偵測到 RSS 頻道：解析標題清單...")
            soup = BeautifulSoup(res.content, 'xml')
            items = soup.find_all(['item', 'entry'])[:10] # 取最新 10 筆
            rss_text = ""
            for item in items:
                title = item.find(['title']).text if item.find(['title']) else ""
                pub_date = item.find(['pubDate', 'published', 'updated']).text if item.find(['pubDate', 'published', 'updated']) else ""
                rss_text += f"- {title} ({pub_date})\n"
            return rss_text, "RSS"
        else:
            # 普通網頁解析
            soup = BeautifulSoup(res.content, 'html.parser')
            for script in soup(["script", "style"]): script.extract()
            text = soup.get_text(separator='\n')
            lines = (line.strip() for line in text.splitlines())
            clean_text = '\n'.join(chunk for chunk in lines if chunk)
            return clean_text[:6000], "HTML"
    except Exception as e:
        logger.warning(f"⚠️ 無法抓取來源內容 {url}: {e}")
        return "", None

def fetch_google_news_rss(query):
    """Google 新聞搜尋備援策略"""
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
            news_summary += f"- {title}\n"
        return news_summary, (items[0].link.text if items else url)
    except:
        return "", ""

def call_gemini_with_retry(prompt, max_retries=5):
    """處理 AI 限速重試邏輯"""
    for attempt in range(max_retries):
        try:
            time.sleep(3)
            response = model.generate_content(prompt)
            return response.text.strip()
        except Exception as e:
            error_msg = str(e)
            if "429" in error_msg or "quota" in error_msg.lower():
                match = re.search(r'retry in (\d+\.?\d*)s', error_msg)
                wait_time = max(60, (float(match.group(1)) + 15) if match else 60)
                logger.warning(f"   ⏳ 觸發 API 限速，暫停 {wait_time:.1f} 秒後重試...")
                time.sleep(wait_time)
            else:
                return None
    return None

# ==========================================
# 4. 主程式邏輯
# ==========================================
def main():
    logger.info("🚀 啟動土地開發進度查核 (RSS整合版)...")
    
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
            # 優先使用新版的 sources 陣列
            sources = p_data.get('sources', [])
            
            logger.info(f"\n🔍 查核案件：【{name}】")
            time.sleep(15) # 主動冷卻

            found_update = False
            update_note = ""
            source_url = ""
            source_type = ""

            # --- 策略一：檢查所有特定來源 (包含 RSS 或 特定網頁) ---
            for s in sources:
                url = s.get('url', '').strip()
                s_name = s.get('name', '官方來源')
                if not url: continue
                
                logger.info(f"   -> 掃描來源：{s_name} ({url})")
                content, mode = fetch_content(url)
                
                if content:
                    prompt = (
                        f"你是一個土地開發分析師。以下是從「{s_name}」({mode}格式)抓取的最新內容。\n"
                        f"專案：【{name}】 關鍵字：【{keywords}】\n"
                        f"請判斷內容中是否有該案的最新進度、會議、或公告？\n"
                        f"如果有，請用一句話總結重點(30字內)。若無，務必只回答「無更新」。\n\n"
                        f"抓取內容：\n{content}"
                    )
                    ans = call_gemini_with_retry(prompt)
                    if ans and "無更新" not in ans and len(ans) > 2:
                        found_update = True
                        update_note = ans
                        source_url = url
                        source_type = f"特定來源：{s_name}"
                        break # 找到動態就停止該案的其他來源掃描

            # --- 策略二：若策略一無結果，啟動 Google 新聞備援 ---
            if not found_update:
                search_query = keywords if keywords else f"{p_data.get('city', '')} {name}"
                logger.info(f"   -> 來源無動靜，啟動廣泛搜尋：{search_query}")
                news_text, first_link = fetch_google_news_rss(search_query)
                if news_text:
                    prompt = (f"分析以下新聞標題中關於「{name}」的最新進度，若有請一句話總結，無則回「無更新」：\n\n{news_text}")
                    ans = call_gemini_with_retry(prompt)
                    if ans and "無更新" not in ans:
                        found_update = True
                        update_note = ans
                        source_url = first_link
                        source_type = "網路公開資訊/新聞稿"

            # --- 寫入資料庫 ---
            if found_update:
                logger.info(f"   🚨 發現新動態：{update_note}")
                user_ref.collection('pending_updates').document(str(time.time())).set({
                    "projectId": p_id, "projectName": name, "date": roc_date_str,
                    "note": f"【AI查核】{update_note}", "source": source_type,
                    "sourceUrl": source_url, "createdAt": firestore.SERVER_TIMESTAMP
                })
            else:
                logger.info("   平靜無波。")

        except Exception as err:
            logger.error(f"❌ 查核出錯：{err}")
            continue

    logger.info("\n🎉 查核任務結束！")

if __name__ == "__main__":
    main()
