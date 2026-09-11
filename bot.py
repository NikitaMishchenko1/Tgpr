import io
import asyncio
import aiohttp
from pathlib import Path
from aiogram import Bot, Dispatcher, F
from aiogram.filters import CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    Message,
    CallbackQuery,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    BufferedInputFile
)

# ================= КОНФИГУРАЦИЯ =================
BOT_TOKEN = "8734513499:AAFZDaHlEjpaX6ortyReXvOsZVkILjuvvXg"
YANDEX_TOKEN = "y0__wgBEPjki3kYgZ1JILi2__sYLPDQfkenDEMWbYxyndaaGhUROe0"
ALLOWED_USERS = [659684962] [5509198477]  # Telegram ID пользователей, которым открыт доступ
ROOT_DIR = "disk:/TelegramBot"  # Базовая папка в Яндекс Диске
# ================================================

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(storage=MemoryStorage())

# Кэш для обхода ограничения Telegram на длину callback_data (64 байта)
items_cache = {}

class BotStates(StatesGroup):
    waiting_for_file = State()
    waiting_for_rename = State()
    waiting_for_folder_name = State()

def cache_item(path: str, name: str, is_dir: bool, size: int = 0) -> int:
    for item_id, item in items_cache.items():
        if item["path"] == path:
            return item_id
    item_id = len(items_cache) + 1
    items_cache[item_id] = {"path": path, "name": name, "is_dir": is_dir, "size": size}
    return item_id


# ================= РАБОТА С ЯНДЕКС ДИСКОМ =================

YANDEX_HEADERS = {"Authorization": f"OAuth {YANDEX_TOKEN}"}
API_URL = "https://cloud-api.yandex.net/v1/disk/resources"

async def yd_create_folder(path: str) -> bool:
    """Создание папки на Диске"""
    async with aiohttp.ClientSession(headers=YANDEX_HEADERS) as session:
        async with session.put(API_URL, params={"path": path}) as resp:
            return resp.status in (201, 409)

async def yd_get_contents(path: str) -> list[dict]:
    """Получение содержимого папки"""
    async with aiohttp.ClientSession(headers=YANDEX_HEADERS) as session:
        async with session.get(API_URL, params={"path": path, "limit": 100, "sort": "name"}) as resp:
            if resp.status != 200:
                return []
            data = await resp.json()
            return data.get("_embedded", {}).get("items", [])

async def yd_upload_file(path: str, file_bytes: bytes) -> bool:
    """Загрузка файла в облако"""
    async with aiohttp.ClientSession(headers=YANDEX_HEADERS) as session:
        async with session.get(f"{API_URL}/upload", params={"path": path, "overwrite": "true"}) as resp:
            if resp.status != 200:
                return False
            upload_url = (await resp.json()).get("href")

        async with session.put(upload_url, data=file_bytes) as upload_resp:
            return upload_resp.status in (201, 202)

async def yd_download_file(path: str) -> bytes | None:
    """Скачивание файла из облака в память"""
    async with aiohttp.ClientSession(headers=YANDEX_HEADERS) as session:
        async with session.get(f"{API_URL}/download", params={"path": path}) as resp:
            if resp.status != 200:
                return None
            download_url = (await resp.json()).get("href")

        async with session.get(download_url) as file_resp:
            if file_resp.status == 200:
                return await file_resp.read()
    return None

async def yd_move_rename(from_path: str, to_path: str) -> bool:
    """Переименование / перемещение"""
    async with aiohttp.ClientSession(headers=YANDEX_HEADERS) as session:
        async with session.post(f"{API_URL}/move", params={"from": from_path, "path": to_path, "overwrite": "false"}) as resp:
            return resp.status in (201, 202)

async def yd_delete_resource(path: str) -> bool:
    """Удаление объекта в Корзину"""
    async with aiohttp.ClientSession(headers=YANDEX_HEADERS) as session:
        async with session.delete(API_URL, params={"path": path, "permanently": "false"}) as resp:
            return resp.status in (202, 204)


# ================= КЛАВИАТУРЫ =================

def get_folder_keyboard(current_path: str, items: list[dict]) -> InlineKeyboardMarkup:
    buttons = []

    # Папки
    for item in items:
        if item["type"] == "dir":
            item_id = cache_item(item["path"], item["name"], True)
            buttons.append([InlineKeyboardButton(text=f"📁 {item['name']}", callback_data=f"nav_dir:{item_id}")])

    # Файлы
    for item in items:
        if item["type"] == "file":
            item_id = cache_item(item["path"], item["name"], False, item.get("size", 0))
            buttons.append([InlineKeyboardButton(text=f"📄 {item['name']}", callback_data=f"nav_file:{item_id}")])

    # Панель управления папкой
    cur_id = cache_item(current_path, current_path.split("/")[-1], True)
    buttons.append([
        InlineKeyboardButton(text="➕ Загрузить файл", callback_data=f"add_file:{cur_id}"),
        InlineKeyboardButton(text="📁 Создать папку", callback_data=f"add_folder:{cur_id}")
    ])

    # Кнопка возврата
    if current_path != ROOT_DIR:
        parent_path = current_path.rsplit("/", 1)[0]
        parent_id = cache_item(parent_path, parent_path.split("/")[-1], True)
        buttons.append([InlineKeyboardButton(text="⬅️ Назад", callback_data=f"nav_dir:{parent_id}")])

    return InlineKeyboardMarkup(inline_keyboard=buttons)

