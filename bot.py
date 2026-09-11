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
YANDEX_TOKEN = "y0__wgBEPjki3kYgZ1JILi2__sYLPDQfkenDEMWbYxyndaagHUROe0"
ALLOWED_USERS = [659684962, 5509198477]
ROOT_DIR = "disk:/TelegramBot"
# ================================================

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(storage=MemoryStorage())

items_cache = {}

class BotStates(StatesGroup):
    waiting_for_file = State()
    waiting_for_replace_file = State()
    waiting_for_rename = State()
    waiting_for_folder_name = State()

def cache_item(path: str, name: str, is_dir: bool, size: int = 0) -> int:
    for item_id, item in items_cache.items():
        if item["path"] == path:
            return item_id
    item_id = len(items_cache) + 1
    items_cache[item_id] = {"path": path, "name": name, "is_dir": is_dir, "size": size}
    return item_id

async def notify_team(sender_id: int, text: str):
    """Оповещение участников команды"""
    for user_id in ALLOWED_USERS:
        if user_id != sender_id:
            try:
                await bot.send_message(user_id, text, parse_mode="HTML")
            except Exception:
                pass


# ================= РАБОТА С ЯНДЕКС ДИСКОМ =================

YANDEX_HEADERS = {"Authorization": f"OAuth {YANDEX_TOKEN}"}
API_URL = "https://cloud-api.yandex.net/v1/disk/resources"

async def yd_create_folder(path: str) -> tuple[bool, str]:
    """Создает папку и возвращает (успех, сообщение ошибки)"""
    async with aiohttp.ClientSession(headers=YANDEX_HEADERS) as session:
        async with session.put(API_URL, params={"path": path}) as resp:
            if resp.status in (201, 409):
                return True, ""
            err_text = await resp.text()
            return False, f"HTTP {resp.status}: {err_text}"

async def yd_get_contents(path: str) -> tuple[list[dict], str]:
    """Получает содержимое папки с локальной сортировкой"""
    async with aiohttp.ClientSession(headers=YANDEX_HEADERS) as session:
        # Убран параметр sort, чтобы исключить 400 Bad Request от API
        async with session.get(API_URL, params={"path": path, "limit": 100}) as resp:
            if resp.status != 200:
                err_text = await resp.text()
                return [], f"HTTP {resp.status}: {err_text}"
            data = await resp.json()
            items = data.get("_embedded", {}).get("items", [])
            # Сортировка по имени средствами Python
            sorted_items = sorted(items, key=lambda x: x.get("name", "").lower())
            return sorted_items, ""

async def yd_upload_file(path: str, file_bytes: bytes) -> tuple[bool, str]:
    """Загружает файл в облако"""
    async with aiohttp.ClientSession(headers=YANDEX_HEADERS) as session:
        async with session.get(f"{API_URL}/upload", params={"path": path, "overwrite": "true"}) as resp:
            if resp.status != 200:
                err = await resp.text()
                return False, f"Не удалось получить URL загрузки ({resp.status}): {err}"
            upload_url = (await resp.json()).get("href")

        async with session.put(upload_url, data=file_bytes) as upload_resp:
            if upload_resp.status in (201, 202):
                return True, ""
            err = await upload_resp.text()
            return False, f"Ошибка отправки данных ({upload_resp.status}): {err}"

async def yd_download_file(path: str) -> bytes | None:
    """Скачивает файл из облака"""
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
    async with aiohttp.ClientSession(headers=YANDEX_HEADERS) as session:
        async with session.post(f"{API_URL}/move", params={"from": from_path, "path": to_path, "overwrite": "false"}) as resp:
            return resp.status in (201, 202)

async def yd_delete_resource(path: str) -> bool:
    async with aiohttp.ClientSession(headers=YANDEX_HEADERS) as session:
        async with session.delete(API_URL, params={"path": path, "permanently": "false"}) as resp:
            return resp.status in (202, 204)

