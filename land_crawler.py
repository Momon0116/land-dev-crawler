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

def normalize_text(text):
    """正規化字串：統一括號與引號格式，轉小寫以提升比對成功率"""
    if not text: return ""
    return text.replace('（', '(').replace('）', ')').replace('「', '').replace('」', '').lower()

def fetch_content(url, keywords_list=None):
    """抓取內容：若是 RSS 則解析清單，若是網頁則抓純文字"""
    try:
        # 增強瀏覽器偽裝，加入語系與安全標頭，降低被政府 WAF 阻擋的機率
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36',
            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8',
            'Accept-Language': 'zh-TW,zh;q=0.9,en-US;q=0.8,en;q=0.7',
            'Connection': 'keep-alive',
            'Upgrade-Insecure-Requests': '1'
        }
        if not url.startswith('http'): url = 'https://' + url
        res = requests.get(url, headers=headers, timeout=20)
        res.raise_for_status()
        
        content_type = res.headers.get('Content-Type', '').lower()
        if 'xml' in content_type or url.endswith('.xml') or url.endswith('.rss') or '<rss' in res.text[:200]:
            soup = BeautifulSoup(res.content, 'xml')
            
            # 修正一：放寬 RSS 讀取筆數，從 10 筆提高到 30 筆，避免被洗版漏抓
            items = soup.find_all(['item', 'entry'])[:30]
            rss_text = ""
            for item in items:
                title = item.find(['title']).text if item.find(['title']) else ""
                
                # 修正二：讀取 description (摘要/時間) 與 pubDate (發布日)
            desc_tag = item.find(['description', 'summary', 'content'])
            desc_str = ""
            if desc_tag and desc_tag.text:
                # 簡單過濾掉可能存在的 HTML 標籤
                desc_str = BeautifulSoup(desc_tag.text, 'html.parser').get_text(separator=' ', strip=True)[:150]
            
            date_tag = item.find(['pubDate', 'published', 'updated'])
            date_str = date_tag.text.strip() if date_tag else ""
            
            # 新增：本地端嚴格關鍵字過濾 (解決 AI 移除後的雜訊問題)
            if keywords_list:
                item_full_text = normalize_text(f"{title} {desc_str}")
                has_keyword = any(normalize_text(kw) in item_full_text for kw in keywords_list if kw)
                if not has_keyword:
                    continue # 若這則 RSS 完全沒提到關鍵字，直接丟棄
            
            rss_text += f"- [RSS項目] {title} | 日期: {date_str} | 摘要: {desc_str}\n"
        return rss_text
    else:
        soup = BeautifulSoup(res.content, 'html.parser')
        for script in soup(["script", "style"]): script.extract()
        text = soup.get_text(separator='\n')
        lines = (line.strip() for line in text.splitlines())
        clean_text = '\n'.join(chunk for chunk in lines if chunk)[:3000] # 限制單一網頁字數

        # 新增：本地端嚴格關鍵字過濾
        if keywords_list:
            has_keyword = any(normalize_text(kw) in normalize_text(clean_text) for kw in keywords_list if kw)
            if not has_keyword:
                return "" # 整個網頁都沒提到關鍵字，當作無更新

        return clean_text
except Exception as e:
    logger.warning(f"   ⚠️ 無法抓取 {url}: {e}")
    return ""

def fetch_google_news_text(query):
    """Google 新聞搜尋內容彙整"""
    try:
        encoded_query = urllib.parse.quote(query)
        url = f"https://news.google.com/rss/search?q={encoded_query}&hl=zh-TW&gl=TW&ceid=TW:zh-Hant"
        # Google 也加上基礎偽裝
        headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36'}
        res = requests.get(url, headers=headers, timeout=15)
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
        
        # 將關鍵字字串轉為陣列，供本地過濾使用 (若無設定則用案名)
        kw_str = keywords.replace('，', ',') if keywords else name
        keywords_list = [k.strip() for k in kw_str.split(',') if k.strip()]

        # 1. 蒐集所有來源資訊 (不立刻問 AI)
        all_raw_data = []
        
        # A. 抓取特定來源
        for s in sources:
            url = s.get('url', '').strip()
            s_name = s.get('name', '官方來源')
            if not url: continue
            logger.info(f"   -> 蒐集來源：{s_name}")
            content = fetch_content(url, keywords_list) # 傳入關鍵字進行過濾
            if content:
                all_raw_data.append(f"【來源名稱：{s_name}】\n{content}")

        # B. 抓取 Google 新聞作為備援資訊
        search_query = keywords if keywords else f"{p_data.get('city', '')} {name}"
            # 修復：將使用的關鍵字印在 Log 中，確保查詢邏輯透明
            logger.info(f"   -> 蒐集 Google 新聞搜尋結果 (使用關鍵字: {search_query})")
            news_text, first_link = fetch_google_news_text(search_query)
            if news_text:
                all_raw_data.append(f"【Google 新聞搜尋結果】\n{news_text}")

            # 2. 直接將抓取到的原始資料合併送出 (移除 AI 判讀)
            if not all_raw_data:
                logger.info("   平靜無波 (無任何資料可供查核)。")
                continue

            combined_info = "\n\n---\n\n".join(all_raw_data)
            
            # 擷取前 1500 字，避免塞爆資料庫與前端介面
            preview_text = combined_info[:1500] + ("\n\n...(資料過長，已截斷)..." if len(combined_info) > 1500 else "")

            logger.info("   🚨 已收集到原始資料，直接送至人工待審核區...")
            user_ref.collection('pending_updates').document(str(time.time())).set({
                "projectId": p_id, "projectName": name, "date": roc_date_str,
                "note": f"【機器人原始抓取資料】\n{preview_text}", 
                "source": "多重來源彙整 (未經 AI 過濾)",
                "sourceUrl": first_link if not sources else sources[0].get('url', ''), 
                "createdAt": firestore.SERVER_TIMESTAMP
            })

            # 每個案件處理完，主動休息 5 秒 (無須等 AI，速度大幅加快)
            time.sleep(5)

        except Exception as err:
            logger.error(f"❌ 查核【{name}】時出錯：{err}")
            continue

    logger.info("\n🎉 查核任務結束！")

if __name__ == "__main__":
    main()
