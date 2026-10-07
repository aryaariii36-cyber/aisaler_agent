"""
گراف اصلی ایجنت. طراحی:

- ورودی متنی -> ابتدا تشخیص نیت (buy/ask/general).
- اگر کاربر وسط فرآیند خرید باشه (session state ست شده)، مستقیم میره سراغ buy_node
  صرف‌نظر از نیت تشخیص داده‌شده (چون در حال تکمیل یک مکالمه‌ی چندمرحله‌ایه).
- تنها جایی که دکمه (Inline Keyboard) نمایش داده میشه: لیست محصولات، و تایید نهایی.
- بقیه‌ی مکالمه (سوال، گپ عمومی، حتی مذاکره/توضیح درباره محصولات) توسط
  chat_node و با زبان طبیعی از طریق مدل انجام میشه.
- آپلود عکس فیش واریزی -> payment_node (خارج از گراف اصلی کلاسه‌بندی میشه،
  چون نوع پیام «عکس» است نه متن).

نکته: تمام IO دیتابیس مستقیماً داخل نودها انجام میشه (asyncpg async-safe است).
ارسال واقعی پیام به تلگرام و فراخوانی OCR/فوروارد به ادمین در handlers.py
انجام میشه؛ گراف فقط "چه پیامی و چه دکمه‌ای" رو تصمیم می‌گیره.
"""

import base64
from typing import Annotated, Any, Optional, TypedDict

from openai import AsyncOpenAI
from langchain_core.messages import HumanMessage, SystemMessage
from langgraph.graph import END, START, StateGraph
from pydantic import BaseModel, Field
from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from . import db, timeutils
from .invoice import build_invoice
from .llm_client import chat_llm, classifier_llm
from .config import (
    GAPGPT_API_KEY,
    STORE_BANK_NAME,
    STORE_CARD_HOLDER,
    STORE_CARD_NUMBER,
    STORE_NAME,
)

import re
import random


# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------

class GraphState(TypedDict, total=False):
    chat_id: int
    input_type: str            # "text" | "callback" | "text_during_flow"
    text: Optional[str]
    callback_data: Optional[str]

    session_state: Optional[str]
    selected_category_id: Optional[int]
    selected_gender: Optional[str]
    selected_product_id: Optional[int]
    selected_variant_id: Optional[int]
    pending_order_id: Optional[int]
    customer_name: Optional[str]
    customer_address: Optional[str]

    intent: Optional[str]
    history: list[dict]

    reply_text: Optional[str]
    reply_markup: Optional[InlineKeyboardMarkup]

    # instructions back to handlers.py for things the graph can't do itself
    new_session_state: Optional[str]
    new_selected_category_id: Optional[int]
    new_selected_gender: Optional[str]
    new_selected_product_id: Optional[int]
    new_selected_variant_id: Optional[int]
    new_pending_order_id: Optional[int]
    new_customer_name: Optional[str]
    new_customer_address: Optional[str]
    # پیام‌های بعدی که handlers.py بعد از reply_text (به‌ترتیب) ارسال می‌کنه
    followup_texts: list[str]
    reset_session: bool


# ---------------------------------------------------------------------------
# Intent detection
# ---------------------------------------------------------------------------