async def yd_publish_and_get_link(path: str) -> str | None:
    async with aiohttp.ClientSession(headers=YANDEX_HEADERS) as session:
        await session.put(f"{API_URL}/publish", params={"path": path})
        async with session.get(API_URL, params={"path": path}) as resp:
            if resp.status == 200:
                data = await resp.json()
                return data.get("public_url")
    return None


# ================= КЛАВИАТУРЫ =================

def get_folder_keyboard(current_path: str, items: list[dict]) -> InlineKeyboardMarkup:
    buttons = []

    # Папки
    for item in items:
        item_type = item.get("type") or item.get("resource_type")
        if item_type == "dir":
            item_id = cache_item(item["path"], item["name"], True)
            buttons.append([InlineKeyboardButton(text=f"📁 {item['name']}", callback_data=f"nav_dir:{item_id}")])

    # Файлы
    for item in items:
        item_type = item.get("type") or item.get("resource_type")
        if item_type == "file":
            item_id = cache_item(item["path"], item["name"], False, item.get("size", 0))
            buttons.append([InlineKeyboardButton(text=f"📄 {item['name']}", callback_data=f"nav_file:{item_id}")])

    cur_id = cache_item(current_path, current_path.split("/")[-1], True)
    buttons.append([
        InlineKeyboardButton(text="➕ Новый файл", callback_data=f"add_file:{cur_id}"),
        InlineKeyboardButton(text="📁 Создать папку", callback_data=f"add_folder:{cur_id}")
    ])

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
            InlineKeyboardButton(text="🔄 Заменить файл", callback_data=f"act_replace:{item_id}")
        ],
        [
            InlineKeyboardButton(text="🔗 Ссылка на Диск", callback_data=f"act_share:{item_id}"),
            InlineKeyboardButton(text="✏️ Переименовать", callback_data=f"act_rename:{item_id}")
        ],
        [
            InlineKeyboardButton(text="🗑️ Удалить", callback_data=f"act_delete_confirm:{item_id}")
        ],
        [
            InlineKeyboardButton(text="⬅️ Назад в папку", callback_data=f"nav_dir:{parent_id}")
        ]
    ])

def get_confirm_delete_keyboard(item_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="⚠️ Да, удалить навсегда", callback_data=f"act_delete_yes:{item_id}")
        ],
        [
            InlineKeyboardButton(text="❌ Отмена", callback_data=f"nav_file:{item_id}")
        ]
    ])

cancel_kb = InlineKeyboardMarkup(
    inline_keyboard=[[InlineKeyboardButton(text="❌ Отмена", callback_data="cancel_fsm")]]
)


# ================= НАВИГАЦИЯ =================

@dp.message(CommandStart())
async def start_handler(message: Message, state: FSMContext):
    if message.from_user.id not in ALLOWED_USERS:
        await message.answer("⛔ Доступ к данному хранилищу закрыт.")
        return

    await state.clear()
    success, err = await yd_create_folder(ROOT_DIR)
    if not success:
        await message.answer(
            f"⚠️ <b>Ошибка доступа к Яндекс Диску!</b>\n\n"
            f"Код: <code>{err}</code>\n\n"
            f"Проверьте OAuth-токен и права приложения в Яндекс ID.",
            parse_mode="HTML"
        )
        return

    items, get_err = await yd_get_contents(ROOT_DIR)
    if get_err:
        await message.answer(f"⚠️ <b>Ошибка чтения Диска:</b> <code>{get_err}</code>", parse_mode="HTML")
        return

    kb = get_folder_keyboard(ROOT_DIR, items)
    await message.answer("☁️ <b>Файлы проектов на Яндекс Диске:</b>", reply_markup=kb, parse_mode="HTML")

