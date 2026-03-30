import requests
from bs4 import BeautifulSoup
import logging
from datetime import datetime
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
import email.utils
import os
import json

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
        
        # 🔑 【雲端安全升級】優先讀取雲端環境變數，若無則讀取本機金鑰檔案
        firebase_cred_json = os.environ.get("FIREBASE_CREDENTIALS")
        if firebase_cred_json:
            cred_dict = json.loads(firebase_cred_json)
            cred = credentials.Certificate(cred_dict)
            logger.info("☁️ 偵測到雲端金鑰，採用雲端環境變數啟動。")
        else:
            self.cred_path = r"C:\Users\User\work-report\Python\land-dev-dashboard-firebase-adminsdk-fbsvc-5811c0deb7.json" # <--- 記得確認這行金鑰路徑正確
            cred = credentials.Certificate(self.cred_path)
            logger.info("💻 採用本機金鑰檔案啟動。")
        
        self.app_id = "land-dev-app"
        self.target_user_id = os.environ.get("FIREBASE_UID") or "UD1QYBA0PsZlGSSxFddidzZDPMk2" # <--- 記得填入 UID
        self.gemini_api_key = os.environ.get("GEMINI_API_KEY") or "AIzaSyCAQLETzOJuU9Rfega0CuI-NffBC-Cp0cI" # <--- 記得填入 Gemini API Key
        
        # 初始化 Firebase
        try:
            if not firebase_admin._apps:
                firebase_admin.initialize_app(cred)
            self.db = firestore.client()
            logger.info("✅ 成功連線至 Firebase 雲端資料庫！")
        except Exception as e:
            logger.error(f"❌ Firebase 連線失敗，請檢查金鑰設定: {e}")
            exit(1)
            
        # 初始化 Gemini AI 大腦
        try:
            genai.configure(api_key=self.gemini_api_key)
            self.ai_model = genai.GenerativeModel('gemini-2.5-flash')
            logger.info("🧠 AI 語意分析大腦 (Gemini) 初始化完成！")
        except Exception as e:
            logger.error(f"❌ Gemini AI 初始化失敗: {e}")

    def _build_session(self):
        session = requests.Session()
        retry_strategy = Retry(total=3, backoff_factor=1)
        adapter = HTTPAdapter(max_retries=retry_strategy)
        session.mount("http://", adapter)
        session.mount("https://", adapter)
        session.headers.update({"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"})
        return session

    def fetch_projects_from_db(self):
        """從雲端資料庫動態抓取需要追蹤的案件清單"""
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
                        "status": data.get("status"),
                        "history": data.get("history", [])
                    })
            logger.info(f"📥 從資料庫載入 {len(tracked_projects)} 筆進行中的案件。")
            return tracked_projects
        except Exception as e:
            logger.error(f"從資料庫讀取專案失敗: {e}")
            return []

    def analyze_with_ai(self, city, proj_name, title):
        """使用 LLM 判斷新聞相關性並生成摘要"""
        prompt = f"""
        你是一個專業的公部門土地開發專案追蹤助理。
        請判斷以下新聞標題，是否與「{city}{proj_name}」的土地開發、都市計畫或工程進度有【直接關聯】。

        新聞標題：{title}

        注意事項：
        1. 很多新聞可能只是剛好提到該地名（例如：在該重劃區附近發生車禍、舉辦跨年晚會、選舉造勢），這些都屬於【無關】。
        2. 必須是實質的專案進度（例如：審查通過、會議召開、抗議陳情、動工、核定、計畫變更等）才算【有關】。

        請嚴格判斷並回覆：
        如果【無關】：請只回覆 "False" 這五個字母。
        如果【有關】：請用一句話（大約15到20字以內）專業、客觀地摘要這個最新動態。
        """
        try:
            response = self.ai_model.generate_content(prompt)
            text = response.text.strip()
            
            if "False" in text or "false" in text or text == "":
                return None
            return text
        except Exception as e:
            logger.error(f"⚠️ AI 分析發生錯誤: {e}")
            return None

    def crawl_and_update(self):
        projects = self.fetch_projects_from_db()
        
        for proj in projects:
            proj_id = proj["id"]
            proj_name = proj["name"]
            city = proj["city"]
            current_history = proj["history"]
            
            logger.info(f"🔍 搜尋中：【{city} - {proj_name}】...")
            
            params = {
                "q": f'"{city}" "{proj_name}"',
                "hl": "zh-TW",
                "gl": "TW",
                "ceid": "TW:zh-Hant"
            }

            try:
                response = self.session.get("https://news.google.com/rss/search", params=params, timeout=10)
                response.raise_for_status()
                soup = BeautifulSoup(response.content, 'xml')
                items = soup.find_all('item')

                if not items:
                    continue

                for item in items:
                    title = item.title.text
                    link = item.link.text
                    pub_date_str = item.pubDate.text
                    
                    city_short = city.replace("市", "").replace("縣", "")
                    core_name = proj_name[:2]
                    
                    if city_short in title and core_name in title:
                        is_duplicate = any(h.get('sourceUrl') == link or title in h.get('note', '') for h in current_history)
                        
                        if not is_duplicate:
                            logger.info(f"  👉 命中基礎關鍵字，交由 AI 判斷：{title}")
                            
                            ai_summary = self.analyze_with_ai(city, proj_name, title)
                            
                            if ai_summary:
                                parsed_date = email.utils.parsedate_tz(pub_date_str)
                                if parsed_date:
                                    dt = datetime.fromtimestamp(email.utils.mktime_tz(parsed_date))
                                    formatted_date = f"{dt.year - 1911}.{dt.strftime('%m.%d')}"
                                else:
                                    formatted_date = "114.01.01"

                                logger.info(f"  🚨 確認為有效進度！準備送入審核區... -> {ai_summary}")
                                
                                record_id = str(datetime.now().timestamp())
                                new_record = {
                                    "id": record_id,
                                    "projectId": proj_id,
                                    "projectName": proj_name,
                                    "date": formatted_date,
                                    "note": f"【AI 判讀最新動態】{ai_summary}",
                                    "source": "網路公開資訊/新聞稿",
                                    "sourceUrl": link,
                                    "phaseIndex": -1,
                                    "status": proj["status"],
                                    "createdAt": firestore.SERVER_TIMESTAMP
                                }
                                
                                pending_ref = self.db.collection('artifacts').document(self.app_id).collection('users').document(self.target_user_id).collection('pending_updates').document(record_id)
                                pending_ref.set(new_record)
                                break 
                            else:
                                logger.info(f"  🛑 雜訊過濾：AI 判斷此新聞與專案實質進度無關。")

            except Exception as e:
                logger.error(f"❌ 搜尋【{proj_name}】發生錯誤: {e}")

if __name__ == "__main__":
    logger.info("="*60)
    logger.info(" 啟動土地開發專案：AI 背景查核推播機器人 🤖")
    logger.info("="*60)
    crawler = LandDevCrawler()
    crawler.crawl_and_update()
    logger.info("="*60)
    logger.info(" 任務執行完畢，所有更新已進入待審核區。")
    logger.info("="*60)