def get_file_actions_keyboard(item_id: int, parent_path: str) -> InlineKeyboardMarkup:
    parent_id = cache_item(parent_path, parent_path.split("/")[-1], True)
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="⬇️ Скачать", callback_data=f"act_download:{item_id}"),
            InlineKeyboardButton(text="✏️ Переименовать", callback_data=f"act_rename:{item_id}")
        ],
        [
            InlineKeyboardButton(text="🗑️ Удалить", callback_data=f"act_delete:{item_id}")
        ],
        [
            InlineKeyboardButton(text="⬅️ Назад в папку", callback_data=f"nav_dir:{parent_id}")
        ]
    ])

cancel_kb = InlineKeyboardMarkup(
    inline_keyboard=[[InlineKeyboardButton(text="❌ Отмена", callback_data="cancel_fsm")]]
)


# ================= ОБРАБОТЧИКИ НАВИГАЦИИ =================

@dp.message(CommandStart())
async def start_handler(message: Message, state: FSMContext):
    if message.from_user.id not in ALLOWED_USERS:
        await message.answer("⛔ Доступ к данному хранилищу запрещен.")
        return

    await state.clear()
    await yd_create_folder(ROOT_DIR)  # Убедимся, что корень существует
    items = await yd_get_contents(ROOT_DIR)
    kb = get_folder_keyboard(ROOT_DIR, items)
    await message.answer("☁️ **Файлы на Яндекс Диске:**", reply_markup=kb, parse_mode="Markdown")

@dp.callback_query(F.data.startswith("nav_dir:"))
async def navigate_dir(callback: CallbackQuery, state: FSMContext):
    if callback.from_user.id not in ALLOWED_USERS:
        return
    await state.clear()

    folder_id = int(callback.data.split(":")[1])
    folder_info = items_cache.get(folder_id, {"path": ROOT_DIR})
    path = folder_info["path"]

    items = await yd_get_contents(path)
    display_path = path.replace("disk:/", "/")

    await callback.message.edit_text(
        f"📁 **Текущий путь:** `{display_path}`",
        reply_markup=get_folder_keyboard(path, items),
        parse_mode="Markdown"
    )
    await callback.answer()

@dp.callback_query(F.data.startswith("nav_file:"))
async def show_file_menu(callback: CallbackQuery, state: FSMContext):
    if callback.from_user.id not in ALLOWED_USERS:
        return
    await state.clear()

    item_id = int(callback.data.split(":")[1])
    file_info = items_cache.get(item_id)
    if not file_info:
        await callback.answer("Файл не найден.", show_alert=True)
        return

    size_mb = file_info["size"] / (1024 * 1024)
    parent_path = file_info["path"].rsplit("/", 1)[0]

    await callback.message.edit_text(
        f"📄 **Файл:** `{file_info['name']}`\n"
        f"📊 **Размер:** `{size_mb:.2f} МБ`\n\n"
        f"Выберите действие:",
        reply_markup=get_file_actions_keyboard(item_id, parent_path),
        parse_mode="Markdown"
    )
    await callback.answer()


# ================= ОПЕРАЦИИ С ФАЙЛАМИ =================

@dp.callback_query(F.data.startswith("act_download:"))
async def download_handler(callback: CallbackQuery):
    if callback.from_user.id not in ALLOWED_USERS:
        return

    item_id = int(callback.data.split(":")[1])
    file_info = items_cache.get(item_id)

    if file_info["size"] > 49.5 * 1024 * 1024:
        await callback.answer("Файл превышает 50 МБ (лимит отправки Telegram).", show_alert=True)
        return

    await callback.answer("Скачиваю из Яндекс Диска...")
    file_bytes = await yd_download_file(file_info["path"])

    if file_bytes:
        document = BufferedInputFile(file_bytes, filename=file_info["name"])
        await callback.message.answer_document(document)
    else:
        await callback.message.answer("⚠️ Не удалось получить файл из облака.")

@dp.callback_query(F.data.startswith("act_delete:"))
async def delete_handler(callback: CallbackQuery):
    if callback.from_user.id not in ALLOWED_USERS:
        return

    item_id = int(callback.data.split(":")[1])
    file_info = items_cache.get(item_id)
    parent_path = file_info["path"].rsplit("/", 1)[0]

    await yd_delete_resource(file_info["path"])
    await callback.answer("Файл удален (в Корзину).")

    items = await yd_get_contents(parent_path)
    await callback.message.edit_text(
        f"📁 **Папка обновлена**",
        reply_markup=get_folder_keyboard(parent_path, items),
        parse_mode="Markdown"
    )

