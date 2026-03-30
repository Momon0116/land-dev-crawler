import requests
from bs4 import BeautifulSoup
import logging
from datetime import datetime
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
import os
import json
import sys

# Firebase Admin SDK
import firebase_admin
from firebase_admin import credentials
from firebase_admin import firestore

# 匯入 Google Gemini AI 套件
import google.generativeai as genai

# ==========================================
# 1. 系統設定與日誌初始化
# ==========================================
logging.basicConfig(
    level=logging.INFO, 
    format='%(asctime)s [%(levelname)s] %(message)s', 
    datefmt='%Y-%m-%d %H:%M:%S', 
    force=True
)
logger = logging.getLogger(__name__)

class LandDevCrawler:
    def __init__(self):
        self.session = self._build_session()
        self.db = None
        self.ai_model = None
        self.app_id = "land-dev-app"

        # 🔑 讀取環境變數 (GitHub Secrets)
        self.firebase_cred_raw = os.environ.get("FIREBASE_CREDENTIALS")
        self.target_user_id = os.environ.get("FIREBASE_UID")
        self.gemini_api_key = os.environ.get("GEMINI_API_KEY")

        # 診斷日誌
        logger.info("--- 系統啟動診斷 ---")
        logger.info(f"Firebase Credentials: {'✅ 已讀取' if self.firebase_cred_raw else '❌ 缺失'}")
        logger.info(f"Firebase UID: {'✅ 已讀取' if self.target_user_id else '❌ 缺失'}")
        logger.info(f"Gemini API Key: {'✅ 已讀取' if self.gemini_api_key else '❌ 缺失'}")
        logger.info("--------------------")

        self._initialize_services()

    def _initialize_services(self):
        try:
            if self.firebase_cred_raw:
                # ☁️ 雲端模式
                logger.info("☁️ 嘗試解析雲端 Secret...")
                # 移除可能影響 JSON 解析的隱形字元
                clean_json = self.firebase_cred_raw.strip()
                # 容錯處理：如果 Secret 被錯誤地包在引號內，進行第二次解析
                if clean_json.startswith('"') and clean_json.endswith('"'):
                    clean_json = json.loads(clean_json)
                
                cred_dict = json.loads(clean_json)
                cred = credentials.Certificate(cred_dict)
            else:
                # 💻 本機模式
                local_path = r"C:\Users\User\work-report\Python\land-dev-dashboard-firebase-adminsdk-fbsvc-5811c0deb7.json"
                if os.path.exists(local_path):
                    logger.info(f"💻 採用本機測試模式，載入金鑰：{local_path}")
                    cred = credentials.Certificate(local_path)
                    # 本機若無 UID，可從本機日誌手動填入用於單機測試
                    if not self.target_user_id:
                        self.target_user_id = "UDlQYBAOPsZlGSSxFddidzzDPMk2"
                else:
                    logger.error("❌ 找不到任何金鑰來源")
                    sys.exit(1)

            if not firebase_admin._apps:
                firebase_admin.initialize_app(cred)
            self.db = firestore.client()
            logger.info(f"✅ Firebase 連線成功，目標使用者：{self.target_user_id}")

            if self.gemini_api_key:
                genai.configure(api_key=self.gemini_api_key)
                self.ai_model = genai.GenerativeModel('gemini-2.5-flash')
                logger.info("🧠 AI 分析模組已就緒")

        except Exception as e:
            logger.error(f"🔥 初始化發生錯誤：{e}")
            sys.exit(1)

    def _build_session(self):
        session = requests.Session()
        retry = Retry(total=3, backoff_factor=1, status_forcelist=[429, 500, 502, 503, 504])
        session.mount("https://", HTTPAdapter(max_retries=retry))
        session.headers.update({"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"})
        return session

    def crawl_and_update(self):
        if not self.db or not self.target_user_id: return

        try:
            # 獲取案件清單
            user_ref = self.db.collection('artifacts').document(self.app_id).collection('users').document(self.target_user_id)
            docs = user_ref.collection('projects').stream()
            
            projects = [d.to_dict() for d in docs if not d.to_dict().get("isArchived")]
            logger.info(f"📥 載入 {len(projects)} 筆案件進行查核")

            for proj in projects:
                p_id, name, city = proj.get("id"), proj.get("name"), proj.get("city", "")
                logger.info(f"🔍 搜尋案件：【{city} {name}】")
                
                params = {"q": f'"{city}" "{name}"', "hl": "zh-TW", "gl": "TW", "ceid": "TW:zh-Hant"}
                res = self.session.get("https://news.google.com/rss/search", params=params, timeout=10)
                soup = BeautifulSoup(res.content, 'xml')
                items = soup.find_all('item')[:3]

                for item in items:
                    title, link = item.title.text, item.link.text
                    if name[:2] in title:
                        history = proj.get("history", [])
                        if any(h.get("sourceUrl") == link for h in history): continue

                        ai_note = f"發現動態：{title[:15]}..."
                        if self.ai_model:
                            try:
                                prompt = f"請摘要『{title}』與『{city}{name}』開發案的關係(15字內)。若無關請回 False。"
                                response = self.ai_model.generate_content(prompt)
                                if "False" in response.text: continue
                                ai_note = response.text.strip()
                            except: pass

                        logger.info(f"🚨 偵測到新動態：{ai_note}")
                        record_id = str(datetime.now().timestamp())
                        new_data = {
                            "id": record_id, "projectId": p_id, "projectName": name,
                            "date": datetime.now().strftime("%Y.%m.%d"),
                            "note": f"【AI查核】{ai_note}", "source": "新聞資訊",
                            "sourceUrl": link, "createdAt": firestore.SERVER_TIMESTAMP
                        }
                        user_ref.collection('pending_updates').document(record_id).set(new_data)
                        break 
        except Exception as e:
            logger.error(f"❌ 查核執行錯誤：{e}")

if __name__ == "__main__":
    crawler = LandDevCrawler()
    crawler.crawl_and_update()
