import requests
from bs4 import BeautifulSoup
import logging
from datetime import datetime
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
import email.utils
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
logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s', datefmt='%Y-%m-%d %H:%M:%S', force=True)
logger = logging.getLogger(__name__)

class LandDevCrawler:
    def __init__(self):
        self.session = self._build_session()
        self.db = None
        
        # 🔑 設定環境變數名稱 (需與 GitHub Secrets 一致)
        firebase_cred_raw = os.environ.get("FIREBASE_CREDENTIALS")
        self.target_user_id = os.environ.get("FIREBASE_UID")
        self.gemini_api_key = os.environ.get("GEMINI_API_KEY")
        
        # ⚠️ 此 ID 必須與 Dashboard.html 中的 appId 完全相同
        self.app_id = "land-dev-app"

        try:
            if firebase_cred_raw:
                # ☁️ 雲端模式
                logger.info("☁️ 正在解析雲端環境變數 FIREBASE_CREDENTIALS...")
                cred_dict = json.loads(firebase_cred_raw)
                cred = credentials.Certificate(cred_dict)
            else:
                # 💻 本機模式 (請替換為您電腦上的實際路徑)
                local_path = r"C:\Users\User\work-report\Python\land-dev-dashboard-firebase-adminsdk-fbsvc-5811c0deb7.json" 
                if os.path.exists(local_path):
                    logger.info(f"💻 讀取本機金鑰：{local_path}")
                    cred = credentials.Certificate(local_path)
                else:
                    raise ValueError("❌ 錯誤：找不到金鑰！請檢查 GitHub Secrets 是否設定了 FIREBASE_CREDENTIALS")

            if not self.target_user_id:
                raise ValueError("❌ 錯誤：缺少 FIREBASE_UID (請在 GitHub Secrets 設定)")
            if not self.gemini_api_key:
                raise ValueError("❌ 錯誤：缺少 GEMINI_API_KEY")

            # 初始化 Firebase
            if not firebase_admin._apps:
                firebase_admin.initialize_app(cred)
            self.db = firestore.client()
            logger.info("✅ 成功連線至 Firebase 雲端資料庫！")
            
            # 初始化 Gemini
            genai.configure(api_key=self.gemini_api_key)
            self.ai_model = genai.GenerativeModel('gemini-2.5-flash')
            logger.info("🧠 AI 語意分析大腦 (Gemini) 就緒！")

        except Exception as e:
            logger.error(f"🔥 初始化失敗: {e}")
            sys.exit(1) # 強制停止

    def _build_session(self):
        session = requests.Session()
        retry_strategy = Retry(total=3, backoff_factor=1)
        adapter = HTTPAdapter(max_retries=retry_strategy)
        session.mount("http://", adapter)
        session.mount("https://", adapter)
        session.headers.update({"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"})
        return session

    def fetch_projects_from_db(self):
        """讀取路徑：/artifacts/land-dev-app/public/data/projects (依據您的 DB 結構調整)"""
        try:
            # 修改為與網頁端一致的私有資料路徑
            projects_ref = self.db.collection('artifacts').document(self.app_id).collection('users').document(self.target_user_id).collection('projects')
            docs = projects_ref.stream()
            
            tracked_projects = []
            for doc in docs:
                data = doc.to_dict()
                if not data.get("isArchived", False):
                    tracked_projects.append({
                        "id": doc.id,
                        "name": data.get("name"),
                        "city": data.get("city"),
                        "history": data.get("history", [])
                    })
            logger.info(f"📥 已載入 {len(tracked_projects)} 筆進行中的案件。")
            return tracked_projects
        except Exception as e:
            logger.error(f"讀取資料失敗: {e}")
            return []

    def analyze_with_ai(self, city, proj_name, title):
        prompt = f"分析新聞『{title}』是否與『{city}{proj_name}』的開發、重劃或進度直接相關。若無關回覆 False，若有關請用15字摘要。"
        try:
            response = self.ai_model.generate_content(prompt)
            text = response.text.strip()
            if "False" in text or "false" in text or text == "":
                return None
            return text
        except Exception as e:
            logger.error(f"AI 判斷錯誤: {e}")
            return None

    def crawl_and_update(self):
        projects = self.fetch_projects_from_db()
        for proj in projects:
            proj_id, proj_name, city = proj["id"], proj["name"], proj["city"]
            logger.info(f"🔍 搜尋：【{city} - {proj_name}】...")
            
            try:
                params = {"q": f'"{city}" "{proj_name}"', "hl": "zh-TW", "gl": "TW", "ceid": "TW:zh-Hant"}
                response = self.session.get("https://news.google.com/rss/search", params=params, timeout=10)
                soup = BeautifulSoup(response.content, 'xml')
                items = soup.find_all('item')

                for item in items:
                    title, link = item.title.text, item.link.text
                    if proj_name[:2] in title:
                        is_duplicate = any(h.get('sourceUrl') == link for h in proj["history"])
                        if not is_duplicate:
                            ai_summary = self.analyze_with_ai(city, proj_name, title)
                            if ai_summary:
                                logger.info(f"  🚨 發現動態：{ai_summary}")
                                record_id = str(datetime.now().timestamp())
                                new_record = {
                                    "id": record_id,
                                    "projectId": proj_id,
                                    "projectName": proj_name,
                                    "date": datetime.now().strftime("%Y.%m.%d"),
                                    "note": f"【AI 查核】{ai_summary}",
                                    "source": "網路新聞",
                                    "sourceUrl": link,
                                    "createdAt": firestore.SERVER_TIMESTAMP
                                }
                                # 寫入待審核路徑
                                pending_ref = self.db.collection('artifacts').document(self.app_id).collection('users').document(self.target_user_id).collection('pending_updates').document(record_id)
                                pending_ref.set(new_record)
                                break 
            except Exception as e:
                logger.error(f"搜尋【{proj_name}】錯誤: {e}")

if __name__ == "__main__":
    crawler = LandDevCrawler()
    crawler.crawl_and_update()
