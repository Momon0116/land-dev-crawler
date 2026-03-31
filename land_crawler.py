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

        # 🔑 讀取 GitHub Secrets 環境變數
        self.firebase_cred_raw = os.environ.get("FIREBASE_CREDENTIALS")
        self.target_user_id = os.environ.get("FIREBASE_UID")
        self.gemini_api_key = os.environ.get("GEMINI_API_KEY")

        # 啟動診斷
        logger.info("--- [系統啟動診斷] ---")
        logger.info(f"檔案位置: {__file__}")
        logger.info(f"Firebase 憑證: {'✅ 偵測到' if self.firebase_cred_raw else '❌ 缺失'}")
        logger.info(f"使用者 UID: {'✅ 偵測到' if self.target_user_id else '❌ 缺失'}")
        logger.info(f"AI API 金鑰: {'✅ 偵測到' if self.gemini_api_key else '❌ 缺失'}")
        
        self._initialize_services()

    def _initialize_services(self):
        try:
            # 1. 初始化 Firebase 連線
            if self.firebase_cred_raw:
                # 雲端模式：強化 JSON 解析容錯
                raw_json = self.firebase_cred_raw.strip()
                # 解決 GitHub 可能對 JSON 內容多加的一層引號
                if raw_json.startswith('"') and raw_json.endswith('"'):
                    raw_json = json.loads(raw_json)
                
                try:
                    cred_dict = json.loads(raw_json)
                    cred = credentials.Certificate(cred_dict)
                    logger.info("☁️ 雲端憑證解析成功")
                except json.JSONDecodeError as e:
                    logger.error(f"❌ JSON 格式損壞，請檢查 Secret 內容: {e}")
                    sys.exit(1)
            else:
                # 💻 本機測試模式：回退到您之前的 Windows 路徑
                local_path = r"C:\Users\User\work-report\Python\land-dev-dashboard-firebase-adminsdk-fbsvc-b9b9e79b3d.json"
                if os.path.exists(local_path):
                    logger.info(f"💻 採用本機模式，載入金鑰：{local_path}")
                    cred = credentials.Certificate(local_path)
                    if not self.target_user_id:
                        # 使用本機運作正常的預設 UID
                        self.target_user_id = "Gc4VCR2nTeVXmmmOUKFKwfZG2WF3"
                else:
                    logger.error("❌ 無法找到任何 Firebase 金鑰來源")
                    sys.exit(1)

            if not firebase_admin._apps:
                firebase_admin.initialize_app(cred)
            self.db = firestore.client()
            logger.info(f"✅ Firebase 連線成功，目標使用者：{self.target_user_id}")

            # 2. 初始化 Gemini AI (採用 Gemini 2.5 Flash)
            if self.gemini_api_key:
                genai.configure(api_key=self.gemini_api_key)
                self.ai_model = genai.GenerativeModel('gemini-2.5-flash-preview-09-2025')
                logger.info("🧠 AI 分析大腦已啟動")

        except Exception as e:
            logger.error(f"🔥 初始化致命錯誤: {e}")
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
            # 讀取 Firestore 專案清單
            # 路徑：/artifacts/land-dev-app/users/{userId}/projects
            user_ref = self.db.collection('artifacts').document(self.app_id).collection('users').document(self.target_user_id)
            docs = user_ref.collection('projects').stream()
            
            projects = []
            for d in docs:
                p_data = d.to_dict()
                if not p_data.get("isArchived"): projects.append(p_data)

            logger.info(f"📥 成功載入 {len(projects)} 筆案件進行雲端查核")

            for proj in projects:
                p_id, name, city = proj.get("id"), proj.get("name"), proj.get("city", "")
                logger.info(f"🔍 搜尋中：【{city} {name}】")
                
                # Google News RSS 搜尋
                params = {"q": f'"{city}" "{name}"', "hl": "zh-TW", "gl": "TW", "ceid": "TW:zh-Hant"}
                res = self.session.get("https://news.google.com/rss/search", params=params, timeout=10)
                soup = BeautifulSoup(res.content, 'xml')
                items = soup.find_all('item')[:3]

                for item in items:
                    title, link = item.title.text, item.link.text
                    # 簡易匹配與重複檢查
                    if name[:2] in title:
                        if any(h.get("sourceUrl") == link for h in proj.get("history", [])): continue

                        # AI 語意分析
                        note = f"抓取到新聞：{title[:15]}..."
                        if self.ai_model:
                            try:
                                prompt = f"請摘要新聞『{title}』與土地開發案『{city}{name}』的關係，字數限制15字。若無關請回 False。"
                                response = self.ai_model.generate_content(prompt)
                                if "False" in response.text: continue
                                note = response.text.strip()
                            except: pass

                        logger.info(f"🚨 發現動態：{note}")
                        # 寫入待審核區 (pending_updates)
                        record_id = str(datetime.now().timestamp())
                        
                        # 將西元年轉換為民國年格式
                        now = datetime.now()
                        roc_year = now.year - 1911
                        roc_date_str = f"{roc_year}.{now.strftime('%m.%d')}"
                        
                        user_ref.collection('pending_updates').document(record_id).set({
                            "id": record_id, "projectId": p_id, "projectName": name,
                            "date": roc_date_str,
                            "note": f"【AI查核】{note}", "source": "新聞資訊",
                            "sourceUrl": link, "createdAt": firestore.SERVER_TIMESTAMP
                        })
                        break 
        except Exception as e:
            logger.error(f"❌ 執行過程發生錯誤: {e}")

if __name__ == "__main__":
    crawler = LandDevCrawler()
    crawler.crawl_and_update()
