import os
import json
import logging
import requests
import re
from datetime import datetime
from bs4 import BeautifulSoup
import firebase_admin
from firebase_admin import credentials, firestore
import google.generativeai as genai
import urllib.parse

# ==========================================
# 0. 系統日誌設定
# ==========================================
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# ==========================================
# 1. 環境變數與金鑰載入
# ==========================================
FIREBASE_CREDENTIALS = os.environ.get("FIREBASE_CREDENTIALS")
FIREBASE_UID = os.environ.get("FIREBASE_UID")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")
APP_ID = "land-dev-app"

if not all([FIREBASE_CREDENTIALS, FIREBASE_UID, GEMINI_API_KEY]):
    logger.error("❌ 嚴重錯誤：找不到必要的環境變數 (FIREBASE_CREDENTIALS, FIREBASE_UID 或 GEMINI_API_KEY)")
    exit(1)

# ==========================================
# 2. 初始化 Firebase 與 Gemini
# ==========================================
try:
    cred_dict = json.loads(FIREBASE_CREDENTIALS)
    cred = credentials.Certificate(cred_dict)
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
def fetch_gov_url_text(url):
    """直接抓取特定政府網站的文字內容"""
    try:
        headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'}
        if not url.startswith('http'): url = 'https://' + url
        res = requests.get(url, headers=headers, timeout=15)
        res.raise_for_status()
        
        soup = BeautifulSoup(res.content, 'html.parser')
        # 移除 script 與 style 以減少雜訊
        for script in soup(["script", "style"]):
            script.extract()
            
        text = soup.get_text(separator='\n')
        # 清理多餘空白
        lines = (line.strip() for line in text.splitlines())
        chunks = (phrase.strip() for line in lines for phrase in line.split("  "))
        clean_text = '\n'.join(chunk for chunk in chunks if chunk)
        
        # 限制字數避免超過 AI token 限制
        return clean_text[:6000]
    except Exception as e:
        logger.warning(f"⚠️ 無法抓取網址內容 {url}: {e}")
        return ""

def fetch_google_news_rss(query):
    """透過 Google 新聞 RSS 抓取關鍵字最新動態"""
    try:
        encoded_query = urllib.parse.quote(query)
        url = f"https://news.google.com/rss/search?q={encoded_query}&hl=zh-TW&gl=TW&ceid=TW:zh-Hant"
        headers = {'User-Agent': 'Mozilla/5.0'}
        res = requests.get(url, headers=headers, timeout=10)
        res.raise_for_status()
        
        soup = BeautifulSoup(res.content, 'xml')
        items = soup.find_all('item')[:5] # 取前5則最新消息
        
        news_summary = ""
        for item in items:
            title = item.title.text if item.title else ""
            pubDate = item.pubDate.text if item.pubDate else ""
            news_summary += f"- {title} ({pubDate})\n"
            
        return news_summary, (items[0].link.text if items else url)
    except Exception as e:
        logger.warning(f"⚠️ Google RSS 抓取失敗 ({query}): {e}")
        return "", ""

