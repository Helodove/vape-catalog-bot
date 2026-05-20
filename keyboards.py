from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from moysklad.models import ProductFolder, Product, Store

PAGE_SIZE_FOLDERS = 10
PAGE_SIZE_PRODUCTS = 8


def main_menu_keyboard(store_name: str | None = None) -> InlineKeyboardMarkup:
    store_label = f"📍 {store_name}" if store_name else "📍 Все точки"
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(store_label, callback_data="store:list")],
        [InlineKeyboardButton("📂 Каталог", callback_data="catalog:root:0")],
        [InlineKeyboardButton("🔍 Поиск", callback_data="search:start")],
    ])


def stores_keyboard(stores: list[Store], current_id: str | None) -> InlineKeyboardMarkup:
    rows = []
    for s in stores:
        mark = "✅ " if s.id == current_id else ""
        # store_id и href — оба нужны, но href длинный, передаём только id
        rows.append([InlineKeyboardButton(
            f"{mark}{s.name}",
            callback_data=f"store_pick:{s.id}",
        )])
    rows.append([InlineKeyboardButton("🌐 Все точки", callback_data="store_pick:all")])
    rows.append([InlineKeyboardButton("🏠 В начало", callback_data="home")])
    return InlineKeyboardMarkup(rows)


def folders_keyboard(
    folders: list[ProductFolder],
    page: int,
    parent_cb: str,
    back_cb: str | None,
) -> InlineKeyboardMarkup:
    start = page * PAGE_SIZE_FOLDERS
    chunk = folders[start: start + PAGE_SIZE_FOLDERS]
    rows = []
    for f in chunk:
        rows.append([InlineKeyboardButton(
            f"📁 {f.name}",
            callback_data=f"folder:{f.id}:{page}",
        )])
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("◀", callback_data=f"{parent_cb}:{page - 1}"))
    if start + PAGE_SIZE_FOLDERS < len(folders):
        nav.append(InlineKeyboardButton("▶", callback_data=f"{parent_cb}:{page + 1}"))
    if nav:
        rows.append(nav)
    bottom = []
    if back_cb:
        bottom.append(InlineKeyboardButton("← Назад", callback_data=back_cb))
    bottom.append(InlineKeyboardButton("🏠 В начало", callback_data="home"))
    rows.append(bottom)
    return InlineKeyboardMarkup(rows)


def products_keyboard(
    products: list[Product],
    page: int,
    folder_id: str,
    only_in_stock: bool,
    back_cb: str,
) -> InlineKeyboardMarkup:
    visible = [p for p in products if (not only_in_stock or p.in_stock)]
    start = page * PAGE_SIZE_PRODUCTS
    chunk = visible[start: start + PAGE_SIZE_PRODUCTS]
    rows = []
    for p in chunk:
        icon = "🟢" if p.in_stock else "🔴"
        rows.append([InlineKeyboardButton(
            f"{icon} {p.name}",
            callback_data=f"product:{p.id}:{page}:{int(only_in_stock)}",
        )])
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton(
            "◀",
            callback_data=f"plist:{folder_id}:{page - 1}:{int(only_in_stock)}",
        ))
    if start + PAGE_SIZE_PRODUCTS < len(visible):
        nav.append(InlineKeyboardButton(
            "▶",
            callback_data=f"plist:{folder_id}:{page + 1}:{int(only_in_stock)}",
        ))
    if nav:
        rows.append(nav)
    filter_label = "Только в наличии 🟢" if not only_in_stock else "Все товары"
    rows.append([InlineKeyboardButton(
        filter_label,
        callback_data=f"plist:{folder_id}:0:{int(not only_in_stock)}",
    )])
    rows.append([
        InlineKeyboardButton("← Назад", callback_data=back_cb),
        InlineKeyboardButton("🏠 В начало", callback_data="home"),
    ])
    return InlineKeyboardMarkup(rows)


def product_back_keyboard(back_cb: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("← Назад к списку", callback_data=back_cb),
        InlineKeyboardButton("🏠 В начало", callback_data="home"),
    ]])


def search_results_keyboard(
    products: list[Product],
    page: int,
    only_in_stock: bool,
) -> InlineKeyboardMarkup:
    visible = [p for p in products if (not only_in_stock or p.in_stock)]
    start = page * PAGE_SIZE_PRODUCTS
    chunk = visible[start: start + PAGE_SIZE_PRODUCTS]
    rows = []
    for p in chunk:
        icon = "🟢" if p.in_stock else "🔴"
        rows.append([InlineKeyboardButton(
            f"{icon} {p.name}",
            callback_data=f"sproduct:{p.id}:{page}:{int(only_in_stock)}",
        )])
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton(
            "◀",
            callback_data=f"slist:{page - 1}:{int(only_in_stock)}",
        ))
    if start + PAGE_SIZE_PRODUCTS < len(visible):
        nav.append(InlineKeyboardButton(
            "▶",
            callback_data=f"slist:{page + 1}:{int(only_in_stock)}",
        ))
    if nav:
        rows.append(nav)
    filter_label = "Только в наличии 🟢" if not only_in_stock else "Все товары"
    rows.append([InlineKeyboardButton(
        filter_label,
        callback_data=f"slist:0:{int(not only_in_stock)}",
    )])
    rows.append([InlineKeyboardButton("🏠 В начало", callback_data="home")])
    return InlineKeyboardMarkup(rows)
