from telegram import InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup

BTN_SALES = "📊 گزارش فروش"
BTN_USERS = "👥 تعداد کاربران"
BTN_INVENTORY = "📦 موجودی انبار"

BTN_USER_LIST = "🔎 اطلاعات دقیق کاربران"

BTN_HISTORY = "🗂 تاریخچه چت"
BTN_RECEIPT = "🧾 فیش پرداخت و وضعیت"
BTN_PROFILE = "🧠 خصوصیات کاربر"
BTN_NO = "خیر"

MAIN_MENU = ReplyKeyboardMarkup(
    [[BTN_SALES], [BTN_USERS], [BTN_INVENTORY]],
    resize_keyboard=True,
    is_persistent=True,
)


def user_detail_menu(show_history: bool, show_receipt: bool, show_profile: bool) -> ReplyKeyboardMarkup:
    row = []
    if show_history:
        row.append(BTN_HISTORY)
    if show_receipt:
        row.append(BTN_RECEIPT)
    if show_profile:
        row.append(BTN_PROFILE)
    rows = [[b] for b in row] + [[BTN_NO]]
    return ReplyKeyboardMarkup(rows, resize_keyboard=True, is_persistent=True)


def users_page_keyboard(users: list[dict], page: int, total_pages: int) -> InlineKeyboardMarkup:
    rows = []
    for u in users:
        rows.append([InlineKeyboardButton(f"👤 کاربر {u['chat_id']}", callback_data=f"adm_user_{u['chat_id']}")])
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("◀️ قبلی", callback_data=f"adm_page_{page - 1}"))
    if page < total_pages - 1:
        nav.append(InlineKeyboardButton("بعدی ▶️", callback_data=f"adm_page_{page + 1}"))
    if nav:
        rows.append(nav)
    return InlineKeyboardMarkup(rows)


def user_list_entry_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton(BTN_USER_LIST, callback_data="adm_page_0")]])