class DetectIntent(BaseModel):
    intent: str = Field(
        description=(
            "نیت کاربر در یک مکالمه با فروشگاه پوشاک آنلاین رو دقیقاً به یکی از سه "
            "دسته زیر تشخیص بده:\n\n"

            "**buy** — کاربر می‌خواد وارد فرآیند خرید بشه یا لیست محصولات رو ببینه. "
            "شامل هر جمله‌ای که نشون بده کاربر قصد اقدام عملی برای خریدن داره، حتی اگه "
            "هنوز محصول خاصی رو مشخص نکرده باشه. مثال‌ها:\n"
            "  - «میخوام خرید کنم» / «می‌خوام بخرم»\n"
            "  - «محصولاتتون رو نشون بده» / «چی دارید؟» / «لیست کالاها رو بفرست»\n"
            "  - «یه پیراهن میخوام» / «دنبال شلوار می‌گردم» (وقتی هدف پیدا کردن و خریدنه)\n"
            "  - «بریم خرید» / «شروع کنیم» (وقتی از context معلومه منظور خریده)\n\n"

            "**ask** — کاربر سوالی می‌پرسه که نیازمند اطلاعاته، نه اقدام فوری برای خرید. "
            "شامل پرسیدن درباره قیمت، موجودی، سایز، رنگ، جنس، زمان ارسال، شرایط مرجوعی، "
            "یا مقایسه‌ی محصولات - حتی اگه غیرمستقیم به خرید مربوط باشه. مثال‌ها:\n"
            "  - «این پیراهن چند مدل رنگ داره؟»\n"
            "  - «قیمت شلوار جین چقدره؟»\n"
            "  - «ارسال به شهرستان چطوریه؟»\n"
            "  - «سایزبندیتون تا چند هست؟»\n"
            "  - «این با اون کدوم بهتره؟»\n\n"

            "**general** — هر چیزی که در دسته‌های بالا جا نمی‌گیره: سلام و احوالپرسی، "
            "تشکر، گپ عمومی، شکایت، تعارف، یا هر پیامی که نه قصد خرید مشخصی داره و نه "
            "سوال اطلاعاتی درباره‌ی محصول/فروشگاهه. مثال‌ها:\n"
            "  - «سلام» / «خوبی؟» / «ممنون»\n"
            "  - «شما بات هستید؟»\n"
            "  - «چه خبر»\n\n"

            "**tracking** — کاربر می‌خواد وضعیت یا محل سفارشی که قبلاً ثبت کرده رو "
            "پیگیری کنه. شامل هر جمله‌ای درباره‌ی وضعیت فعلی، زمان تحویل، یا رسیدن "
            "سفارشی که قبلاً ثبت شده - نه سفارش جدید. مثال‌ها:\n"
            "  - «سفارشم کجاست؟» / «سفارشم به کجا رسید؟»\n"
            "  - «پیگیری سفارش» / «وضعیت سفارشم چیه؟»\n"
            "  - «چقدر دیگه میرسه؟» / «کی دستم میرسه؟»\n\n"

            "نکات مهم:\n"
            "1. اگه پیام هم بوی سوال می‌ده و هم بوی خرید (مثلاً «این چنده، می‌خوامش»)، "
            "چون کاربر صراحتاً قصد خریدن داره، **buy** رو انتخاب کن؛ قصد نهایی مهم‌تر از "
            "شکل جمله‌ست.\n"
            "2. اگه فقط سوال قیمتیه بدون بیان قصد خرید («این چنده؟»)، **ask** بده.\n"
            "3. جمله‌های کوتاه و مبهم رو با توجه به فعل اصلی جمله تشخیص بده، نه فقط "
            "کلمات کلیدی؛ مثلاً «نمی‌خوام بخرم، فقط می‌پرسم» باید **ask** باشه نه buy.\n"
            "4. اگه کاربر درباره‌ی سفارشی صحبت می‌کنه که قبلاً ثبتش کرده (نه سفارش "
            "جدید)، حتی اگه لحنش شبیه سوال باشه، **tracking** رو انتخاب کن نه ask.\n"
            "5. همیشه دقیقاً یکی از چهار مقدار buy / ask / general / tracking رو "
            "برگردون، بدون توضیح اضافه."
        )
    )
    
#-----------------------------------------------
class ReceiptInfo(BaseModel):
    source_card_number: str = Field(description="شماره کارت مبدأ، دقیقاً همان‌طور که در تصویر چاپ شده (اگر بخشی با ستاره ماسک شده، همان ستاره‌ها را نگه دار؛ فقط خط تیره و فاصله را حذف کن)")
    sender_name: str = Field(description="نام کامل صاحب کارت مبدأ، دقیقاً مطابق تصویر")
    destination_card_number: str = Field(description="شماره کارت مقصد، دقیقاً همان‌طور که در تصویر چاپ شده (اگر بخشی با ستاره ماسک شده، همان ستاره‌ها را نگه دار؛ فقط خط تیره و فاصله را حذف کن)")
    recipient_name: str = Field(description="نام کامل صاحب کارت مقصد، دقیقاً مطابق تصویر")
    bank_name: str = Field(description="نام بانک مقصد")
    amount: int = Field(description="مبلغ تراکنش به ریال، فقط عدد (بدون کاما و واحد)")
    fee: int = Field(description="کارمزد تراکنش به ریال، فقط عدد (بدون کاما و واحد)")
    transaction_date: str = Field(description="تاریخ به فرمت YYYY/MM/DD")
    transaction_time: str = Field(description="زمان به فرمت HH:MM:SS")
    transaction_description: str = Field(description="توضیحات تراکنش")
    tracking_number: str = Field(description="شماره پیگیری تراکنش")
    
    
EXPECTED_RECEIPT = ReceiptInfo(
    source_card_number="",  # خالی بذار - از فیش میاد
    sender_name="",          # خالی بذار - از فیش میاد
    destination_card_number=STORE_CARD_NUMBER,  # from env
    recipient_name=STORE_CARD_HOLDER,  # from env
    bank_name=STORE_BANK_NAME,  # from env
    amount=0,  # filled per order in handlers.py
    fee=0,
    transaction_date="",
    transaction_time="",
    transaction_description="",
    tracking_number="",
)
    

class ReceiptCheck(BaseModel):
    destination_card_match: bool = Field(
        description="True if the destination card number matches the store's card"
    )
    recipient_name_match: bool = Field(
        description="True if the recipient name matches the store card holder (minor differences OK)"
    )
    bank_match: bool = Field(
        description="True if the bank name matches the store's bank"
    )
    amount_match: bool = Field(
        description="True if the amount matches the order price exactly"
    )
    all_critical_ok: bool = Field(
        description="True if destination card, amount, and recipient name all match"
    )
    approved: bool = Field(description="True if the receipt is approved")
    reason: str = Field(description="Short explanation in Persian")
#-----------------------------------------------

