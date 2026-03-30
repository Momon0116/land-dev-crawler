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

        # 🔑 讀取環境變數
        self.firebase_cred_raw = os.environ.get("FIREBASE_CREDENTIALS")
        self.target_user_id = os.environ.get("FIREBASE_UID")
        self.gemini_api_key = os.environ.get("GEMINI_API_KEY")

        # 診斷日誌：確認變數是否傳入（不顯示敏感內容）
        logger.info("--- 系統啟動診斷 ---")
        logger.info(f"Firebase Credentials: {'✅ 已讀取' if self.firebase_cred_raw else '❌ 缺失'}")
        logger.info(f"Firebase UID: {'✅ 已讀取' if self.target_user_id else '❌ 缺失'}")
        logger.info(f"Gemini API Key: {'✅ 已讀取' if self.gemini_api_key else '❌ 缺失'}")
        logger.info("--------------------")

        self._initialize_services()

    def _initialize_services(self):
        try:
            # 1. 初始化 Firebase 連線
            if self.firebase_cred_raw:
                # ☁️ 雲端模式：強化 JSON 解析邏輯
                try:
                    # 處理 GitHub Secrets 可能產生的格式異常（例如單引號、多餘空格）
                    cred_content = self.firebase_cred_raw.strip()
                    # 如果內容被不當包裝在額外引號內，進行修復
                    if cred_content.startswith('"') and cred_content.endswith('"'):
                        cred_content = json.loads(cred_content)
                    
                    cred_dict = json.loads(cred_content)
                    cred = credentials.Certificate(cred_dict)
                    logger.info("☁️ 雲端 Secret 解析成功")
                except json.JSONDecodeError as je:
                    logger.error(f"❌ JSON 格式損壞，請檢查 Secret 內容是否完整：{je}")
                    sys.exit(1)
            else:
                # 💻 本機模式
                local_path = r"C:\Users\User\work-report\Python\land-dev-dashboard-firebase-adminsdk-fbsvc-5811c0deb7.json"
                if os.path.exists(local_path):
                    logger.info(f"💻 採用本機測試模式，載入金鑰檔案：{local_path}")
                    cred = credentials.Certificate(local_path)
                    # 本機手動填入測試 UID (選填)
                    if not self.target_user_id:
                        self.target_user_id = "UDlQYBAOPsZlGSSxFddidzzDPMk2" # 範例 UID
                else:
                    logger.error("❌ 嚴重錯誤：找不到金鑰來源（Secret 或 JSON 檔）")
                    sys.exit(1)

            if not firebase_admin._apps:
                firebase_admin.initialize_app(cred)
            self.db = firestore.client()
            logger.info("✅ Firebase 連線成功")

            # 2. 初始化 Gemini AI
            if self.gemini_api_key:
                genai.configure(api_key=self.gemini_api_key)
                self.ai_model = genai.GenerativeModel('gemini-2.5-flash')
                logger.info("🧠 AI 語意分析模組已就緒")
            else:
                logger.warning("⚠️ 缺少 Gemini API Key，將使用基礎匹配模式")

        except Exception as e:
            logger.error(f"🔥 初始化失敗：{e}")
            sys.exit(1)

    def _build_session(self):
        session = requests.Session()
        retry_strategy = Retry(total=3, backoff_factor=1, status_forcelist=[429, 500, 502, 503, 504])
        session.mount("https://", HTTPAdapter(max_retries=retry_strategy))
        session.headers.update({"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"})
        return session

    def crawl_and_update(self):
        if not self.db or not self.target_user_id:
            logger.error("❌ 無法執行：資料庫連線或 UID 未設定")
            return

        try:
            # 讀取案件清單
            user_ref = self.db.collection('artifacts').document(self.app_id).collection('users').document(self.target_user_id)
            projects_ref = user_ref.collection('projects')
            docs = projects_ref.stream()
            
            projects = []
            for d in docs:
                p_data = d.to_dict()
                if not p_data.get("isArchived", False):
                    projects.append(p_data)
            
            logger.info(f"📥 成功載入 {len(projects)} 筆進行中的案件。")
            
            if not projects:
                logger.warning("📭 目前無列管中的案件，任務結束。")
                return

            for proj in projects:
                p_id = proj.get("id")
                name = proj.get("name")
                city = proj.get("city", "")
                logger.info(f"🔍 查核案件：【{city} {name}】")
                
                # RSS 搜尋
                params = {"q": f'"{city}" "{name}"', "hl": "zh-TW", "gl": "TW", "ceid": "TW:zh-Hant"}
                res = self.session.get("https://news.google.com/rss/search", params=params, timeout=10)
                soup = BeautifulSoup(res.content, 'xml')
                items = soup.find_all('item')[:3]

                for item in items:
                    title, link = item.title.text, item.link.text
                    # 簡易過濾與重複檢查
                    if name[:2] in title:
                        history = proj.get("history", [])
                        if any(h.get("sourceUrl") == link for h in history):
                            continue

                        # AI 摘要
                        ai_note = f"發現新動態：{title[:20]}..."
                        if self.ai_model:
                            try:
                                prompt = f"請摘要新聞『{title}』與土地開發案『{city}{name}』的關係，15字以內。若無關請回 False。"
                                response = self.ai_model.generate_content(prompt)
                                if "False" in response.text: continue
                                ai_note = response.text.strip()
                            except: pass

                        logger.info(f"  🚨 偵測到動態：{ai_note}")
                        
                        # 寫入待審核區 (pending_updates)
                        record_id = str(datetime.now().timestamp())
                        new_data = {
                            "id": record_id,
                            "projectId": p_id,
                            "projectName": name,
                            "date": datetime.now().strftime("%Y.%m.%d"),
                            "note": f"【AI查核】{ai_note}",
                            "source": "網路新聞資訊",
                            "sourceUrl": link,
                            "createdAt": firestore.SERVER_TIMESTAMP
                        }
                        user_ref.collection('pending_updates').document(record_id).set(new_data)
                        break 

        except Exception as e:
            logger.error(f"❌ 執行過程發生錯誤：{e}")

if __name__ == "__main__":
    crawler = LandDevCrawler()
    crawler.crawl_and_update()
