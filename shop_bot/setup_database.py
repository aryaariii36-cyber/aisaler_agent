"""
یک فایل، یک اجرا. این اسکریپت:
  1. از تو مشخصات دیتابیس رو می‌پرسه (یا اگر env var ست کرده باشی، همونا رو می‌خونه)
  2. تمام جدول‌های لازم رو می‌سازه
  3. چند تا محصول نمونه (با سایز/رنگ) بهش اضافه می‌کنه

فقط کافیه این رو اجرا کنی:
    pip install psycopg2-binary
    python setup_database.py

و مشخصات دیتابیس لیارا (Host, Port, Database name, Username, Password) رو
وقتی ازت پرسید وارد کن. دوباره که اجراش کنی اگر محصولی از قبل باشه چیزی
اضافه نمی‌کنه (خرابی/تکرار نمی‌سازه).
"""

import getpass
import os
import sys

import psycopg2

SCHEMA_SQL = """
CREATE SCHEMA IF NOT EXISTS shopbot;
SET search_path TO shopbot;

-- دسته‌بندی محصولات (تیشرت، شلوار، پیراهن و ...)
CREATE TABLE IF NOT EXISTS categories (
    id          SERIAL PRIMARY KEY,
    name        TEXT NOT NULL UNIQUE,
    sort_order  INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS products (
    id           SERIAL PRIMARY KEY,
    name         TEXT NOT NULL,
    description  TEXT NOT NULL DEFAULT '',
    base_price   NUMERIC(12, 0) NOT NULL,
    image_url    TEXT,
    is_active    BOOLEAN NOT NULL DEFAULT TRUE,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    category_id  INTEGER REFERENCES categories(id),
    -- 'مردانه' / 'زنانه' / ... ؛ اگر محصول جنسیتی نداره (یونیسکس)، NULL بمونه.
    gender       TEXT
);

-- برای دیتابیس‌هایی که از قبل جدول products رو بدون این ستون‌ها ساخته بودن.
ALTER TABLE products ADD COLUMN IF NOT EXISTS category_id INTEGER REFERENCES categories(id);
ALTER TABLE products ADD COLUMN IF NOT EXISTS gender TEXT;

CREATE TABLE IF NOT EXISTS product_variants (
    id             SERIAL PRIMARY KEY,
    product_id     INTEGER NOT NULL REFERENCES products(id) ON DELETE CASCADE,
    size           TEXT NOT NULL,
    color          TEXT NOT NULL,
    price_override NUMERIC(12, 0),
    stock          INTEGER NOT NULL DEFAULT 0,
    sku            TEXT UNIQUE,
    is_active      BOOLEAN NOT NULL DEFAULT TRUE,
    UNIQUE (product_id, size, color)
);

-- همه‌ی کاربرها در جدول‌های مشترک زیر نگه داشته می‌شن (نه یک PostgreSQL
-- schema جدا به‌ازای هر کاربر)؛ هر ردیف با ستون chat_id (ایندکس‌شده) به
-- کاربرش وصل میشه. این جدول‌ها به‌صورت خودکار توسط برنامه هم ساخته می‌شن
-- (app/db.py -> init_pool)، پس اجرای این اسکریپت اختیاریه؛ فقط برای این
-- که از قبلِ اولین دیپلوی هم دیتابیس آماده باشه اینجا هم ساخته می‌شن.
CREATE TABLE IF NOT EXISTS users (
    chat_id       BIGINT PRIMARY KEY,
    first_seen_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_seen_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS sessions (
    chat_id              BIGINT PRIMARY KEY,
    state                TEXT,
    intent               TEXT,
    selected_category_id INTEGER,
    selected_gender      TEXT,
    selected_product_id  INTEGER,
    selected_variant_id  INTEGER,
    pending_order_id     BIGINT,
    updated_at           TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS messages (
    id          BIGSERIAL PRIMARY KEY,
    chat_id     BIGINT NOT NULL,
    role        TEXT NOT NULL,
    content     TEXT NOT NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_shopbot_messages_chat_created ON messages (chat_id, created_at);

CREATE TABLE IF NOT EXISTS orders (
    id                   BIGSERIAL PRIMARY KEY,
    chat_id              BIGINT NOT NULL,
    variant_id           INTEGER NOT NULL,
    product_name         TEXT NOT NULL,
    variant_desc         TEXT NOT NULL,
    unit_price           NUMERIC(12, 0) NOT NULL,
    status               TEXT NOT NULL DEFAULT 'awaiting_payment',
    payment_file_id      TEXT,
    payment_image        BYTEA,
    payment_image_mime   TEXT,
    ocr_extracted_amount NUMERIC(12, 0),
    ocr_confidence       TEXT,
    tracking_number      TEXT,
    admin_chat_id        BIGINT,
    admin_message_id     BIGINT,
    created_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at           TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_shopbot_orders_chat_created ON orders (chat_id, created_at);
CREATE INDEX IF NOT EXISTS idx_shopbot_orders_status ON orders (status);

ALTER TABLE orders   ADD COLUMN IF NOT EXISTS customer_name    TEXT;
ALTER TABLE orders   ADD COLUMN IF NOT EXISTS customer_address TEXT;
ALTER TABLE sessions ADD COLUMN IF NOT EXISTS customer_name    TEXT;
ALTER TABLE sessions ADD COLUMN IF NOT EXISTS customer_address TEXT;

CREATE TABLE IF NOT EXISTS pending_reminders (
    chat_id  BIGINT PRIMARY KEY,
    due_at   TIMESTAMPTZ NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_shopbot_pending_reminders_due ON pending_reminders (due_at);

CREATE TABLE IF NOT EXISTS user_profile (
    chat_id               BIGINT PRIMARY KEY,
    profile_text          TEXT NOT NULL,
    generated_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    source_message_count  INTEGER NOT NULL DEFAULT 0,
    source_order_count    INTEGER NOT NULL DEFAULT 0
);
"""