def masked_card_match(extracted_number: str, expected_number: str) -> bool:
    """
    مقایسه‌ی قطعی (بدون دخالت مدل) بین شماره کارت استخراج‌شده از فیش
    (که ممکنه بعضی ارقامش با * ماسک شده باشن) و شماره کارت واقعی فروشگاه.

    قانون: باید تعداد ارقام برابر باشه، و در هر موقعیتی که رقم استخراج‌شده
    ستاره نیست، باید دقیقاً با رقم متناظر در شماره واقعی یکی باشه.
    اگه همه‌ی ارقام ماسک شده باشن (یعنی هیچ رقم قابل‌مقایسه‌ای نمونده)،
    این تابع مقدار False برمی‌گردونه، چون نمی‌شه کارت رو تایید کرد.
    """
    clean_extracted = re.sub(r"[\s\-]", "", extracted_number or "")
    clean_expected = re.sub(r"[\s\-]", "", expected_number or "")

    if not clean_extracted or not clean_expected:
        return False
    if len(clean_extracted) != len(clean_expected):
        return False

    compared_any_digit = False
    for ch_extracted, ch_expected in zip(clean_extracted, clean_expected):
        if ch_extracted in ("*", "x", "X"):
            continue
        compared_any_digit = True
        if ch_extracted != ch_expected:
            return False

    return compared_any_digit

async def detect_intent(state: GraphState) -> GraphState:
    if state.get("input_type") != "text" or not state.get("text"):
        return {"intent": None}

    history = state.get("history") or []
    last_assistant_msg = next(
        (turn["content"] for turn in reversed(history) if turn["role"] != "user"),
        None,
    )

    context_note = ""
    if last_assistant_msg:
        context_note = (
            "\n\nبرای کمک به تشخیص، آخرین پیامی که خود فروشگاه به کاربر داده رو هم می‌بینی:\n"
            f"[آخرین پیام فروشگاه]: {last_assistant_msg}\n"
            "اگه پیام کاربر یک پاسخ کوتاه (مثل یک عدد، اسم سایز/رنگ، یا «بله»/«آره»/«تایید») "
            "باشه و از متن آخرین پیام فروشگاه معلومه که ادامه‌ی یک تصمیم خرید (انتخاب محصول، "
            "سایز، رنگ، یا تایید نهایی سفارش) است، آن را **buy** در نظر بگیر."
        )

    messages = [
        SystemMessage(content=(
            "تو دستیار طبقه‌بندی نیت کاربر برای یک فروشگاه پوشاک آنلاین روی تلگرام هستی. "
            "بر اساس معیارهایی که در schema خروجی توضیح داده شده، نیت پیام کاربر رو تشخیص بده "
            "و فقط ساختار خروجی مشخص‌شده رو برگردون." + context_note
        )),
        HumanMessage(content=state["text"]),
    ]
    model = classifier_llm.with_structured_output(DetectIntent)
    result = await model.ainvoke(messages)
    return {"intent": result.intent}


# ---------------------------------------------------------------------------
# Routing
# ---------------------------------------------------------------------------

def route(state: GraphState) -> str:
    if state.get("input_type") == "callback":
        return "buy"
    if state.get("session_state"):
        # کاربر وسط یک فرآیند خرید چندمرحله‌ایه؛ حتی اگه نیت این پیام tracking
        # تشخیص داده بشه، باید همون فرآیند خرید ادامه پیدا کنه.
        return "buy"
    if state.get("intent") == "buy":
        return "buy"
    if state.get("intent") == "tracking":
        return "track"
    return "chat"


# ---------------------------------------------------------------------------
# Buy flow (deterministic - reliability matters more than creativity here)
# ---------------------------------------------------------------------------

async def extract_receipt_info(
    image_bytes: bytes,
    api_key=GAPGPT_API_KEY,
    base_url="https://api.gapgpt.app/v1",
    model="gpt-4o",
) -> "ReceiptInfo":
    """
    اطلاعات فیش واریزی رو مستقیماً از روی عکس (بدون OCR جداگانه) با یک مدل
    چندوجهی (vision) استخراج می‌کنه. طبق نیاز پروژه، این تنها جایی است که
    باید از gpt-4o استفاده شود؛ بقیه‌ی فراخوانی‌های مدل روی gpt-4o-mini بمونن.

    نکته‌ی مهم: از AsyncOpenAI استفاده می‌کنیم (نه کلاینت sync). این تابع
    async def است و روی همون event loop تکی و مشترک بین همه‌ی کاربرهای
    بات اجرا می‌شه؛ اگه از کلاینت sync استفاده می‌شد، هر فراخوانی مدل
    (که چند ثانیه طول می‌کشه) کل event loop رو برای همه‌ی کاربرهای دیگه هم
    بلاک می‌کرد و بات عملاً سریال جواب می‌داد، نه موازی.
    """
    client = AsyncOpenAI(api_key=api_key, base_url=base_url)
    b64_image = base64.b64encode(image_bytes).decode("utf-8")

    fields_description = "\n".join(
        f"- {name}: {field.description}" for name, field in ReceiptInfo.model_fields.items()
    )

    prompt = f"""تو یک دستیار استخراج اطلاعات از عکس فیش واریز بانکی هستی.
از روی عکس زیر، دقیقاً همین فیلدها رو استخراج کن:

{fields_description}

اگر مقداری در عکس واضح نبود، برای رشته‌ها "" و برای عددها 0 بگذار.
فقط یک JSON معتبر و منطبق با همین فیلدها برگردون، بدون هیچ توضیح اضافه."""

    completion = await client.chat.completions.create(
        model=model,
        messages=[
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt},
                    {
                        "type": "image_url",
                        "image_url": {"url": f"data:image/jpeg;base64,{b64_image}"},
                    },
                ],
            }
        ],
        temperature=0,
        max_tokens=1000,
        response_format={"type": "json_object"},
    )

    raw = completion.choices[0].message.content.strip()
    if raw.startswith("```"):
        raw = re.sub(r"^```(?:json)?\s*", "", raw)
        raw = re.sub(r"\s*```$", "", raw)

    return ReceiptInfo.model_validate_json(raw)


