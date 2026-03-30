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
        self.ai_model = None
        
        # 🔑 【設定區】
        # 雲端環境會自動讀取 GitHub Secrets
        # 本機環境若讀不到，請在下方引號內填入您的 UID
        self.target_user_id = os.environ.get("FIREBASE_UID") or "UD1QYBA0PsZlGSSxFddidzZDPMk2" 
        
        self.gemini_api_key = os.environ.get("GEMINI_API_KEY") or "AIzaSyCAQLETzOJuU9Rfega0CuI-NffBC-Cp0cI"
        firebase_cred_raw = os.environ.get("FIREBASE_CREDENTIALS")
        
        # ⚠️ 此 ID 必須與 Dashboard.html 中的 appId 完全相同
        self.app_id = "land-dev-app"

        try:
            # 初始化 Firebase 連線
            if firebase_cred_raw:
                logger.info("☁️ 偵測到雲端 Secret，啟動雲端模式...")
                cred_dict = json.loads(firebase_cred_raw.strip())
                cred = credentials.Certificate(cred_dict)
            else:
                # 本機測試模式：讀取金鑰檔案
                local_path = r"C:\Users\User\work-report\Python\land-dev-dashboard-firebase-adminsdk-fbsvc-5811c0deb7.json" 
                if os.path.exists(local_path):
                    logger.info(f"💻 未偵測到雲端變數，讀取本機金鑰檔案：{local_path}")
                    cred = credentials.Certificate(local_path)
                else:
                    logger.error("❌ 嚴重錯誤：找不到任何金鑰來源！請確認 JSON 檔案路徑或 GitHub Secrets 設定。")
                    sys.exit(1)

            # 二次確認 UID 是否已填寫
            if not self.target_user_id or "您的_FIREBASE" in self.target_user_id:
                logger.error("❌ 錯誤：缺少 FIREBASE_UID。")
                logger.error("提示：若在本機執行，請直接修改 land_crawler.py 第 39 行填入 UID。")
                sys.exit(1)

            # 啟動 Firebase 服務
            if not firebase_admin._apps:
                firebase_admin.initialize_app(cred)
            self.db = firestore.client()
            logger.info("✅ 成功連線至 Firebase 雲端資料庫！")
            
            # 初始化 Gemini AI
            if self.gemini_api_key and "您的_GEMINI" not in self.gemini_api_key:
                genai.configure(api_key=self.gemini_api_key)
                self.ai_model = genai.GenerativeModel('gemini-2.5-flash')
                logger.info("🧠 AI 語意分析大腦 (Gemini) 初始化完成！")
            else:
                logger.warning("⚠️ 未設定 GEMINI_API_KEY，將跳過 AI 分析，所有結果將直接推播。")

        except Exception as e:
            logger.error(f"🔥 初始化階段發生嚴重錯誤: {e}")
            sys.exit(1)

    def _build_session(self):
        session = requests.Session()
        retry_strategy = Retry(total=5, backoff_factor=1, status_forcelist=[429, 500, 502, 503, 504])
        adapter = HTTPAdapter(max_retries=retry_strategy)
        session.mount("http://", adapter)
        session.mount("https://", adapter)
        session.headers.update({"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"})
        return session

    def fetch_projects_from_db(self):
        if not self.db: return []
        try:
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
            logger.info(f"📥 已從使用者 {self.target_user_id} 載入 {len(tracked_projects)} 筆案件。")
            return tracked_projects
        except Exception as e:
            logger.error(f"從資料庫讀取專案失敗: {e}")
            return []

    def analyze_with_ai(self, city, proj_name, title):
        if not self.ai_model: return "發現新動態"
        prompt = f"分析新聞『{title}』是否與『{city}{proj_name}』的開發、重劃或實質進度有關。若無關回覆 False，若有關請用15字摘要。"
        try:
            response = self.ai_model.generate_content(prompt)
            text = response.text.strip()
            if "False" in text or "false" in text or text == "":
                return None
            return text
        except Exception as e:
            logger.error(f"AI 判斷發生錯誤: {e}")
            return "發現新進度 (AI 判斷異常)"

    def crawl_and_update(self):
        projects = self.fetch_projects_from_db()
        if not projects:
            logger.warning("📭 目前沒有進行中的案件，結束任務。")
            return

        for proj in projects:
            proj_id, proj_name, city = proj["id"], proj["name"], proj["city"]
            logger.info(f"🔍 搜尋中：【{city} - {proj_name}】...")
            
            try:
                params = {"q": f'"{city}" "{proj_name}"', "hl": "zh-TW", "gl": "TW", "ceid": "TW:zh-Hant"}
                response = self.session.get("https://news.google.com/rss/search", params=params, timeout=15)
                soup = BeautifulSoup(response.content, 'xml')
                items = soup.find_all('item')

                for item in items:
                    title, link = item.title.text, item.link.text
                    if proj_name[:2] in title:
                        is_duplicate = any(h.get('sourceUrl') == link for h in proj["history"])
                        if not is_duplicate:
                            ai_summary = self.analyze_with_ai(city, proj_name, title)
                            if ai_summary:
                                logger.info(f"  🚨 發現動態！送入網頁待審核區 -> {ai_summary}")
                                record_id = str(datetime.now().timestamp())
                                new_record = {
                                    "id": record_id,
                                    "projectId": proj_id,
                                    "projectName": proj_name,
                                    "date": datetime.now().strftime("%Y.%m.%d"),
                                    "note": f"【AI 查核】{ai_summary}",
                                    "source": "網路新聞資訊",
                                    "sourceUrl": link,
                                    "createdAt": firestore.SERVER_TIMESTAMP
                                }
                                pending_ref = self.db.collection('artifacts').document(self.app_id).collection('users').document(self.target_user_id).collection('pending_updates').document(record_id)
                                pending_ref.set(new_record)
                                break 
            except Exception as e:
                logger.error(f"查核案件【{proj_name}】錯誤: {e}")

if __name__ == "__main__":
    crawler = LandDevCrawler()
    crawler.crawl_and_update()