# ترتیب نمایش دسته‌بندی‌ها به کاربر (sort_order)
SAMPLE_CATEGORIES = [
    "تیشرت",
    "پیراهن",
    "شلوار",
    "هودی",
    "ژاکت و کاپشن",
    "دامن",
    "کفش",
    "کلاه و اکسسوری",
]

# هر محصول به یکی از SAMPLE_CATEGORIES تعلق داره و "gender" مشخص می‌کنه
# مردانه/زنانه است یا None (یونیسکس/بدون تفکیک). وقتی یک دسته‌بندی فقط
# یک نوع gender داره (مثل «دامن» یا «کلاه و اکسسوری» که همه None هستن)،
# بات دیگه سوال مردانه/زنانه نمی‌پرسه و مستقیم محصولات رو نشون می‌ده.
SAMPLE_PRODUCTS = [
    # --- تیشرت ---
    {
        "category": "تیشرت", "gender": "مردانه",
        "name": "تیشرت مردانه ساده",
        "description": "تیشرت نخی یقه‌گرد، مناسب استفاده روزمره.",
        "base_price": 420_000,
        "variants": [
            {"size": "M", "color": "مشکی", "stock": 20},
            {"size": "L", "color": "مشکی", "stock": 15},
            {"size": "M", "color": "سفید", "stock": 18},
            {"size": "XL", "color": "طوسی", "stock": 10},
        ],
    },
    {
        "category": "تیشرت", "gender": "مردانه",
        "name": "تیشرت مردانه طرح‌دار",
        "description": "تیشرت نخی با چاپ گرافیکی روی سینه.",
        "base_price": 480_000,
        "variants": [
            {"size": "M", "color": "سرمه‌ای", "stock": 12},
            {"size": "L", "color": "سرمه‌ای", "stock": 9},
        ],
    },
    {
        "category": "تیشرت", "gender": "زنانه",
        "name": "تیشرت زنانه بیسیک",
        "description": "تیشرت آستین‌کوتاه نخی با برش راحت.",
        "base_price": 400_000,
        "variants": [
            {"size": "S", "color": "سفید", "stock": 16},
            {"size": "M", "color": "سفید", "stock": 16},
            {"size": "M", "color": "صورتی", "stock": 11},
        ],
    },
    {
        "category": "تیشرت", "gender": "زنانه",
        "name": "تیشرت زنانه اورسایز",
        "description": "برش گشاد و راحت، مناسب استایل روزمره.",
        "base_price": 450_000,
        "variants": [
            {"size": "S", "color": "کرم", "stock": 10},
            {"size": "M", "color": "کرم", "stock": 10},
        ],
    },
    # --- پیراهن ---
    {
        "category": "پیراهن", "gender": "مردانه",
        "name": "پیراهن اسپرت مردانه",
        "description": "پیراهن نخی آستین‌بلند، مناسب استفاده روزمره و نیمه‌رسمی.",
        "base_price": 890_000,
        "variants": [
            {"size": "M", "color": "سفید", "stock": 12},
            {"size": "L", "color": "سفید", "stock": 8},
            {"size": "M", "color": "مشکی", "stock": 5},
        ],
    },
    {
        "category": "پیراهن", "gender": "مردانه",
        "name": "پیراهن چهارخانه مردانه",
        "description": "پیراهن پشمی چهارخانه، مناسب پاییز و زمستان.",
        "base_price": 950_000,
        "variants": [
            {"size": "M", "color": "قرمز-مشکی", "stock": 7},
            {"size": "L", "color": "قرمز-مشکی", "stock": 6},
        ],
    },
    {
        "category": "پیراهن", "gender": "زنانه",
        "name": "پیراهن زنانه گلدار",
        "description": "پیراهن نخی سبک با طرح گل‌ریز، مناسب بهار و تابستان.",
        "base_price": 870_000,
        "variants": [
            {"size": "S", "color": "صورتی", "stock": 9},
            {"size": "M", "color": "صورتی", "stock": 9},
            {"size": "M", "color": "آبی", "stock": 7},
        ],
    },
    # --- شلوار ---
    {
        "category": "شلوار", "gender": "مردانه",
        "name": "شلوار جین کلاسیک مردانه",
        "description": "جین راسته با پارچه ضخیم و دوخت مقاوم.",
        "base_price": 1_250_000,
        "variants": [
            {"size": "30", "color": "آبی تیره", "stock": 10},
            {"size": "32", "color": "آبی تیره", "stock": 10},
            {"size": "34", "color": "آبی روشن", "stock": 6},
        ],
    },
    {
        "category": "شلوار", "gender": "مردانه",
        "name": "شلوار کتان مردانه",
        "description": "شلوار کتان اسلیم‌فیت، مناسب پوشش نیمه‌رسمی.",
        "base_price": 980_000,
        "variants": [
            {"size": "31", "color": "خاکی", "stock": 8},
            {"size": "33", "color": "خاکی", "stock": 8},
        ],
    },
    {
        "category": "شلوار", "gender": "زنانه",
        "name": "شلوار جین زنانه",
        "description": "جین زنانه فیت‌بادی با کمر متوسط.",
        "base_price": 1_150_000,
        "variants": [
            {"size": "28", "color": "آبی روشن", "stock": 9},
            {"size": "30", "color": "آبی روشن", "stock": 9},
        ],
    },
    {
        "category": "شلوار", "gender": "زنانه",
        "name": "شلوار لگ زنانه",
        "description": "لگ کشی نخی، مناسب استفاده روزمره و ورزش سبک.",
        "base_price": 590_000,
        "variants": [
            {"size": "S", "color": "مشکی", "stock": 20},
            {"size": "M", "color": "مشکی", "stock": 20},
        ],
    },
    # --- هودی ---
    {
        "category": "هودی", "gender": "زنانه",
        "name": "هودی زنانه",
        "description": "هودی گرم و راحت با پارچه فرنچ‌تری، مناسب پاییز و زمستان.",
        "base_price": 980_000,
        "variants": [
            {"size": "S", "color": "کرم", "stock": 15},
            {"size": "M", "color": "کرم", "stock": 15},
            {"size": "M", "color": "زرشکی", "stock": 9, "price_override": 1_050_000},
        ],
    },
    {
        "category": "هودی", "gender": "مردانه",
        "name": "هودی مردانه",
        "description": "هودی کلاه‌دار با جیب کانگورویی، پارچه ضخیم.",
        "base_price": 1_020_000,
        "variants": [
            {"size": "M", "color": "طوسی", "stock": 13},
            {"size": "L", "color": "طوسی", "stock": 11},
            {"size": "L", "color": "مشکی", "stock": 10},
        ],
    },
    # --- ژاکت و کاپشن ---
    {
        "category": "ژاکت و کاپشن", "gender": "مردانه",
        "name": "کاپشن مردانه ضدآب",
        "description": "کاپشن سبک ضدآب با آستری گرم، مناسب زمستان.",
        "base_price": 1_890_000,
        "variants": [
            {"size": "L", "color": "مشکی", "stock": 6},
            {"size": "XL", "color": "مشکی", "stock": 5},
        ],
    },
    {
        "category": "ژاکت و کاپشن", "gender": "زنانه",
        "name": "ژاکت بافت زنانه",
        "description": "ژاکت بافت سبک با یقه‌گرد، مناسب اواخر پاییز.",
        "base_price": 1_150_000,
        "variants": [
            {"size": "S", "color": "بژ", "stock": 9},
            {"size": "M", "color": "بژ", "stock": 9},
        ],
    },
    # --- دامن (فقط زنانه؛ بات دیگه سوال جنسیت نمی‌پرسه) ---
    {
        "category": "دامن", "gender": "زنانه",
        "name": "دامن پلیسه زنانه",
        "description": "دامن پلیسه بلند با پارچه سبک، مناسب استفاده روزمره.",
        "base_price": 780_000,
        "variants": [
            {"size": "S", "color": "مشکی", "stock": 8},
            {"size": "M", "color": "مشکی", "stock": 8},
        ],
    },
    # --- کفش ---
    {
        "category": "کفش", "gender": "مردانه",
        "name": "کفش اسپرت مردانه",
        "description": "کفش کتانی سبک، مناسب پیاده‌روی روزانه.",
        "base_price": 1_450_000,
        "variants": [
            {"size": "41", "color": "سفید-مشکی", "stock": 7},
            {"size": "42", "color": "سفید-مشکی", "stock": 7},
            {"size": "43", "color": "سفید-مشکی", "stock": 5},
        ],
    },
    {
        "category": "کفش", "gender": "زنانه",
        "name": "کفش اسپرت زنانه",
        "description": "کتانی سبک و راحت با زیره انعطاف‌پذیر.",
        "base_price": 1_320_000,
        "variants": [
            {"size": "37", "color": "صورتی-سفید", "stock": 8},
            {"size": "38", "color": "صورتی-سفید", "stock": 8},
        ],
    },
    # --- کلاه و اکسسوری (یونیسکس؛ بات دیگه سوال جنسیت نمی‌پرسه) ---
    {
        "category": "کلاه و اکسسوری", "gender": None,
        "name": "کلاه بافت یونیسکس",
        "description": "کلاه بافت گرم، مناسب پاییز و زمستان، سایزبندی آزاد.",
        "base_price": 280_000,
        "variants": [
            {"size": "آزاد", "color": "مشکی", "stock": 25},
            {"size": "آزاد", "color": "طوسی", "stock": 20},
        ],
    },
    {
        "category": "کلاه و اکسسوری", "gender": None,
        "name": "شال گردن یونیسکس",
        "description": "شال گردن بافت نرم، سایزبندی آزاد.",
        "base_price": 320_000,
        "variants": [
            {"size": "آزاد", "color": "طوسی", "stock": 18},
            {"size": "آزاد", "color": "قهوه‌ای", "stock": 14},
        ],
    },
]