async def check_receipt(
    extracted,
    expected,
    api_key,
    base_url="https://api.gapgpt.app/v1",
    model="gpt-4o-mini",
):
    # همون دلیل extract_receipt_info: کلاینت async تا event loop مشترک
    # بین کاربرها بلاک نشه.
    client = AsyncOpenAI(api_key=api_key, base_url=base_url)

    schema_fields = "\n".join(
        f"- {name} ({'boolean' if field.annotation is bool else 'string'}): {field.description}"
        for name, field in ReceiptCheck.model_fields.items()
    )

    prompt = f"""You are a bank receipt validator.

Compare these two data sets field by field.

EXTRACTED:
{extracted.model_dump_json(indent=2)}

EXPECTED:
{expected.model_dump_json(indent=2)}

Rules:
- The destination_card_number in EXTRACTED may contain '*' characters where the
  receipt image masks digits. Treat any '*' position as unknown/don't-care.
- Names can have small spelling differences (recipient_name_match).
- Bank name can be written in slightly different forms (bank_match), e.g. "پاسارگاد" vs "بانک پاسارگاد".
- tracking_number is NOT a criterion at all: it is random per transaction and is
  only stored for record-keeping, never compared against EXPECTED. Ignore it
  completely — do not fail anything because of it, and do not mention it in "reason".
- destination_card_match and amount_match will be recalculated deterministically
  by the calling code afterwards and your values for them will be discarded, so
  just make a best-effort guess for them.
- Judge only recipient_name_match, bank_match and write a short Persian "reason"
  describing anything unusual you notice (or that everything looks fine).

Return a JSON object that contains EXACTLY these fields, all of them, with no field missing:
{schema_fields}"""

    completion = await client.chat.completions.create(
        model=model,
        messages=[{"role": "user", "content": prompt}],
        temperature=0,
        max_tokens=1000,
        response_format={"type": "json_object"},
    )

    raw = completion.choices[0].message.content.strip()
    if raw.startswith("```"):
        raw = re.sub(r"^```(?:json)?\s*", "", raw)
        raw = re.sub(r"\s*```$", "", raw)

    result = ReceiptCheck.model_validate_json(raw)

    # --- تصمیم‌های حیاتی و قابل محاسبه، همیشه قطعی و در پایتون گرفته می‌شن؛
    # به مدل زبانی فقط برای مقایسه‌های فازی (اسم، بانک) و متن دلیل اعتماد می‌کنیم.
    # شماره پیگیری اصلاً معیار تایید نیست، فقط ذخیره می‌شه.
    deterministic_card_match = masked_card_match(
        extracted.destination_card_number,
        expected.destination_card_number,
    )
    deterministic_amount_match = extracted.amount == expected.amount

    result.destination_card_match = deterministic_card_match
    result.amount_match = deterministic_amount_match
    result.all_critical_ok = deterministic_card_match and deterministic_amount_match
    result.approved = (
        result.all_critical_ok
        and result.recipient_name_match
        and result.bank_match
    )

    if not result.all_critical_ok:
        if not deterministic_card_match and not deterministic_amount_match:
            result.reason = "شماره کارت مقصد و مبلغ واریزی با سفارش مطابقت ندارند."
        elif not deterministic_card_match:
            result.reason = "شماره کارت مقصد با شماره کارت فروشگاه مطابقت ندارد."
        elif not deterministic_amount_match:
            result.reason = (
                f"مبلغ واریزی ({extracted.amount:,} ریال) با مبلغ سفارش "
                f"({expected.amount:,} ریال) مطابقت ندارد."
            )
    elif not result.approved:
        if not result.recipient_name_match:
            result.reason = "نام گیرنده با صاحب کارت فروشگاه مطابقت ندارد."
        elif not result.bank_match:
            result.reason = "نام بانک مقصد مطابقت ندارد."

    return result

# جنسیتی که None (بدون تفکیک/یکسان) رو در callback_data نشون میده،
# چون خودِ None رو نمیشه توی رشته‌ی callback فرستاد.
_GENDER_NONE_SENTINEL = "__none__"
_GENDER_LABELS = {
    "مردانه": "👔 مردانه",
    "زنانه": "👗 زنانه",
}


def _gender_button_label(gender: Optional[str]) -> str:
    if gender is None:
        return "🔘 یکسان"
    return _GENDER_LABELS.get(gender, gender)