@dp.callback_query(F.data.startswith("nav_dir:"))
async def navigate_dir(callback: CallbackQuery, state: FSMContext):
    if callback.from_user.id not in ALLOWED_USERS:
        return
    await state.clear()

    folder_id = int(callback.data.split(":")[1])
    folder_info = items_cache.get(folder_id, {"path": ROOT_DIR})
    path = folder_info["path"]

    items, err = await yd_get_contents(path)
    if err:
        await callback.answer(f"Ошибка загрузки: {err[:40]}", show_alert=True)
        return

    display_path = path.replace("disk:/", "/")
    await callback.message.edit_text(
        f"📁 <b>Текущая папка:</b> <code>{display_path}</code>",
        reply_markup=get_folder_keyboard(path, items),
        parse_mode="HTML"
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
        f"📄 <b>Файл / Проект:</b> <code>{file_info['name']}</code>\n"
        f"📊 <b>Размер:</b> <code>{size_mb:.2f} МБ</code>\n\n"
        f"Выберите действие:",
        reply_markup=get_file_actions_keyboard(item_id, parent_path),
        parse_mode="HTML"
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
        await callback.answer("Файл больше 50 МБ. Используйте кнопку «Ссылка на Диск».", show_alert=True)
        return

    await callback.answer("Скачиваю из Яндекс Диска...")
    file_bytes = await yd_download_file(file_info["path"])

    if file_bytes:
        document = BufferedInputFile(file_bytes, filename=file_info["name"])
        await callback.message.answer_document(document)
    else:
        await callback.message.answer("⚠️ Не удалось получить файл из облака.")

@dp.callback_query(F.data.startswith("act_share:"))
async def share_link_handler(callback: CallbackQuery):
    if callback.from_user.id not in ALLOWED_USERS:
        return

    item_id = int(callback.data.split(":")[1])
    file_info = items_cache.get(item_id)

    await callback.answer("Генерирую ссылку...")
    pub_link = await yd_publish_and_get_link(file_info["path"])

    if pub_link:
        await callback.message.answer(
            f"🔗 <b>Публичная ссылка на файл:</b>\n"
            f"<code>{file_info['name']}</code>\n\n"
            f"{pub_link}",
            parse_mode="HTML"
        )
    else:
        await callback.message.answer("Не удалось получить ссылку.")

@dp.callback_query(F.data.startswith("act_replace:"))
async def init_replace_handler(callback: CallbackQuery, state: FSMContext):
    if callback.from_user.id not in ALLOWED_USERS:
        return

    item_id = int(callback.data.split(":")[1])
    file_info = items_cache.get(item_id)

    await state.set_state(BotStates.waiting_for_replace_file)
    await state.update_data(item_id=item_id, target_path=file_info["path"], file_name=file_info["name"])

    await callback.message.edit_text(
        f"🔄 <b>Замена на новую версию</b>\n\n"
        f"Вы обновляете: <code>{file_info['name']}</code>\n\n"
        f"Отправьте новый документ в чат (без сжатия).\n"
        f"<i>Старая версия будет перезаписана.</i>",
        reply_markup=cancel_kb,
        parse_mode="HTML"
    )
    await callback.answer()

@dp.message(BotStates.waiting_for_replace_file, F.document)
async def process_replace_file(message: Message, state: FSMContext):
    if message.document.file_size > 20 * 1024 * 1024:
        await message.answer("⚠️ Файл больше 20 МБ (лимит Telegram).", reply_markup=cancel_kb)
        return

    data = await state.get_data()
    target_path = data.get("target_path")
    original_name = data.get("file_name")
    parent_path = target_path.rsplit("/", 1)[0]

    load_msg = await message.answer("⏳ Обновляю проект на Яндекс Диске...")

    stream = io.BytesIO()
    await bot.download(message.document, destination=stream)
    file_bytes = stream.getvalue()

    success, err = await yd_upload_file(target_path, file_bytes)
    await state.clear()
    await load_msg.delete()

    if success:
        user_mention = f"@{message.from_user.username}" if message.from_user.username else message.from_user.full_name
        await notify_team(
            sender_id=message.from_user.id,
            text=f"📢 <b>Проект обновлен!</b>\n"
                 f"👤 Пользователь: {user_mention}\n"
                 f"📄 Файл: <code>{original_name}</code>\n"
                 f"📁 Папка: <code>{parent_path.replace('disk:/', '/')}</code>"
        )

        items, _ = await yd_get_contents(parent_path)
        await message.answer(
            f"✅ Проект <code>{original_name}</code> успешно обновлен!",
            reply_markup=get_folder_keyboard(parent_path, items),
            parse_mode="HTML"
        )
    else:
        await message.answer(f"❌ <b>Ошибка обновления:</b>\n<code>{err}</code>", parse_mode="HTML")

@dp.callback_query(F.data.startswith("act_delete_confirm:"))
async def confirm_delete_prompt(callback: CallbackQuery):
    if callback.from_user.id not in ALLOWED_USERS:
        return

    item_id = int(callback.data.split(":")[1])
    file_info = items_cache.get(item_id)

    await callback.message.edit_text(
        f"❓ <b>Подтверждение удаления</b>\n\n"
        f"Удалить проект <code>{file_info['name']}</code>?\n"
        f"<i>(Файл переместится в Корзину Диска)</i>",
        reply_markup=get_confirm_delete_keyboard(item_id),
        parse_mode="HTML"
    )
    await callback.answer()

@dp.callback_query(F.data.startswith("act_delete_yes:"))
async def delete_confirmed_handler(callback: CallbackQuery):
    if callback.from_user.id not in ALLOWED_USERS:
        return

    item_id = int(callback.data.split(":")[1])
    file_info = items_cache.get(item_id)
    parent_path = file_info["path"].rsplit("/", 1)[0]

    await yd_delete_resource(file_info["path"])
    await callback.answer("Файл удален.")

    user_mention = f"@{callback.from_user.username}" if callback.from_user.username else callback.from_user.full_name
    await notify_team(
        sender_id=callback.from_user.id,
        text=f"🗑️ <b>Файл удален</b>\n"
             f"👤 Пользователь: {user_mention}\n"
             f"📄 Файл: <code>{file_info['name']}</code>"
    )

    items, _ = await yd_get_contents(parent_path)
    await callback.message.edit_text(
        f"📁 <b>Папка обновлена</b>",
        reply_markup=get_folder_keyboard(parent_path, items),
        parse_mode="HTML"
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
        f"📥 Отправьте документ (без сжатия).\n"
        f"Он будет сохранен в: <code>{target_path.replace('disk:/', '/')}</code>",
        reply_markup=cancel_kb,
        parse_mode="HTML"
    )
    await callback.answer()

@dp.message(BotStates.waiting_for_file, F.document)
async def process_file_upload(message: Message, state: FSMContext):
    if message.document.file_size > 20 * 1024 * 1024:
        await message.answer("⚠️ Файл больше 20 МБ. Загрузите его через браузер в Яндекс Диск.", reply_markup=cancel_kb)
        return

    data = await state.get_data()
    target_path = data.get("target_path", ROOT_DIR)
    filename = Path(message.document.file_name).name
    destination = f"{target_path}/{filename}"

    load_msg = await message.answer("⏳ Сохраняю в Яндекс Диск...")

    stream = io.BytesIO()
    await bot.download(message.document, destination=stream)
    file_bytes = stream.getvalue()

    success, err = await yd_upload_file(destination, file_bytes)
    await state.clear()
    await load_msg.delete()

    if success:
        user_mention = f"@{message.from_user.username}" if message.from_user.username else message.from_user.full_name
        await notify_team(
            sender_id=message.from_user.id,
            text=f"📥 <b>Новый проект добавлен!</b>\n"
                 f"👤 Пользователь: {user_mention}\n"
                 f"📄 Файл: <code>{filename}</code>\n"
                 f"📁 Папка: <code>{target_path.replace('disk:/', '/')}</code>"
        )

        items, _ = await yd_get_contents(target_path)
        await message.answer(
            f"✅ Файл <code>{filename}</code> добавлен!",
            reply_markup=get_folder_keyboard(target_path, items),
            parse_mode="HTML"
        )
    else:
        await message.answer(f"❌ <b>Ошибка при сохранении:</b>\n<code>{err}</code>", parse_mode="HTML")

@dp.callback_query(F.data.startswith("add_folder:"))
async def init_create_folder(callback: CallbackQuery, state: FSMContext):
    if callback.from_user.id not in ALLOWED_USERS:
        return

    folder_id = int(callback.data.split(":")[1])
    target_path = items_cache.get(folder_id, {}).get("path", ROOT_DIR)

    await state.set_state(BotStates.waiting_for_folder_name)
    await state.update_data(target_path=target_path)

    await callback.message.edit_text(
        f"📁 Введите название новой папки для каталога <code>{target_path.replace('disk:/', '/')}</code>:",
        reply_markup=cancel_kb,
        parse_mode="HTML"
    )
    await callback.answer()

@dp.message(BotStates.waiting_for_folder_name, F.text)
async def process_create_folder(message: Message, state: FSMContext):
    folder_name = message.text.strip().replace("/", "").replace("\\", "")
    data = await state.get_data()
    target_path = data.get("target_path", ROOT_DIR)
    new_folder_path = f"{target_path}/{folder_name}"

    success, err_msg = await yd_create_folder(new_folder_path)
    await state.clear()

    if not success:
        await message.answer(
            f"❌ <b>Не удалось создать папку на Яндекс Диске:</b>\n<code>{err_msg}</code>",
            parse_mode="HTML"
        )
        return

    items, _ = await yd_get_contents(target_path)
    await message.answer(
        f"✅ Папка <code>{folder_name}</code> создана!",
        reply_markup=get_folder_keyboard(target_path, items),
        parse_mode="HTML"
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
        f"✏️ Введите новое имя для <code>{file_info['name']}</code> (с расширением):",
        reply_markup=cancel_kb,
        parse_mode="HTML"
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
        items, _ = await yd_get_contents(parent_path)
        await message.answer(
            f"✅ Переименовано в <code>{new_name}</code>",
            reply_markup=get_folder_keyboard(parent_path, items),
            parse_mode="HTML"
        )
    else:
        await message.answer("❌ Ошибка при переименовании.")

@dp.callback_query(F.data == "cancel_fsm")
async def cancel_action(callback: CallbackQuery, state: FSMContext):
    if callback.from_user.id not in ALLOWED_USERS:
        await callback.answer("Доступ закрыт", show_alert=True)
        return

    data = await state.get_data()
    target_path = data.get("target_path", ROOT_DIR)
    item_id = data.get("item_id")
    await state.clear()

    if item_id and item_id in items_cache:
        file_info = items_cache[item_id]
        parent_path = file_info["path"].rsplit("/", 1)[0]
        size_mb = file_info["size"] / (1024 * 1024)
        await callback.message.edit_text(
            f"📄 <b>Файл / Проект:</b> <code>{file_info['name']}</code>\n"
            f"📊 <b>Размер:</b> <code>{size_mb:.2f} МБ</code>\n\n"
            f"Выберите действие:",
            reply_markup=get_file_actions_keyboard(item_id, parent_path),
            parse_mode="HTML"
        )
    else:
        folder_path = target_path if "." not in target_path.split("/")[-1] else target_path.rsplit("/", 1)[0]
        items, _ = await yd_get_contents(folder_path)
        display_path = folder_path.replace("disk:/", "/")
        await callback.message.edit_text(
            f"📁 <b>Текущая папка:</b> <code>{display_path}</code>",
            reply_markup=get_folder_keyboard(folder_path, items),
            parse_mode="HTML"
        )

    await callback.answer("Действие отменено.")


# ================= ЗАПУСК =================

async def main():
    print("Бот успешно запущен и работает с Яндекс Диском.")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