def ask(prompt: str, env_name: str, default: str | None = None, secret: bool = False) -> str:
    """مقدار رو اول از env var می‌خونه؛ اگر نبود، از کاربر می‌پرسه."""
    existing = os.environ.get(env_name)
    if existing:
        return existing

    label = f"{prompt}" + (f" [{default}]" if default else "")
    if secret:
        value = getpass.getpass(label + ": ")
    else:
        value = input(label + ": ").strip()

    return value or default or ""


def main():
    print("=== تنظیم دیتابیس فروشگاه ===")
    print("مشخصات رو از پنل دیتابیس لیارا کپی کن.\n")

    host = ask("آدرس (Host)", "DB_HOST")
    port = ask("پورت (Port)", "DB_PORT", default="5432")
    dbname = ask("نام دیتابیس (Database name)", "DB_NAME")
    user = ask("نام کاربری (Username)", "DB_USER")
    password = ask("رمز عبور (Password)", "DB_PASSWORD", secret=True)

    print("\nدر حال اتصال به دیتابیس...")
    try:
        conn = psycopg2.connect(
            host=host, port=port, dbname=dbname, user=user, password=password
        )
    except Exception as e:
        print(f"\n❌ اتصال ناموفق بود: {e}")
        sys.exit(1)

    conn.autocommit = False
    try:
        with conn.cursor() as cur:
            print("در حال ساخت جدول‌ها...")
            cur.execute(SCHEMA_SQL)

            cur.execute("SELECT COUNT(*) FROM products;")
            (count,) = cur.fetchone()

            # دسته‌بندی‌ها همیشه idempotent ساخته/به‌روز می‌شن (بدون توجه به
            # اینکه محصولی از قبل هست یا نه)، چون فقط یک UPSERT سبکه.
            print("در حال ساخت دسته‌بندی‌ها...")
            category_ids: dict[str, int] = {}
            for i, cat_name in enumerate(SAMPLE_CATEGORIES):
                cur.execute(
                    """
                    INSERT INTO categories (name, sort_order)
                    VALUES (%s, %s)
                    ON CONFLICT (name) DO UPDATE SET sort_order = EXCLUDED.sort_order
                    RETURNING id;
                    """,
                    (cat_name, i),
                )
                (category_ids[cat_name],) = cur.fetchone()

            if count > 0:
                # محصولاتی که قبل از اضافه شدن دسته‌بندی‌ها ثبت شده بودن (بدون
                # category_id) رو به یک دسته‌بندی fallback می‌بریم تا گم نشن
                # (خودِ برنامه هم این کار رو سر هر بار بالا اومدن انجام می‌ده،
                # اینجا فقط برای اطمینان و شفافیت به کاربر این اسکریپت تکرار شده).
                cur.execute(
                    """
                    INSERT INTO categories (name, sort_order)
                    VALUES ('سایر محصولات', 999)
                    ON CONFLICT (name) DO NOTHING
                    RETURNING id;
                    """
                )
                fallback_row = cur.fetchone()
                if fallback_row is None:
                    cur.execute("SELECT id FROM categories WHERE name = 'سایر محصولات';")
                    fallback_row = cur.fetchone()
                (fallback_category_id,) = fallback_row
                cur.execute(
                    "UPDATE products SET category_id = %s WHERE category_id IS NULL;",
                    (fallback_category_id,),
                )
                if cur.rowcount:
                    print(
                        f"ℹ️  {cur.rowcount} محصول قدیمی بدون دسته‌بندی رو به دسته «سایر محصولات» منتقل کردم "
                        "(می‌تونی بعداً دستی دسته‌بندی درست‌تری براشون بذاری)."
                    )

            # محصولات نمونه‌ی متنوع (تیشرت/پیراهن/شلوار/هودی/کفش/...) رو
            # همیشه تلاش می‌کنیم اضافه کنیم - فارغ از اینکه قبلاً محصول
            # دیگه‌ای (مثل محصولات واقعی خودت) توی دیتابیس بوده یا نه.
            # برای جلوگیری از تکراری شدن، هر محصول رو فقط وقتی اضافه می‌کنه
            # که هنوز محصولی دقیقاً با همون اسم وجود نداشته باشه؛ پس اجرای
            # چندباره‌ی این اسکریپت هیچ‌وقت محصول تکراری نمی‌سازه و به
            # محصولات واقعی/دستی خودت هم دست نمی‌زنه.
            print("در حال اضافه کردن محصولات نمونه (در صورت نبودن)...")
            added = 0
            for p in SAMPLE_PRODUCTS:
                cur.execute("SELECT id FROM products WHERE name = %s;", (p["name"],))
                if cur.fetchone():
                    continue  # این محصول از قبل هست (اجرای قبلی همین اسکریپت یا محصول واقعی با همین اسم)

                cur.execute(
                    """
                    INSERT INTO products (name, description, base_price, category_id, gender)
                    VALUES (%s, %s, %s, %s, %s)
                    RETURNING id;
                    """,
                    (
                        p["name"],
                        p["description"],
                        p["base_price"],
                        category_ids[p["category"]],
                        p["gender"],
                    ),
                )
                (product_id,) = cur.fetchone()
                added += 1

                for v in p["variants"]:
                    sku = f"P{product_id}-{v['size']}-{v['color']}".replace(" ", "")
                    cur.execute(
                        """
                        INSERT INTO product_variants
                            (product_id, size, color, price_override, stock, sku)
                        VALUES (%s, %s, %s, %s, %s, %s)
                        ON CONFLICT (product_id, size, color) DO NOTHING;
                        """,
                        (
                            product_id,
                            v["size"],
                            v["color"],
                            v.get("price_override"),
                            v["stock"],
                            sku,
                        ),
                    )

            if added:
                print(f"✅ {added} محصول نمونه‌ی جدید اضافه شد.")
            else:
                print("✅ همه‌ی محصولات نمونه از قبل موجود بودن، چیزی اضافه نشد.")

        conn.commit()
        print("\n🎉 دیتابیس آماده‌ست. حالا می‌تونی بات رو دیپلوی کنی.")
    except Exception as e:
        conn.rollback()
        print(f"\n❌ یه خطا پیش اومد، هیچ تغییری ذخیره نشد: {e}")
        sys.exit(1)
    finally:
        conn.close()


if __name__ == "__main__":
    main()
