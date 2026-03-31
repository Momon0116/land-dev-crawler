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
# 3. 輔助函式
# ==========================================
def fetch_gov_url_text(url):
    try:
        headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'}
        if not url.startswith('http'): url = 'https://' + url
        res = requests.get(url, headers=headers, timeout=20)
        res.raise_for_status()
        soup = BeautifulSoup(res.content, 'html.parser')
        for script in soup(["script", "style"]):
            script.extract()
        text = soup.get_text(separator='\n')
        lines = (line.strip() for line in text.splitlines())
        chunks = (phrase.strip() for line in lines for phrase in line.split("  "))
        clean_text = '\n'.join(chunk for chunk in chunks if chunk)
        return clean_text[:6000]
    except Exception as e:
        logger.warning(f"⚠️ 無法抓取網址內容 {url}: {e}")
        return ""

def fetch_google_news_rss(query):
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
            pubDate = item.pubDate.text if item.pubDate else ""
            news_summary += f"- {title} ({pubDate})\n"
        return news_summary, (items[0].link.text if items else url)
    except Exception as e:
        logger.warning(f"⚠️ RSS 抓取失敗 ({query}): {e}")
        return "", ""

def call_gemini_with_retry(prompt, max_retries=5):
    """將重試次數提升至 5 次，並增加重試間隔緩衝"""
    for attempt in range(max_retries):
        try:
            # 每次呼叫前基本緩衝，避免瞬間併發
            time.sleep(3)
            response = model.generate_content(prompt)
            return response.text.strip()
        except Exception as e:
            error_msg = str(e)
            if "429" in error_msg or "quota" in error_msg.lower():
                # 解析建議等待秒數
                match = re.search(r'retry in (\d+\.?\d*)s', error_msg)
                # 至少等待 60 秒，或依照官方要求再加 15 秒緩衝
                wait_time = max(60, (float(match.group(1)) + 15) if match else 60)
                
                logger.warning(f"   ⏳ 頻率限制 (RPM/TPM)，暫停 {wait_time:.1f} 秒後重試 (進度: {attempt+1}/{max_retries})...")
                time.sleep(wait_time)
            else:
                logger.error(f"   ❌ AI 判讀錯誤: {e}")
                return None
    return None

# ==========================================
# 4. 主程式
# ==========================================
def main():
    logger.info("🚀 啟動土地開發進度穩定版查核...")
    
    try:
        user_ref = db.collection('artifacts').document(APP_ID).collection('users').document(FIREBASE_UID)
        projects_query = user_ref.collection('projects').where(filter=FieldFilter('isArchived', '==', False))
        projects = list(projects_query.stream())
    except Exception as e:
        logger.error(f"❌ 讀取專案清單失敗: {e}")
        return

    now = datetime.now()
    roc_date_str = f"{now.year - 1911}.{now.strftime('%m.%d')}"

    for doc_snap in projects:
        try:
            p_id = doc_snap.id
            p_data = doc_snap.to_dict()
            name = p_data.get('name', '未知案件')
            city = p_data.get('city', '')
            keywords = p_data.get('keywords', '').strip()
            gov_url_str = p_data.get('govUrl', '').strip()
            
            logger.info(f"\n🔍 查核：【{name}】")
            
            # 主動冷卻：避免多個案件連續請求導致 API 崩潰
            logger.info("   ⏳ 主動冷卻 20 秒...")
            time.sleep(20)

            found_update = False
            update_note = ""
            source_url = ""
            source_type = ""

            if gov_url_str:
                urls = re.split(r'[\s,]+', gov_url_str)
                for url in urls:
                    if not url: continue
                    logger.info(f"   -> 官網掃描：{url}")
                    gov_text = fetch_gov_url_text(url)
                    if gov_text:
                        prompt = (
                            f"你是一個專業的土地開發分析師。\n"
                            f"以下是從政府特定網站擷取的內容。\n"
                            f"專案：【{name}】 關鍵字：【{keywords}】\n"
                            f"請判斷是否有最新公告、會議或進度？如果有，請用一句話(30字內)總結。\n"
                            f"若無新進度或找不到相關資訊，務必只回答「無更新」。\n\n"
                            f"內容摘要：\n{gov_text}"
                        )
                        ans = call_gemini_with_retry(prompt)
                        if ans and "無更新" not in ans and len(ans) > 2:
                            found_update = True
                            update_note = ans
                            source_url = url
                            source_type = "特定來源直擊"
                            break
            
            if not found_update:
                search_query = keywords if keywords else f"{city} {name}"
                logger.info(f"   -> 廣泛搜尋：{search_query}")
                news_text, first_link = fetch_google_news_rss(search_query)
                if news_text:
                    prompt = (
                        f"你是一個專業土地開發分析師。\n"
                        f"以下是關於「{search_query}」的最新搜尋結果：\n\n{news_text}\n\n"
                        f"判斷是否包含最新進度或政府公告？如果有，請用一句話總結重點。\n"
                        f"若為舊聞或廣告，務必只回答「無更新」。"
                    )
                    ans = call_gemini_with_retry(prompt)
                    if ans and "無更新" not in ans and len(ans) > 2:
                        found_update = True
                        update_note = ans
                        source_url = first_link
                        source_type = "網路公開資訊/新聞稿"

            if found_update:
                logger.info(f"   🚨 發現動態：{update_note}")
                record_id = str(datetime.now().timestamp()).replace('.', '')
                user_ref.collection('pending_updates').document(record_id).set({
                    "id": record_id, "projectId": p_id, "projectName": name,
                    "date": roc_date_str, "note": f"【AI 判讀】{update_note}", 
                    "source": source_type, "sourceUrl": source_url, 
                    "createdAt": firestore.SERVER_TIMESTAMP
                })
            else:
                logger.info("   平靜無波。")

        except Exception as p_err:
            logger.error(f"❌ 查核案件出錯，跳過：{p_err}")
            continue

    logger.info("\n🎉 全部案件查核完成！")

if __name__ == "__main__":
    main()
