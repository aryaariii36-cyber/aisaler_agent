import os

try:  # optional: load a local .env file (ignored on PaaS where real env vars are set)
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass


def _require(name: str) -> str:
    val = os.environ.get(name)
    if not val:
        raise RuntimeError(f"Environment variable {name} is required but not set.")
    return val


# --- Telegram (بات پنل ادمین - جداگانه از بات اصلی) ----------------------
TELEGRAM_BOT_TOKEN = _require("TELEGRAM_BOT_TOKEN")

# Public HTTPS URL که PaaS بهت میده، مثلاً https://admin-yourbot.liara.run
WEBHOOK_BASE_URL = _require("WEBHOOK_BASE_URL")
WEBHOOK_PATH = os.environ.get("WEBHOOK_PATH", "/telegram/webhook")
WEBHOOK_SECRET = _require("WEBHOOK_SECRET_TOKEN")
PORT = int(os.environ.get("PORT", "8080"))

# فقط این chat_id اجازه‌ی استفاده از پنل ادمین رو داره.
ADMIN_CHAT_ID = int(_require("ADMIN_CHAT_ID"))

# --- PostgreSQL (همون دیتابیس بات اصلی - فقط خوندن، به‌جز کش پروفایل) ----
DB_HOST = _require("DB_HOST")
DB_PORT = int(os.environ.get("DB_PORT", "5432"))
DB_NAME = _require("DB_NAME")
DB_USER = _require("DB_USER")
DB_PASSWORD = _require("DB_PASSWORD")

# اندازه‌ی connection pool. این بات فقط یک ادمین رو سرویس می‌ده، پس نیاز به
# pool بزرگی نداره؛ ولی مثل بات اصلی از env var قابل‌تنظیمه تا اگه چند
# نمونه ازش هم بالا بیاد، محدودیت‌های ثابت کد باعث bottleneck نشه.
DB_POOL_MIN_SIZE = int(os.environ.get("DB_POOL_MIN_SIZE", "1"))
DB_POOL_MAX_SIZE = int(os.environ.get("DB_POOL_MAX_SIZE", "5"))

# --- تنظیمات نمایش -------------------------------------------------------
USERS_PAGE_SIZE = 8          # تعداد کاربر در هر صفحه از لیست کاربران
SALES_HISTORY_LIMIT = 300    # حداکثر تعداد سفارش فروخته‌شده که در گزارش می‌آید
MESSAGES_PAGE_LIMIT = 1000   # حداکثر تعداد پیام در PDF تاریخچه چت

# --- LLM provider (GapGPT - همون پروکسی OpenAI-compatible بات اصلی) ------
# برای ایجنت تحلیل «خصوصیات کاربر» (app/profile_agent.py) استفاده میشه.
GAPGPT_API_KEY = _require("GAPGPT_API_KEY")
GAPGPT_BASE_URL = os.environ.get("GAPGPT_BASE_URL", "https://api.gapgpt.app/v1")
GAPGPT_MODEL = os.environ.get("GAPGPT_MODEL", "gpt-4o-mini")

# حداکثر تعداد پیامی که برای تحلیل خصوصیات کاربر به مدل داده می‌شه (برای
# جلوگیری از prompt خیلی بزرگ در کاربرهایی با تاریخچه‌ی خیلی طولانی).
PROFILE_MESSAGE_LIMIT = 500
