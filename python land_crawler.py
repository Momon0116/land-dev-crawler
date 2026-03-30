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

        # 診斷：確認變數是否存在
        logger.info("--- 啟動診斷 (Cloud Diagnostic) ---")
        logger.info(f"Secret-Credentials: {'✅ 偵測到' if self.firebase_cred_raw else '❌ 缺失'}")
        logger.info(f"Secret-UID: {'✅ 偵測到' if self.target_user_id else '❌ 缺失'}")
        logger.info(f"Secret-AI_Key: {'✅ 偵測到' if self.gemini_api_key else '❌ 缺失'}")
        
        self._initialize_services()

    def _initialize_services(self):
        try:
            # 1. 初始化 Firebase
            if self.firebase_cred_raw:
                # 雲端模式：強化 JSON 解析與清理
                raw_json = self.firebase_cred_raw.strip()
                # 容錯處理：移除可能的頭尾多餘雙引號 (常見於 GitHub Secrets 貼上錯誤)
                if raw_json.startswith('"') and raw_json.endswith('"'):
                    raw_json = json.loads(raw_json)
                
                try:
                    cred_dict = json.loads(raw_json)
                    cred = credentials.Certificate(cred_dict)
                    logger.info("☁️ 雲端憑證解析成功")
                except json.JSONDecodeError as e:
                    logger.error(f"❌ JSON 解析失敗，請檢查 Secret 內容是否為完整的大括號 JSON 格式: {e}")
                    sys.exit(1)
            else:
                # 本機測試模式
                local_path = r"C:\Users\User\work-report\Python\land-dev-dashboard-firebase-adminsdk-fbsvc-5811c0deb7.json"
                if os.path.exists(local_path):
                    logger.info(f"💻 本機模式，讀取金鑰：{local_path}")
                    cred = credentials.Certificate(local_path)
                    if not self.target_user_id:
                        # 這是您本機日誌中看到的 UID，填入以供本機測試
                        self.target_user_id = "UDlQYBAOPsZlGSSxFddidzzDPMk2"
                else:
                    logger.error("❌ 錯誤：找不到任何金鑰來源（Secrets 或 JSON 檔案）")
                    sys.exit(1)

            if not firebase_admin._apps:
                firebase_admin.initialize_app(cred)
            self.db = firestore.client()
            logger.info(f"✅ Firebase 連線成功，使用者：{self.target_user_id}")

            # 2. 初始化 AI
            if self.gemini_api_key:
                genai.configure(api_key=self.gemini_api_key)
                self.ai_model = genai.GenerativeModel('gemini-2.5-flash')
                logger.info("🧠 AI 分析模組就緒")

        except Exception as e:
            logger.error(f"🔥 系統初始化致命錯誤: {e}")
            sys.exit(1)

    def _build_session(self):
        session = requests.Session()
        retry = Retry(total=3, backoff_factor=1, status_forcelist=[429, 500, 502, 503, 504])
        session.mount("https://", HTTPAdapter(max_retries=retry))
        session.headers.update({"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"})
        return session

    def crawl_and_update(self):
        if not self.db or not self.target_user_id: return

        # 指向資料夾：artifacts -> land-dev-app -> users -> {uid} -> projects
        try:
            user_ref = self.db.collection('artifacts').document(self.app_id).collection('users').document(self.target_user_id)
            docs = user_ref.collection('projects').stream()
            
            projects = []
            for d in docs:
                p_data = d.to_dict()
                if not p_data.get("isArchived"): projects.append(p_data)

            logger.info(f"📥 成功讀取 {len(projects)} 筆案件")
            if len(projects) == 0:
                logger.warning("💡 提示：目前資料庫中沒有進行中的案件。請先到網頁端「新增案件」後再執行機器人。")
                return

            for proj in projects:
                name, city = proj.get("name"), proj.get("city", "")
                logger.info(f"🔍 搜尋案件：【{city} {name}】")
                
                # Google News RSS 搜尋
                params = {"q": f'"{city}" "{name}"', "hl": "zh-TW", "gl": "TW", "ceid": "TW:zh-Hant"}
                res = self.session.get("https://news.google.com/rss/search", params=params, timeout=10)
                soup = BeautifulSoup(res.content, 'xml')
                items = soup.find_all('item')[:3]

                for item in items:
                    title, link = item.title.text, item.link.text
                    if name[:2] in title:
                        # 避免重複抓取
                        if any(h.get("sourceUrl") == link for h in proj.get("history", [])): continue

                        # 使用 AI 進行摘要
                        note = f"抓取到新聞：{title[:15]}..."
                        if self.ai_model:
                            try:
                                prompt = f"摘要新聞『{title}』與土地開發案『{city}{name}』的具體進度關係，15字內。若無關請回 False。"
                                response = self.ai_model.generate_content(prompt)
                                if "False" in response.text: continue
                                note = response.text.strip()
                            except: pass

                        logger.info(f"🚨 發現動態：{note}")
                        # 寫入待審核區
                        rec_id = str(datetime.now().timestamp())
                        user_ref.collection('pending_updates').document(rec_id).set({
                            "id": rec_id, "projectId": proj.get("id"), "projectName": name,
                            "date": datetime.now().strftime("%Y.%m.%d"),
                            "note": f"【AI查核】{note}", "source": "網路新聞",
                            "sourceUrl": link, "createdAt": firestore.SERVER_TIMESTAMP
                        })
                        break 
        except Exception as e:
            logger.error(f"❌ 查核執行失敗: {e}")

if __name__ == "__main__":
    crawler = LandDevCrawler()
    crawler.crawl_and_update()