# ==========================================
# 4. 主程式邏輯 (三合一查核)
# ==========================================
def main():
    logger.info("🚀 開始執行土地開發進度自動查核...")
    
    user_ref = db.collection('artifacts').document(APP_ID).collection('users').document(FIREBASE_UID)
    projects = user_ref.collection('projects').where('isArchived', '==', False).stream()
    
    # 產生當天民國年字串 (例如: 115.03.31)
    now = datetime.now()
    roc_date_str = f"{now.year - 1911}.{now.strftime('%m.%d')}"

    for doc_snap in projects:
        p_id = doc_snap.id
        p_data = doc_snap.to_dict()
        name = p_data.get('name', '未知案件')
        city = p_data.get('city', '')
        keywords = p_data.get('keywords', '').strip()
        gov_url_str = p_data.get('govUrl', '').strip()
        
        logger.info(f"\n🔍 正在查核案件：【{name}】")
        
        found_update = False
        update_note = ""
        source_url = ""
        source_type = ""

        # --- 策略 A：如果有設定特定政府網址，優先掃描網址 ---
        if gov_url_str:
            urls = re.split(r'[\s,]+', gov_url_str)
            for url in urls:
                if not url: continue
                logger.info(f"   -> 掃描特定網頁：{url}")
                gov_text = fetch_gov_url_text(url)
                
                if gov_text:
                    prompt = (
                        f"你是一個專業的土地開發與都市計畫分析師。\n"
                        f"以下是從政府特定網站擷取的最新文字內容。\n"
                        f"目標追蹤專案：【{name}】\n"
                        f"關聯關鍵字：【{keywords}】\n"
                        f"請嚴格判斷網頁內容中是否有關於此案的「最新公告」、「會議進度」或「辦理情形」？\n"
                        f"如果有實質進度，請用一句話（30字以內）總結重點，不要包含引號或多餘問候。\n"
                        f"如果沒有新的進度、找不到相關資訊，請務必只回答「無更新」。\n\n"
                        f"網頁內容摘要：\n{gov_text}"
                    )
                    
                    try:
                        response = model.generate_content(prompt)
                        ans = response.text.strip()
                        if ans and "無更新" not in ans and len(ans) > 2:
                            found_update = True
                            update_note = ans
                            source_url = url
                            source_type = "特定來源直擊"
                            break # 找到進度就跳出該案件的網址迴圈
                    except Exception as e:
                        logger.error(f"   ❌ AI 判讀官網內容時發生錯誤: {e}")
        
        # --- 策略 B：如果官網沒動靜，或是沒設官網，則啟動關鍵字 RSS 廣泛搜尋 ---
        if not found_update:
            search_query = keywords if keywords else f"{city} {name}"
            logger.info(f"   -> 啟動廣泛搜尋：{search_query}")
            news_text, first_link = fetch_google_news_rss(search_query)
            
            if news_text:
                prompt = (
                    f"你是一個專業的土地開發分析師。\n"
                    f"以下是關於「{search_query}」的最新網路新聞或公告搜尋結果：\n\n"
                    f"{news_text}\n\n"
                    f"請判斷這些資訊中，是否包含該案件實質的「最新進度」、「審議結果」或「政府公告」？\n"
                    f"如果有，請用一句話（30字以內）總結最新動態，不要包含引號。\n"
                    f"如果是舊聞、無關新聞、或純粹房地產廣告，請務必只回答「無更新」。"
                )
                
                try:
                    response = model.generate_content(prompt)
                    ans = response.text.strip()
                    if ans and "無更新" not in ans and len(ans) > 2:
                        found_update = True
                        update_note = ans
                        source_url = first_link
                        source_type = "網路公開資訊/新聞稿"
                except Exception as e:
                    logger.error(f"   ❌ AI 判讀新聞內容時發生錯誤: {e}")

        # --- 寫入 Firebase 待審核區 ---
        if found_update:
            logger.info(f"   🚨 發現動態：{update_note} ({source_type})")
            try:
                record_id = str(datetime.now().timestamp()).replace('.', '')
                user_ref.collection('pending_updates').document(record_id).set({
                    "id": record_id, 
                    "projectId": p_id, 
                    "projectName": name,
                    "date": roc_date_str, # 自動轉換的民國年格式
                    "note": f"【AI 判讀最新動態】{update_note}", 
                    "source": source_type,
                    "sourceUrl": source_url, 
                    "createdAt": firestore.SERVER_TIMESTAMP
                })
            except Exception as e:
                logger.error(f"   ❌ 寫入資料庫失敗: {e}")
        else:
            logger.info("   平靜無波，無最新動態。")

    logger.info("\n🎉 所有案件查核完畢！")

if __name__ == "__main__":
    main()