@dp.callback_query(F.data.startswith("add_file:"))
async def init_upload(callback: CallbackQuery, state: FSMContext):
    if callback.from_user.id not in ALLOWED_USERS:
        return

    folder_id = int(callback.data.split(":")[1])
    target_path = items_cache.get(folder_id, {}).get("path", ROOT_DIR)

    await state.set_state(BotStates.waiting_for_file)
    await state.update_data(target_path=target_path)

    await callback.message.edit_text(
        f"📥 Отправьте файл документом (без сжатия фото).\n"
        f"Он будет загружен в: `{target_path.replace('disk:/', '/')}`\n"
        f"*(Лимит Telegram на прием файлов ботом — 20 МБ)*",
        reply_markup=cancel_kb,
        parse_mode="Markdown"
    )
    await callback.answer()

@dp.message(BotStates.waiting_for_file, F.document)
async def process_file_upload(message: Message, state: FSMContext):
    if message.document.file_size > 20 * 1024 * 1024:
        await message.answer("⚠️ Файл больше 20 МБ. Telegram не позволяет боту его принять.", reply_markup=cancel_kb)
        return

    data = await state.get_data()
    target_path = data.get("target_path", ROOT_DIR)
    filename = Path(message.document.file_name).name
    destination = f"{target_path}/{filename}"

    load_msg = await message.answer("⏳ Загружаю в Яндекс Диск...")

    # Скачивание файла Telegram сразу в оперативную память
    stream = io.BytesIO()
    await bot.download(message.document, destination=stream)
    file_bytes = stream.getvalue()

    success = await yd_upload_file(destination, file_bytes)
    await state.clear()
    await load_msg.delete()

    if success:
        items = await yd_get_contents(target_path)
        await message.answer(
            f"✅ Файл `{filename}` сохранен!",
            reply_markup=get_folder_keyboard(target_path, items),
            parse_mode="Markdown"
        )
    else:
        await message.answer("❌ Ошибка при отправке в Яндекс Диск.")

@dp.callback_query(F.data.startswith("add_folder:"))
async def init_create_folder(callback: CallbackQuery, state: FSMContext):
    if callback.from_user.id not in ALLOWED_USERS:
        return

    folder_id = int(callback.data.split(":")[1])
    target_path = items_cache.get(folder_id, {}).get("path", ROOT_DIR)

    await state.set_state(BotStates.waiting_for_folder_name)
    await state.update_data(target_path=target_path)

    await callback.message.edit_text(
        f"📁 Введите название новой папки для каталога `{target_path.replace('disk:/', '/')}`:",
        reply_markup=cancel_kb,
        parse_mode="Markdown"
    )
    await callback.answer()

@dp.message(BotStates.waiting_for_folder_name, F.text)
async def process_create_folder(message: Message, state: FSMContext):
    folder_name = message.text.strip().replace("/", "").replace("\\", "")
    data = await state.get_data()
    target_path = data.get("target_path", ROOT_DIR)
    new_folder_path = f"{target_path}/{folder_name}"

    await yd_create_folder(new_folder_path)
    await state.clear()

    items = await yd_get_contents(target_path)
    await message.answer(
        f"✅ Папка `{folder_name}` создана!",
        reply_markup=get_folder_keyboard(target_path, items),
        parse_mode="Markdown"
    )

@dp.callback_query(F.data.startswith("act_rename:"))
async def init_rename(callback: CallbackQuery, state: FSMContext):
    if callback.from_user.id not in ALLOWED_USERS:
        return

    item_id = int(callback.data.split(":")[1])
    file_info = items_cache.get(item_id)

    await state.set_state(BotStates.waiting_for_rename)
    await state.update_data(item_id=item_id)

    await callback.message.edit_text(
        f"✏️ Введите новое имя для `{file_info['name']}` (с расширением):",
        reply_markup=cancel_kb,
        parse_mode="Markdown"
    )
    await callback.answer()

@dp.message(BotStates.waiting_for_rename, F.text)
async def process_rename(message: Message, state: FSMContext):
    data = await state.get_data()
    file_info = items_cache.get(data.get("item_id"))

    new_name = Path(message.text.strip()).name
    parent_path = file_info["path"].rsplit("/", 1)[0]
    new_path = f"{parent_path}/{new_name}"

    success = await yd_move_rename(file_info["path"], new_path)
    await state.clear()

    if success:
        items = await yd_get_contents(parent_path)
        await message.answer(
            f"✅ Переименовано в `{new_name}`",
            reply_markup=get_folder_keyboard(parent_path, items),
            parse_mode="Markdown"
        )
    else:
        await message.answer("❌ Ошибка переименования.")

@dp.callback_query(F.data == "cancel_fsm")
async def cancel_action(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    await start_handler(callback.message, state)


# ================= ЗАПУСК =================

async def main():
    print("Бот успешно запущен и подключен к Яндекс Диску.")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