def _products_keyboard(products: list[dict]) -> InlineKeyboardMarkup:
    keyboard = [
        [InlineKeyboardButton(p["name"], callback_data=f"product_{p['id']}")]
        for p in products
    ]
    keyboard.append([InlineKeyboardButton("↩️ بازگشت به دسته‌بندی‌ها", callback_data="back_to_categories")])
    return InlineKeyboardMarkup(keyboard)


async def _show_products_for_category(category_id: int, gender: Optional[str], any_gender: bool) -> GraphState:
    """محصولات یک دسته‌بندی (با فیلتر جنسیت یا بدون آن) رو نشون می‌ده."""
    products = await db.list_products_for_category(category_id, gender=gender, any_gender=any_gender)
    if not products:
        return {
            "reply_text": "متاسفانه فعلاً محصولی با این مشخصات نداریم 🙁 یه دسته‌بندی دیگه رو امتحان کن.",
            "reply_markup": None,
            "reset_session": True,
        }
    return {
        "reply_text": "این‌ها محصولات موجود در این بخش هستن، کدومش رو می‌پسندی؟ 👇",
        "reply_markup": _products_keyboard(products),
        "new_session_state": "awaiting_product_choice",
        "new_selected_gender": gender if not any_gender else None,
    }


#-------------------------------------------
def _clean_text(text: Optional[str]) -> str:
    return re.sub(r"[ \t]+", " ", (text or "").strip())


def _validate_customer_name(text: Optional[str]) -> Optional[str]:
    """اگه نام معتبره برمی‌گردونه، وگرنه None."""
    name = _clean_text(text).replace("\n", " ")
    if not (3 <= len(name) <= 60):
        return None
    if re.search(r"\d", name):
        return None
    if len(re.findall(r"[^\W\d_]+", name)) < 2:  # حداقل اسم + فامیل
        return None
    return name


def _validate_customer_address(text: Optional[str]) -> Optional[str]:
    address = _clean_text(text)
    if not (10 <= len(address) <= 300):
        return None
    if len(re.findall(r"[^\W\d_]", address)) < 5:  # فقط عدد/علامت نباشه
        return None
    return address


def _format_card_number(number: str) -> str:
    digits = re.sub(r"\D", "", number)
    return "-".join(digits[i:i + 4] for i in range(0, len(digits), 4))


async def buy_node(state: GraphState) -> GraphState:
    session_state = state.get("session_state")
    input_type = state.get("input_type")

    # "بازگشت به دسته‌بندی‌ها" از هر مرحله‌ای قابل استفاده‌ست
    if input_type == "callback" and state.get("callback_data") == "back_to_categories":
        session_state = None

    # ---- شروع تازه: نمایش دسته‌بندی‌ها ----
    if session_state is None:
        categories = await db.list_categories_with_products()
        if not categories:
            return {
                "reply_text": "متاسفانه در حال حاضر محصولی برای فروش نداریم؛ به‌زودی سر بزن 🙏",
                "reply_markup": None,
                "reset_session": True,
            }

        keyboard = [
            [InlineKeyboardButton(c["name"], callback_data=f"cat_{c['id']}")]
            for c in categories
        ]
        return {
            "reply_text": "چه نوع لباسی مد نظرته؟ یکی از دسته‌بندی‌های زیر رو انتخاب کن 👇",
            "reply_markup": InlineKeyboardMarkup(keyboard),
            "new_session_state": "awaiting_category_choice",
            "new_selected_category_id": None,
            "new_selected_gender": None,
        }

    # ---- منتظر انتخاب دسته‌بندی ----
    if session_state == "awaiting_category_choice":
        if input_type == "callback" and state["callback_data"].startswith("cat_"):
            category_id = int(state["callback_data"].split("_", 1)[1])
            category = await db.get_category(category_id)
            if not category:
                return {
                    "reply_text": "این دسته‌بندی پیدا نشد 🙁 یکی دیگه رو انتخاب کن.",
                    "reply_markup": None,
                }

            genders = await db.list_genders_for_category(category_id)

            # اگر این دسته‌بندی فقط یک نوع (یا بدون تفکیک جنسیت) داره،
            # نیازی به پرسیدن نیست؛ مستقیم محصولات رو نشون بده.
            if len(genders) <= 1:
                result = await _show_products_for_category(category_id, gender=None, any_gender=True)
                result["new_selected_category_id"] = category_id
                return result

            keyboard = [
                [InlineKeyboardButton(
                    _gender_button_label(g),
                    callback_data=f"gender_{_GENDER_NONE_SENTINEL if g is None else g}",
                )]
                for g in genders
            ]
            keyboard.append([InlineKeyboardButton("↩️ بازگشت به دسته‌بندی‌ها", callback_data="back_to_categories")])
            return {
                "reply_text": f"«{category['name']}» رو انتخاب کردی. برای چه کسی می‌خوای؟ 👇",
                "reply_markup": InlineKeyboardMarkup(keyboard),
                "new_session_state": "awaiting_gender_choice",
                "new_selected_category_id": category_id,
            }

        return {
            "reply_text": "لطفاً از بین دکمه‌های بالا یکی رو انتخاب کن 🙏 (یا با /cancel می‌تونی لغو کنی)",
            "reply_markup": None,
        }

    # ---- منتظر انتخاب جنسیت ----
    if session_state == "awaiting_gender_choice":
        if input_type == "callback" and state["callback_data"].startswith("gender_"):
            raw_gender = state["callback_data"][len("gender_"):]
            gender = None if raw_gender == _GENDER_NONE_SENTINEL else raw_gender
            category_id = state.get("selected_category_id")

            if not category_id:
                return {
                    "reply_text": "یه مشکلی پیش اومد، از اول شروع می‌کنیم. برای دیدن محصولات بگو «می‌خوام بخرم».",
                    "reply_markup": None,
                    "reset_session": True,
                }

            return await _show_products_for_category(category_id, gender=gender, any_gender=False)

        return {
            "reply_text": "لطفاً از بین دکمه‌های بالا یکی رو انتخاب کن 🙏 (یا با /cancel می‌تونی لغو کنی)",
            "reply_markup": None,
        }

    # ---- منتظر انتخاب محصول ----
    if session_state == "awaiting_product_choice":
        if input_type == "callback" and state["callback_data"].startswith("product_"):
            product_id = int(state["callback_data"].split("_", 1)[1])
            product = await db.get_product(product_id)
            variants = await db.list_variants_for_product(product_id)
            in_stock = [v for v in variants if v["stock"] > 0]

            if not product or not in_stock:
                return {
                    "reply_text": "این محصول فعلاً ناموجوده 🙁 یکی دیگه رو انتخاب کن.",
                    "reply_markup": None,
                }

            keyboard = [
                [InlineKeyboardButton(
                    f"سایز {v['size']} - {v['color']} ({int(v['price_override'] or product['base_price']):,} تومان)",
                    callback_data=f"variant_{v['id']}",
                )]
                for v in in_stock
            ]
            return {
                "reply_text": f"«{product['name']}» رو انتخاب کردی. حالا سایز و رنگ رو مشخص کن:",
                "reply_markup": InlineKeyboardMarkup(keyboard),
                "new_session_state": "awaiting_variant_choice",
                "new_selected_product_id": product_id,
            }

        # کاربر پیام متنی فرستاده به‌جای زدن دکمه
        return {
            "reply_text": "لطفاً از بین دکمه‌های بالا یکی رو انتخاب کن 🙏 (یا با /cancel می‌تونی لغو کنی)",
            "reply_markup": None,
        }

    # ---- منتظر انتخاب سایز/رنگ ----
    if session_state == "awaiting_variant_choice":
        if input_type == "callback" and state["callback_data"].startswith("variant_"):
            variant_id = int(state["callback_data"].split("_", 1)[1])
            variant = await db.get_variant(variant_id)
            product = await db.get_product(state["selected_product_id"]) if state.get("selected_product_id") else None

            if not variant or not product or variant["stock"] <= 0:
                return {
                    "reply_text": "این گزینه فعلاً موجود نیست 🙁 دوباره از اول امتحان کن.",
                    "reply_markup": None,
                    "reset_session": True,
                }

            price = int(variant["price_override"] or product["base_price"])
            keyboard = InlineKeyboardMarkup([[
                InlineKeyboardButton("✅ بله، نهایی کن", callback_data="confirm_yes"),
                InlineKeyboardButton("❌ نه، بی‌خیال", callback_data="confirm_no"),
            ]])
            return {
                "reply_text": (
                    f"«{product['name']}» / سایز {variant['size']} / {variant['color']}\n"
                    f"قیمت: {price:,} تومان\n\nتایید می‌کنی؟"
                ),
                "reply_markup": keyboard,
                "new_session_state": "confirming_order",
                "new_selected_variant_id": variant_id,
            }

        return {
            "reply_text": "لطفاً از بین دکمه‌های بالا سایز/رنگ رو انتخاب کن 🙏",
            "reply_markup": None,
        }

    # ---- تایید نهایی ----
    if session_state == "confirming_order":
        if input_type == "callback" and state["callback_data"] == "confirm_yes":
            variant = await db.get_variant(state["selected_variant_id"])
            product = await db.get_product(state["selected_product_id"])

            if not variant or not product or variant["stock"] <= 0:
                return {
                    "reply_text": "متاسفانه این کالا همین الان تموم شد 🙁 دوباره از لیست انتخاب کن.",
                    "reply_markup": None,
                    "reset_session": True,
                }

            # قبل از ثبت سفارش و ارسال شماره کارت، اول مشخصات خریدار رو می‌گیریم.
            return {
                "reply_text": (
                    "عالیه! 🙌 برای صدور فاکتور چند تا اطلاعات لازم دارم.\n\n"
                    "اول نام و نام خانوادگی‌ت رو بنویس:"
                ),
                "reply_markup": None,
                "new_session_state": "awaiting_customer_name",
            }

        if input_type == "callback" and state["callback_data"] == "confirm_no":
            return {
                "reply_text": "باشه، لغو شد. هر وقت خواستی دوباره بگو تا محصولات رو نشونت بدم 🙂",
                "reply_markup": None,
                "reset_session": True,
            }

        return {
            "reply_text": "لطفاً یکی از دو دکمه بالا رو بزن 🙏",
            "reply_markup": None,
        }

    # ---- منتظر نام و نام خانوادگی ----
    if session_state == "awaiting_customer_name":
        if input_type == "text":
            name = _validate_customer_name(state.get("text"))
            if name:
                return {
                    "reply_text": f"ممنون {name} 🌸\nحالا آدرس کامل پستی‌ت رو بنویس (شهر، خیابان، پلاک، واحد، کدپستی):",
                    "reply_markup": None,
                    "new_session_state": "awaiting_customer_address",
                    "new_customer_name": name,
                }
            return {
                "reply_text": "لطفاً نام و نام خانوادگی‌ت رو کامل و فقط با حروف بنویس (مثلاً: علی رضایی) 🙏",
                "reply_markup": None,
            }
        return {
            "reply_text": "لطفاً نام و نام خانوادگی‌ت رو به‌صورت پیام متنی بنویس 🙏 (یا با /cancel لغو کن)",
            "reply_markup": None,
        }

    # ---- منتظر آدرس -> ثبت سفارش + فاکتور + شماره کارت ----
    if session_state == "awaiting_customer_address":
        if input_type != "text":
            return {
                "reply_text": "لطفاً آدرس رو به‌صورت پیام متنی بنویس 🙏 (یا با /cancel لغو کن)",
                "reply_markup": None,
            }

        address = _validate_customer_address(state.get("text"))
        if not address:
            return {
                "reply_text": "آدرس کامل نیست 🙏 لطفاً شهر، خیابان، پلاک و واحد رو هم بنویس (حداقل ۱۰ حرف).",
                "reply_markup": None,
            }

        variant = await db.get_variant(state["selected_variant_id"]) if state.get("selected_variant_id") else None
        product = await db.get_product(state["selected_product_id"]) if state.get("selected_product_id") else None
        customer_name = state.get("customer_name")

        if not variant or not product or not customer_name or variant["stock"] <= 0:
            return {
                "reply_text": "متاسفانه این کالا همین الان تموم شد یا اطلاعات ناقص موند 🙁 لطفاً دوباره از اول شروع کن: «می‌خوام بخرم»",
                "reply_markup": None,
                "reset_session": True,
            }

        order_id = await db.create_order(
            state["chat_id"], variant, product,
            customer_name=customer_name, customer_address=address,
        )
        order = await db.get_order(order_id)
        invoice_text = await build_invoice(order, stage="pending")

        card_text = (
            "لطفاً مبلغ فاکتور رو به شماره کارت زیر واریز کن و سپس عکس فیش پرداخت رو همین‌جا بفرست 👇\n\n"
            f"💰 مبلغ: {int(order['unit_price']):,} تومان\n"
            f"💳 {_format_card_number(EXPECTED_RECEIPT.destination_card_number)}\n"
            f"به نام: {EXPECTED_RECEIPT.recipient_name}\n"
            f"بانک: {EXPECTED_RECEIPT.bank_name}"
        )
        return {
            "reply_text": invoice_text,
            "followup_texts": [card_text],
            "reply_markup": None,
            "new_session_state": "awaiting_payment_slip",
            "new_pending_order_id": order_id,
            "new_customer_address": address,
        }

    # ---- منتظر عکس فیش (اگر متن فرستاد نه عکس) ----
    if session_state == "awaiting_payment_slip":
        return {
            "reply_text": "منتظر عکس فیش پرداخت هستم؛ لطفاً عکسش رو همین‌جا بفرست 🙏",
            "reply_markup": None,
        }

    # حالت ناشناخته -> ریست امن
    return {
        "reply_text": "یه مشکلی پیش اومد، از اول شروع می‌کنیم. برای دیدن محصولات بگو «می‌خوام بخرم».",
        "reply_markup": None,
        "reset_session": True,
    }


# ---------------------------------------------------------------------------
# Order tracking
# ---------------------------------------------------------------------------

_STATUS_LABELS = {
    "awaiting_payment": "در انتظار پرداخت شما",
    "rejected": "پرداختش تایید نشده (فیش نامعتبر بوده یا مغایرت داشته)",
    "cancelled": "لغو شده",
    "paid_verified": "پرداخت تایید شده و در حال آماده‌سازی/ارسال",
}


def _random_delivery_note(status: str) -> str:
    """
    یه تخمین رندوم برای وضعیت تحویل تولید می‌کنه. چون فعلاً هیچ سیستم واقعی
    پیگیری مرسوله (پست/تیپاکس و ...) به این پروژه وصل نیست، این فقط یه
    تخمین نمایشیه، نه داده‌ی واقعی از باربری.
    """
    if status != "paid_verified":
        return ""
    if random.random() < 0.35:
        return "\n📦 وضعیت ارسال: تحویل داده شده ✅"
    days = random.randint(1, 5)
    return f"\n📦 وضعیت ارسال: حدود {days} روز دیگه دستتون می‌رسه 🚚"


async def track_node(state: GraphState) -> GraphState:
    chat_id = state["chat_id"]
    orders = await db.list_orders_for_chat(chat_id, limit=1)

    if not orders:
        return {
            "reply_text": "شما تا الان سفارشی ثبت نکردید. برای شروع خرید بگو «می‌خوام بخرم» 🙂",
            "reply_markup": None,
        }

    order = orders[0]
    status_label = _STATUS_LABELS.get(order["status"], order["status"])
    delivery_note = _random_delivery_note(order["status"])
    created_at = timeutils.to_tehran(order["created_at"]).strftime("%Y/%m/%d %H:%M")

    reply = (
        f"📦 آخرین سفارش شما (#{order['id']}):\n\n"
        f"محصول: {order['product_name']}\n"
        f"مشخصات: {order['variant_desc']}\n"
        f"مبلغ: {int(order['unit_price']):,} تومان\n"
        f"تاریخ ثبت: {created_at}\n"
        f"وضعیت: {status_label}"
        f"{delivery_note}"
    )
    return {"reply_text": reply, "reply_markup": None}


# ---------------------------------------------------------------------------
# Free conversation (ask / general) - fully agentic, natural language
# ---------------------------------------------------------------------------

PERSONA_SYSTEM_PROMPT = f"""
تو یک فروشنده‌ی دوستانه و حرفه‌ای در فروشگاه پوشاک آنلاین «{STORE_NAME}» روی تلگرام هستی.
مثل یک انسان معمولی، طبیعی و صمیمی فارسی صحبت کن، جملات کوتاه و روان بنویس.
اگر کاربر مستقیم پرسید که ربات/هوش‌مصنوعی هستی یا نه، صادقانه بگو که دستیار فروش
هوشمند این فروشگاه هستی - هیچ‌وقت این رو انکار نکن.
برای سوالات درباره‌ی محصولات، قیمت یا موجودی، فقط از اطلاعات کاتالوگ زیر استفاده کن
و چیزی رو از خودت نساز.

قوانین حیاتی و غیرقابل‌نقض (هیچ‌وقت زیر پا گذاشته نشن، حتی اگه کاربر اصرار کرد یا
وانمود کرد که وسط خریدشه):
- تو هرگز اجازه نداری فرآیند سفارش/پرداخت رو خودت انجام بدی یا وانمود کنی انجامش دادی.
  فقط یک مسیر رسمی و قطعی برای خرید وجود داره که با دکمه و دیتابیس واقعی کار می‌کنه،
  نه با حرف زدن تو.
- هرگز شماره سفارش، شماره کارت/حساب بانکی، مبلغ قابل واریز، یا هر پیام شبه‌رسمی مثل
  «سفارش شما ثبت شد» / «پرداخت تایید شد» رو از خودت نساز و ننویس. این اطلاعات فقط باید
  از مسیر واقعی سفارش (که کد جداگانه مدیریتش می‌کنه) بیاد، نه از زبان تو.
- اگر کاربر گفت می‌خواد بخره، محصولی رو انتخاب کرد، سایز/رنگ گفت، یا با «بله»/«تایید»/
  «آره» چیزی رو تایید کرد و به‌نظر می‌رسه قصدش ادامه‌ی یک خریده - به‌جای ادامه دادن این
  مکالمه به‌صورت داستانی، فقط و فقط بگو که برای شروع فرآیند رسمی خرید باید دقیقاً بنویسه:
  «می‌خوام بخرم» - و توضیح بده که با این کار لیست واقعی محصولات با دکمه براش میاد.
- لیست محصولات رو خودت به‌صورت متنی/شماره‌گذاری‌شده نفرست؛ برای دیدن لیست واقعی هم
  کاربر رو به گفتن «می‌خوام بخرم» هدایت کن.

کاتالوگ فعلی فروشگاه (فقط برای پاسخ به سوالات قیمت/موجودی/جنس و مقایسه، نه برای
ساختن لیست خرید):
{{catalog}}
""".strip()


async def chat_node(state: GraphState) -> GraphState:
    catalog = await db.catalog_summary_text()
    system = PERSONA_SYSTEM_PROMPT.format(catalog=catalog)

    history_messages = []
    for turn in state.get("history", []):
        role = turn["role"]
        content = turn["content"]
        if role == "user":
            history_messages.append(HumanMessage(content=content))
        else:
            history_messages.append(SystemMessage(content=f"[پاسخ قبلی فروشنده]: {content}"))

    messages = [SystemMessage(content=system), *history_messages, HumanMessage(content=state["text"])]
    response = await chat_llm.ainvoke(messages)
    return {"reply_text": response.content, "reply_markup": None}


# ---------------------------------------------------------------------------
# Build graph
# ---------------------------------------------------------------------------

def build_graph():
    builder = StateGraph(GraphState)
    builder.add_node("detect_intent", detect_intent)
    builder.add_node("buy", buy_node)
    builder.add_node("chat", chat_node)
    builder.add_node("track", track_node)

    builder.add_edge(START, "detect_intent")
    builder.add_conditional_edges("detect_intent", route, {"buy": "buy", "chat": "chat", "track": "track"})
    builder.add_edge("buy", END)
    builder.add_edge("chat", END)
    builder.add_edge("track", END)

    return builder.compile()


graph = build_graph()
