import os
import re
import json
import time
import shutil
import random
import socket
import zipfile
import urllib.request
import threading
import subprocess
import telebot
from telebot import types
from playwright.sync_api import sync_playwright
from tiktok_captcha_solver import PlaywrightSolver
import g4f
from g4f.client import Client

try:
    from playwright_stealth import Stealth
    HAS_STEALTH_CLASS = True
except ImportError:
    HAS_STEALTH_CLASS = False

# ==========================================
# НАСТРОЙКИ (ключи берём из переменных окружения!)
#   export BOT_TOKEN="новый_токен_от_BotFather"
#   export SADCAPTCHA_API_KEY="новый_ключ"
# ==========================================
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
SADCAPTCHA_API_KEY = os.getenv("SADCAPTCHA_API_KEY", "").strip()
if not BOT_TOKEN:
    raise SystemExit("Не задан BOT_TOKEN. Выполни: export BOT_TOKEN='твой_токен'")

USERS_DIR = os.path.join(os.getcwd(), "users")
os.makedirs(USERS_DIR, exist_ok=True)

TIKTOK_HOST = "www.tiktok.com"
VNC_DISPLAY = ":99"
# ==========================================

bot = telebot.TeleBot(BOT_TOKEN)

user_states = {}       # user_id -> "wait_faq" | "wait_extra" | None
active_workers = {}    # user_id -> {"stop": bool, "extra_event": Event, "extra_count": int}
login_locks = threading.Lock()


def get_user_paths(user_id):
    base = os.path.join(USERS_DIR, str(user_id))
    profiles_dir = os.path.join(base, "profiles")
    screens_dir = os.path.join(base, "screenshots")
    os.makedirs(profiles_dir, exist_ok=True)
    os.makedirs(screens_dir, exist_ok=True)
    return {
        "base": base,
        "profiles": profiles_dir,
        "screens": screens_dir,
        "faq": os.path.join(base, "faq.txt"),
        "meta": os.path.join(base, "accounts.json"),
    }


def load_accounts(user_id):
    meta_path = get_user_paths(user_id)["meta"]
    if os.path.exists(meta_path):
        try:
            with open(meta_path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {}


def save_accounts(user_id, data):
    meta_path = get_user_paths(user_id)["meta"]
    with open(meta_path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


# ==========================================
# ПАРСЕР ТЗ
# ==========================================
def parse_faq_text(raw_text):
    direct_comment = None
    m = re.search(
        r"^(?:Текст\s*комментария|Текст|Комментарий)\s*[:\-–—]\s*(.+)$",
        raw_text,
        re.IGNORECASE | re.MULTILINE,
    )
    if m:
        direct_comment = m.group(1).strip()

    if not direct_comment:
        raise ValueError(
            "В тексте ТЗ не найдена обязательная строка `Текст: ...` или `Комментарий: ...`!"
        )

    prompt = f"""
Ты — ИИ-аналитик техзаданий. Проанализируй текст ТЗ и верни СТРОГО чистый JSON-объект без разметки markdown (без ```json).
Хештеги в списке hashtags пиши БЕЗ символа # (например: ["vpn", "впн"]).
Также извлеки ключевые слова/названия ботов или сервисов из комментария в список keywords (в нижнем регистре).

Текст ТЗ:
\"\"\"
{raw_text}
\"\"\"

Обязательная структура JSON:
{{
  "task_name": "Краткое_название_задания",
  "hashtags": ["хештег1", "хештег2"],
  "target_screens": 20,
  "max_per_video": 4,
  "need_like": true,
  "keywords": ["название_бота_в_нижнем_регистре"]
}}
"""
    try:
        client = Client()
        response = client.chat.completions.create(
            model=g4f.models.gpt_4o_mini,
            messages=[{"role": "user", "content": prompt}],
        )
        raw_response = response.choices[0].message.content.strip()
        json_match = re.search(r"\{.*\}", raw_response, re.DOTALL)
        clean_json = json_match.group(0) if json_match else raw_response
        cfg = json.loads(clean_json)
        if cfg.get("hashtags"):
            cfg["hashtags"] = [h.lstrip("#").strip() for h in cfg["hashtags"] if h.lstrip("#").strip()]
    except Exception:
        lines = [line.strip() for line in raw_text.splitlines() if line.strip()]
        raw_title = lines[0] if lines else "Task_TT"
        task_name = re.sub(r'[\\/*?:"<>|]', "", raw_title).strip()[:40] or "Task_TT"
        hashtags = [h.lstrip("#") for h in re.findall(r"#([a-zA-Zа-яА-ЯёЁ0-9_]+)", raw_text) if not h.isdigit()]
        if not hashtags:
            hashtags = ["vpn", "впн"]
        screens_match = re.search(r"Скрин\w*:\s*(?:от\s*)?(\d+)", raw_text, re.IGNORECASE)
        target_screens = int(screens_match.group(1)) if screens_match else 20
        per_video_match = re.search(r"(\d+)\s*комментар\w*\s*на\s*ролик", raw_text, re.IGNORECASE)
        max_per_video = int(per_video_match.group(1)) if per_video_match else 4
        need_like = bool(re.search(r"лайкать|❤️|👍", raw_text, re.IGNORECASE))
        bot_names = re.findall(r"[a-zA-Z0-9_]{4,}", direct_comment.lower())
        cfg = {
            "task_name": task_name,
            "hashtags": hashtags,
            "target_screens": target_screens,
            "max_per_video": max_per_video,
            "need_like": need_like,
            "keywords": list(set(bot_names)),
        }

    # Защита от кривых значений из ИИ
    cfg["task_name"] = re.sub(r'[\\/*?:"<>|]', "", str(cfg.get("task_name") or "Task_TT")).strip()[:40] or "Task_TT"
    try:
        cfg["target_screens"] = int(cfg.get("target_screens") or 20)
    except Exception:
        cfg["target_screens"] = 20
    try:
        cfg["max_per_video"] = int(cfg.get("max_per_video") or 4)
    except Exception:
        cfg["max_per_video"] = 4

    cfg["comment_text"] = direct_comment
    cfg["is_reply_mode"] = True
    if not cfg.get("hashtags"):
        cfg["hashtags"] = ["vpn", "впн"]
    if not cfg.get("keywords"):
        cfg["keywords"] = list(set(re.findall(r"[a-zA-Z0-9_]{4,}", direct_comment.lower())))
    return cfg


# ==========================================
# МЕНЮ TELEGRAM
# ==========================================
def main_menu_kb():
    kb = types.InlineKeyboardMarkup(row_width=2)
    kb.add(
        types.InlineKeyboardButton("📱 Аккаунты", callback_data="menu_accounts"),
        types.InlineKeyboardButton("📝 Задать / Обновить ТЗ", callback_data="menu_faq"),
    )
    kb.add(types.InlineKeyboardButton("🚀 Запустить задание", callback_data="menu_run"))
    return kb


def accounts_menu_kb(user_id):
    accounts = load_accounts(user_id)
    kb = types.InlineKeyboardMarkup(row_width=1)
    for acc_id, info in accounts.items():
        uname = info.get("username", acc_id)
        kb.add(types.InlineKeyboardButton(f"👤 {uname}", callback_data=f"acc_info:{acc_id}"))
    kb.add(types.InlineKeyboardButton("➕ Добавить аккаунт", callback_data="acc_add"))
    kb.add(types.InlineKeyboardButton("🔙 Назад", callback_data="menu_main"))
    return kb


@bot.message_handler(commands=["start"])
def cmd_start(message):
    user_id = message.from_user.id
    user_states[user_id] = None
    get_user_paths(user_id)
    bot.send_message(
        message.chat.id,
        "👋 **Панель управления TikTok-ботом**\n\n"
        "1. Во вкладке **«📱 Аккаунты»** добавь свой профиль TikTok по ссылке.\n"
        "2. Во вкладке **«📝 Задать / Обновить ТЗ»** отправь текст задания.\n"
        "3. Нажми **«🚀 Запустить задание»**, выбери аккаунт и формат отправки скринов.",
        parse_mode="Markdown",
        reply_markup=main_menu_kb(),
    )


# ==========================================
# ВИРТУАЛЬНЫЙ ЭКРАН + noVNC + CLOUDFLARED
# ==========================================
def port_open(port, host="127.0.0.1"):
    try:
        with socket.create_connection((host, port), timeout=1):
            return True
    except OSError:
        return False


def find_novnc_dir():
    for d in ("/usr/share/novnc", "/usr/share/novnc/", "/opt/novnc", "/usr/share/noVNC"):
        if os.path.exists(os.path.join(d, "vnc.html")):
            return d
    return "/usr/share/novnc/"


def ensure_vnc_services():
    """Поднимает Xvfb, x11vnc и websockify, если они ещё не работают."""
    # Chromium должен открываться именно на виртуальном экране
    os.environ["DISPLAY"] = VNC_DISPLAY

    if not os.path.exists("/tmp/.X11-unix/X99"):
        subprocess.Popen(
            ["Xvfb", VNC_DISPLAY, "-screen", "0", "1600x950x24"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        time.sleep(1.5)

    if not port_open(5900):
        subprocess.Popen(
            ["x11vnc", "-display", VNC_DISPLAY, "-forever", "-nopw", "-shared", "-rfbport", "5900"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        time.sleep(1.5)

    if not port_open(6080):
        subprocess.Popen(
            ["websockify", f"--web={find_novnc_dir()}", "6080", "localhost:5900"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        time.sleep(1.5)

    if not port_open(6080):
        raise RuntimeError("noVNC (порт 6080) не запустился. Проверь установку Xvfb, x11vnc, websockify, novnc.")


def wait_url_ready(url, timeout=45):
    """Ждёт, пока публичная ссылка реально начнёт отдавать noVNC (DNS + туннель)."""
    end = time.time() + timeout
    while time.time() < end:
        try:
            with urllib.request.urlopen(url + "/vnc.html", timeout=6) as r:
                if r.status == 200:
                    return True
        except Exception:
            pass
        time.sleep(2)
    return False


def start_cloudflared_tunnel():
    """Возвращает (process, public_url). Ссылка отдаётся только когда туннель реально подключён и отвечает."""
    ensure_vnc_services()
    proc = subprocess.Popen(
        ["cloudflared", "tunnel", "--no-autoupdate", "--protocol", "http2",
         "--url", "http://127.0.0.1:6080"],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
    )
    state = {"url": None, "connected": False}

    def drain_output():
        try:
            for line in proc.stdout:
                if not state["url"]:
                    m = re.search(r"(https://[a-zA-Z0-9-]+\.trycloudflare\.com)", line)
                    if m:
                        state["url"] = m.group(1)
                if "Registered tunnel connection" in line:
                    state["connected"] = True
        except Exception:
            pass

    threading.Thread(target=drain_output, daemon=True).start()

    start_t = time.time()
    while time.time() - start_t < 45:
        if state["url"] and state["connected"]:
            ready = wait_url_ready(state["url"], timeout=45)
            if ready:
                time.sleep(15)  # запас: DNS на стороне пользователя обновляется не мгновенно
            return proc, state["url"]
        if proc.poll() is not None:
            break
        time.sleep(0.5)

    try:
        proc.terminate()
    except Exception:
        pass
    return proc, None


def detect_username(page, context):
    """Возвращает @username, если вход выполнен, иначе None."""
    cookies = {c["name"] for c in context.cookies()}
    has_session = "sessionid" in cookies or "sessionid_ss" in cookies

    try:
        link = page.locator('a[href^="/@"]').first
        if link.is_visible():
            m = re.search(r"/@([a-zA-Z0-9_.]+)", link.get_attribute("href") or "")
            if m:
                return f"@{m.group(1)}"
    except Exception:
        pass

    if has_session:
        try:
            page.goto(f"https://{TIKTOK_HOST}/profile", wait_until="domcontentloaded", timeout=30000)
            time.sleep(3)
            m = re.search(r"/@([a-zA-Z0-9_.]+)", page.url)
            if m:
                return f"@{m.group(1)}"
        except Exception:
            pass
    return None


def login_session_worker(user_id, chat_id):
    if not login_locks.acquire(blocking=False):
        bot.send_message(chat_id, "⏳ Сейчас другой пользователь проходит вход в браузере. Попробуй через 1–2 минуты.")
        return

    tunnel_proc = None
    try:
        paths = get_user_paths(user_id)
        accounts = load_accounts(user_id)
        acc_id = f"acc_{int(time.time())}"
        profile_path = os.path.join(paths["profiles"], acc_id)
        os.makedirs(profile_path, exist_ok=True)

        bot.send_message(chat_id, "⏳ Поднимаю ссылку для входа, подожди 10–30 секунд...")
        tunnel_proc, public_url = start_cloudflared_tunnel()
        if not public_url:
            shutil.rmtree(profile_path, ignore_errors=True)
            bot.send_message(chat_id, "❌ Не удалось поднять ссылку для входа. Попробуй ещё раз.")
            return

        vnc_link = f"{public_url}/vnc.html?autoconnect=true&resize=scale&reconnect=true"
        bot.send_message(
            chat_id,
            f"🔗 Перейди по ссылке ниже, чтобы войти в свой TikTok:\n\n"
            f"{vnc_link}\n\n"
            f"⏳ У тебя есть 3 минуты, чтобы авторизоваться. Как только войдёшь, бот сам сохранит аккаунт и покажет твой никнейм.\n\n"
            f"ℹ️ Если пишет «DNS_PROBE_FINISHED_NXDOMAIN»: подожди 30 секунд и обнови страницу, "
            f"либо смени DNS на 1.1.1.1 / включи VPN."
        )

        with sync_playwright() as p:
            context = p.chromium.launch_persistent_context(
                user_data_dir=profile_path,
                headless=False,
                viewport={"width": 1440, "height": 900},
                args=["--no-sandbox", "--disable-blink-features=AutomationControlled", "--start-maximized"],
            )
            page = context.pages[0] if context.pages else context.new_page()
            if HAS_STEALTH_CLASS:
                Stealth().apply_stealth_sync(page)

            page.goto(f"https://{TIKTOK_HOST}/login", wait_until="domcontentloaded", timeout=60000)

            username = None
            deadline = time.time() + 180
            while time.time() < deadline:
                time.sleep(3)
                username = detect_username(page, context)
                if username:
                    break

            if username:
                time.sleep(3)  # даём браузеру записать cookies в профиль
                accounts[acc_id] = {"username": username, "path": profile_path}
                save_accounts(user_id, accounts)
                context.close()
                bot.send_message(
                    chat_id,
                    f"✅ Аккаунт **{username}** успешно добавлен!",
                    parse_mode="Markdown",
                    reply_markup=accounts_menu_kb(user_id),
                )
            else:
                context.close()
                shutil.rmtree(profile_path, ignore_errors=True)
                bot.send_message(
                    chat_id,
                    "⏰ Время ожидания входа вышло. Нажми «➕ Добавить аккаунт», чтобы попробовать снова.",
                    reply_markup=accounts_menu_kb(user_id),
                )
    except Exception as e:
        bot.send_message(chat_id, f"❌ Ошибка при входе: {str(e)[:200]}")
    finally:
        if tunnel_proc:
            try:
                tunnel_proc.terminate()
            except Exception:
                pass
        login_locks.release()


# ==========================================
# РАБОЧИЙ ЦИКЛ КОММЕНТИНГА И ОТПРАВКА СКРИНОВ
# ==========================================
def apply_stealth(page):
    if HAS_STEALTH_CLASS:
        Stealth().apply_stealth_sync(page)
    else:
        page.add_init_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined})")


def check_and_solve_captcha(page, solver):
    for sel in ['#captcha-verify-image', '.captcha_verify_container', '[id*="captcha"]']:
        try:
            if page.locator(sel).first.is_visible():
                if SADCAPTCHA_API_KEY:
                    solver.solve_captcha_if_present()
                    time.sleep(3)
                return
        except Exception:
            pass


def make_unique_comment(base_text, index):
    dots = "." * ((index % 3) + 1)
    return f"{base_text.rstrip('.')}{dots}"


def click_any_reply_button(page, replied_comment_keys, keywords):
    priority_words = ["мне", "газ", "го", "пж", "дай ключ", "дай впн", "скинь", "ссылк"]
    reply_btns = page.locator('[data-e2e="comment-reply-1"], span:text-is("Ответить"), span:text-is("Reply"), p:text-is("Ответить"), p:text-is("Reply")')
    total = reply_btns.count()
    candidates = []

    for i in range(total):
        btn = reply_btns.nth(i)
        try:
            if not btn.is_visible():
                continue
            block = btn.locator('xpath=../..')
            block_text = block.inner_text().strip()
            block_lower = block_text.lower()
            if not block_text:
                continue
            lines = [l.strip() for l in block_text.splitlines() if l.strip() and l.strip().lower() not in ("ответить", "reply")]
            comment_key = " | ".join(lines[:2]).lower()
            if not comment_key or comment_key in replied_comment_keys:
                continue
            if any(marker in block_lower for marker in ["просмотреть ответы", "view replies", "просмотреть еще", "hide replies", "скрыть ответы"]):
                replied_comment_keys.add(comment_key)
                continue
            if any(kw in block_lower for kw in keywords):
                replied_comment_keys.add(comment_key)
                continue
            is_priority = any(pw in block_lower for pw in priority_words)
            candidates.append((0 if is_priority else 1, btn, comment_key))
        except Exception:
            continue

    candidates.sort(key=lambda x: x[0])
    if candidates:
        _, btn, comment_key = candidates[0]
        btn.scroll_into_view_if_needed()
        time.sleep(0.3)
        btn.click(force=True)
        replied_comment_keys.add(comment_key)
        return True
    return False


def send_screens_result(chat_id, save_dir, task_name, delivery_mode, start_from_idx=1):
    """Отправляет скриншоты либо ZIP-архивом, либо пачкой фотографий (альбомами по 10 штук)."""
    all_files = sorted(
        [f for f in os.listdir(save_dir) if f.endswith(".jpg")],
        key=lambda x: int(re.search(r"(\d+)", x).group(1)),
    )
    if not all_files:
        return

    if delivery_mode == "photos":
        files_to_send = [
            f for f in all_files
            if int(re.search(r"(\d+)", f).group(1)) >= start_from_idx
        ]
        if not files_to_send:
            return
        bot.send_message(chat_id, f"🖼 Отправляю {len(files_to_send)} скриншотов пачкой в чат...")
        for i in range(0, len(files_to_send), 10):
            chunk = files_to_send[i:i + 10]
            media = []
            opened_files = []
            for idx, fname in enumerate(chunk):
                f_obj = open(os.path.join(save_dir, fname), "rb")
                opened_files.append(f_obj)
                caption = f"📸 {task_name} ({i + 1}–{i + len(chunk)})" if idx == 0 else None
                media.append(types.InputMediaPhoto(f_obj, caption=caption))
            try:
                if len(media) == 1:
                    opened_files[0].seek(0)
                    bot.send_photo(chat_id, opened_files[0], caption=media[0].caption)
                else:
                    bot.send_media_group(chat_id, media)
            finally:
                for f_obj in opened_files:
                    f_obj.close()
            time.sleep(1.5)
    else:
        zip_path = f"{save_dir}.zip"
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
            for fname in all_files:
                zf.write(os.path.join(save_dir, fname), arcname=fname)
        with open(zip_path, "rb") as f:
            bot.send_document(
                chat_id,
                f,
                caption=f"📦 Готовые скриншоты по заданию {task_name} ({len(all_files)} шт.)",
            )
        os.remove(zip_path)


def task_worker(user_id, chat_id, acc_id, delivery_mode):
    paths = get_user_paths(user_id)
    accounts = load_accounts(user_id)
    acc_info = accounts.get(acc_id)
    if not acc_info:
        bot.send_message(chat_id, "❌ Аккаунт не найден.")
        return

    try:
        with open(paths["faq"], "r", encoding="utf-8") as f:
            cfg = parse_faq_text(f.read())
    except Exception as e:
        bot.send_message(chat_id, f"❌ Не удалось прочитать ТЗ: {str(e)[:150]}")
        return

    task_name = cfg["task_name"]
    target_screens = cfg["target_screens"]
    max_per_video = cfg["max_per_video"]
    base_comment = cfg["comment_text"]
    keywords = cfg["keywords"]
    username = acc_info["username"]
    profile_path = acc_info["path"]

    save_dir = os.path.join(paths["screens"], f"{task_name}_{int(time.time())}")
    os.makedirs(save_dir, exist_ok=True)
    total_screens = 0
    last_sent_idx = 1

    state_ctrl = {"stop": False, "extra_event": threading.Event(), "extra_count": 0}
    active_workers[user_id] = state_ctrl

    mode_label = "🖼 Пачкой фото в чат" if delivery_mode == "photos" else "📦 ZIP-архивом"
    bot.send_message(
        chat_id,
        f"🚀 Запустил задание **{task_name}** на аккаунте **{username}**\n"
        f"• Цель: {target_screens} скринов (макс {max_per_video}/ролик)\n"
        f"• Выдача: {mode_label}\n"
        f"• Текст: `{base_comment}`",
        parse_mode="Markdown",
    )

    try:
        ensure_vnc_services()  # виртуальный экран нужен и для рабочего браузера

        with sync_playwright() as p:
            context = p.chromium.launch_persistent_context(
                user_data_dir=profile_path,
                headless=False,
                viewport={"width": 1600, "height": 950},
                args=["--no-sandbox", "--disable-blink-features=AutomationControlled"],
            )
            page = context.pages[0] if context.pages else context.new_page()
            apply_stealth(page)
            solver = PlaywrightSolver(page, SADCAPTCHA_API_KEY)

            page.goto(f"https://{TIKTOK_HOST}/", wait_until="domcontentloaded", timeout=60000)
            time.sleep(5)
            check_and_solve_captcha(page, solver)

            tag = random.choice(cfg["hashtags"]).lstrip("#").strip()
            search_input = page.locator('input[data-e2e="search-user-input"], input[type="search"]').first
            if search_input.is_visible():
                search_input.click()
                page.keyboard.type(f"#{tag}", delay=60)
                page.keyboard.press("Enter")
            else:
                page.goto(f"https://{TIKTOK_HOST}/search/video?q=%23{tag}", wait_until="domcontentloaded", timeout=60000)

            time.sleep(4)
            check_and_solve_captcha(page, solver)

            video_tab = page.locator('[data-e2e="search-tab-video"], div[role="tab"]:has-text("Видео"), div[role="tab"]:has-text("Videos")').first
            if video_tab.is_visible():
                video_tab.click()
                time.sleep(2.5)

            videos = page.locator('[data-e2e="search_video-item"]')
            if videos.count() == 0:
                bot.send_message(chat_id, f"❌ Видео по тегу #{tag} не найдены.")
                context.close()
                return

            videos.first.click()
            time.sleep(3)

            while not state_ctrl["stop"]:
                while total_screens < target_screens and not state_ctrl["stop"]:
                    video_start = time.time()
                    try:
                        check_and_solve_captcha(page, solver)
                        time.sleep(1.5)

                        replied_keys = set()

                        for _ in range(max_per_video):
                            if total_screens >= target_screens or state_ctrl["stop"]:
                                break

                            if not click_any_reply_button(page, replied_keys, keywords):
                                break

                            time.sleep(1.0)
                            input_box = page.locator('[data-e2e="comment-input"] [contenteditable="true"], div[contenteditable="true"][role="textbox"]').first
                            if not input_box.is_visible():
                                continue

                            input_box.click(force=True)
                            text_to_send = make_unique_comment(base_comment, total_screens)
                            page.keyboard.type(text_to_send, delay=35)
                            time.sleep(0.5)

                            page.keyboard.press("Enter")
                            time.sleep(2.5)

                            total_screens += 1
                            filepath = os.path.join(save_dir, f"screen_{total_screens}.jpg")
                            page.screenshot(path=filepath, type="jpeg", quality=85, full_page=False)

                            if total_screens % 5 == 0 or total_screens == target_screens:
                                bot.send_message(chat_id, f"✅ Сделано скриншотов: {total_screens}/{target_screens}")

                        if total_screens >= target_screens:
                            break

                        wait_time = max(1.0, random.uniform(13.0, 15.0) - (time.time() - video_start))
                        time.sleep(wait_time)
                        page.keyboard.press("ArrowDown")
                        time.sleep(2.2)
                    except Exception:
                        time.sleep(2)
                        try:
                            page.keyboard.press("ArrowDown")
                        except Exception:
                            pass

                if state_ctrl["stop"]:
                    break

                send_screens_result(chat_id, save_dir, task_name, delivery_mode, start_from_idx=last_sent_idx)
                last_sent_idx = total_screens + 1

                kb = types.InlineKeyboardMarkup(row_width=1)
                kb.add(
                    types.InlineKeyboardButton("➕ Сделать штрафные скриншоты", callback_data="task_extra"),
                    types.InlineKeyboardButton("🔄 Новое задание (обновить ТЗ)", callback_data="task_next"),
                    types.InlineKeyboardButton("🏁 Завершить", callback_data="task_finish"),
                )
                bot.send_message(
                    chat_id,
                    f"🏁 Задание **{task_name}** выполнено ({total_screens} скринов)!\nНужно ли доделать штрафные скриншоты?",
                    parse_mode="Markdown",
                    reply_markup=kb,
                )

                state_ctrl["extra_event"].clear()
                state_ctrl["extra_event"].wait()

                if state_ctrl["stop"]:
                    break

                if state_ctrl["extra_count"] > 0:
                    target_screens += state_ctrl["extra_count"]
                    state_ctrl["extra_count"] = 0
                    page.keyboard.press("ArrowDown")
                    time.sleep(2.5)
                else:
                    break

            context.close()
    except Exception as e:
        bot.send_message(chat_id, f"💥 Ошибка при выполнении: {str(e)[:180]}")
    finally:
        active_workers.pop(user_id, None)


# ==========================================
# ОБРАБОТКА КНОПОК И ТЕКСТА
# ==========================================
@bot.callback_query_handler(func=lambda call: True)
def handle_callbacks(call):
    user_id = call.from_user.id
    chat_id = call.message.chat.id
    data = call.data

    if data == "menu_main":
        bot.edit_message_text("🏠 Главное меню:", chat_id, call.message.message_id, reply_markup=main_menu_kb())

    elif data == "menu_accounts":
        bot.edit_message_text(
            "📱 **Твои добавленные аккаунты TikTok:**\nНажми на аккаунт для управления или добавь новый.",
            chat_id,
            call.message.message_id,
            parse_mode="Markdown",
            reply_markup=accounts_menu_kb(user_id),
        )

    elif data == "acc_add":
        bot.answer_callback_query(call.id, "Запускаю браузер для входа...")
        threading.Thread(target=login_session_worker, args=(user_id, chat_id), daemon=True).start()

    elif data.startswith("acc_info:"):
        acc_id = data.split(":", 1)[1]
        accounts = load_accounts(user_id)
        info = accounts.get(acc_id)
        if not info:
            bot.answer_callback_query(call.id, "Аккаунт не найден")
            return
        kb = types.InlineKeyboardMarkup(row_width=1)
        kb.add(
            types.InlineKeyboardButton("🚀 Запустить задание на этом аккаунте", callback_data=f"choose_fmt:{acc_id}"),
            types.InlineKeyboardButton("🗑 Удалить аккаунт", callback_data=f"del_acc:{acc_id}"),
            types.InlineKeyboardButton("🔙 К списку аккаунтов", callback_data="menu_accounts"),
        )
        bot.edit_message_text(f"👤 Аккаунт: **{info['username']}**", chat_id, call.message.message_id, parse_mode="Markdown", reply_markup=kb)

    elif data.startswith("del_acc:"):
        acc_id = data.split(":", 1)[1]
        accounts = load_accounts(user_id)
        info = accounts.pop(acc_id, None)
        if info:
            shutil.rmtree(info["path"], ignore_errors=True)
            save_accounts(user_id, accounts)
        bot.answer_callback_query(call.id, "Аккаунт удалён")
        bot.edit_message_text("📱 Аккаунты:", chat_id, call.message.message_id, reply_markup=accounts_menu_kb(user_id))

    elif data == "menu_faq":
        user_states[user_id] = "wait_faq"
        bot.send_message(
            chat_id,
            "📝 **Отправь текст нового ТЗ одним сообщением.**\n\n"
            "⚠️ **Обязательно:** в тексте должна быть строка с меткой `Текст:` или `Комментарий:` (например:\n"
            "`Текст: Никогда не подводит PullUpVpn_bot`\n"
            "или\n"
            "`Комментарий: Никогда не подводит PullUpVpn_bot`), иначе бот не примет задание.",
            parse_mode="Markdown",
        )

    elif data == "menu_run":
        paths = get_user_paths(user_id)
        if not os.path.exists(paths["faq"]):
            bot.answer_callback_query(call.id, "Сначала задай ТЗ!", show_alert=True)
            return
        accounts = load_accounts(user_id)
        if not accounts:
            bot.answer_callback_query(call.id, "Сначала добавь хотя бы один аккаунт!", show_alert=True)
            return
        kb = types.InlineKeyboardMarkup(row_width=1)
        for acc_id, info in accounts.items():
            kb.add(types.InlineKeyboardButton(f"▶️ {info['username']}", callback_data=f"choose_fmt:{acc_id}"))
        kb.add(types.InlineKeyboardButton("🔙 Назад", callback_data="menu_main"))
        bot.edit_message_text("Выбери аккаунт для запуска задания:", chat_id, call.message.message_id, reply_markup=kb)

    elif data.startswith("choose_fmt:"):
        acc_id = data.split(":", 1)[1]
        paths = get_user_paths(user_id)
        if not os.path.exists(paths["faq"]):
            bot.answer_callback_query(call.id, "Сначала задай ТЗ!", show_alert=True)
            return
        if user_id in active_workers:
            bot.answer_callback_query(call.id, "У тебя уже запущено задание!", show_alert=True)
            return
        kb = types.InlineKeyboardMarkup(row_width=1)
        kb.add(
            types.InlineKeyboardButton("🖼 Пачкой фото сразу в чат", callback_data=f"start_task:{acc_id}:photos"),
            types.InlineKeyboardButton("📦 Одним ZIP-архивом", callback_data=f"start_task:{acc_id}:zip"),
            types.InlineKeyboardButton("🔙 Назад", callback_data="menu_run"),
        )
        bot.edit_message_text(
            "📤 **Как отправить готовые скриншоты после выполнения?**",
            chat_id,
            call.message.message_id,
            parse_mode="Markdown",
            reply_markup=kb,
        )

    elif data.startswith("start_task:"):
        _, acc_id, delivery_mode = data.split(":", 2)
        if user_id in active_workers:
            bot.answer_callback_query(call.id, "У тебя уже запущено задание!", show_alert=True)
            return
        bot.answer_callback_query(call.id, "Запускаю задание...")
        threading.Thread(target=task_worker, args=(user_id, chat_id, acc_id, delivery_mode), daemon=True).start()

    elif data == "task_extra":
        user_states[user_id] = "wait_extra"
        bot.send_message(chat_id, "🔢 Напиши числом, сколько штрафных скриншотов нужно доделать (например: `3`):", parse_mode="Markdown")

    elif data == "task_next":
        worker = active_workers.get(user_id)
        if worker:
            worker["extra_count"] = 0
            worker["extra_event"].set()
        user_states[user_id] = "wait_faq"
        bot.send_message(
            chat_id,
            "📝 Отправь новое ТЗ в чат (не забудь указать `Текст: ...` или `Комментарий: ...`):",
            parse_mode="Markdown",
        )

    elif data == "task_finish":
        worker = active_workers.get(user_id)
        if worker:
            worker["stop"] = True
            worker["extra_count"] = 0
            worker["extra_event"].set()
        bot.send_message(chat_id, "👋 Работа завершена!", reply_markup=main_menu_kb())


@bot.message_handler(func=lambda m: True)
def handle_text(message):
    user_id = message.from_user.id
    chat_id = message.chat.id
    state = user_states.get(user_id)
    text = (message.text or "").strip()

    if state == "wait_faq":
        try:
            cfg = parse_faq_text(text)
            paths = get_user_paths(user_id)
            with open(paths["faq"], "w", encoding="utf-8") as f:
                f.write(text)
            user_states[user_id] = None
            bot.send_message(
                chat_id,
                f"✅ **ТЗ успешно сохранено!**\n"
                f"• Название: `{cfg['task_name']}`\n"
                f"• Хештеги: `{cfg['hashtags']}`\n"
                f"• Скриншотов: `{cfg['target_screens']}`\n"
                f"• Текст комментария: `{cfg['comment_text']}`",
                parse_mode="Markdown",
                reply_markup=main_menu_kb(),
            )
        except ValueError as e:
            bot.send_message(
                chat_id,
                f"❌ **Ошибка в ТЗ:** {e}\n\n"
                f"Добавь в текст строку вида:\n`Текст: твой комментарий`\nили\n`Комментарий: твой комментарий`\nи отправь ТЗ ещё раз:",
                parse_mode="Markdown",
            )

    elif state == "wait_extra":
        if text.isdigit() and int(text) > 0:
            worker = active_workers.get(user_id)
            if worker:
                worker["extra_count"] = int(text)
                worker["extra_event"].set()
                user_states[user_id] = None
                bot.send_message(chat_id, f"⏳ Доделываю {text} штрафных скриншотов...")
            else:
                user_states[user_id] = None
                bot.send_message(chat_id, "❌ Активная задача уже закрыта.", reply_markup=main_menu_kb())
        else:
            bot.send_message(chat_id, "⚠️ Введи целое положительное число (например: `4`).", parse_mode="Markdown")


if __name__ == "__main__":
    try:
        ensure_vnc_services()
    except Exception as e:
        print(f"[!] Не удалось заранее поднять VNC: {e}")
    print("[🤖] Telegram-бот запущен и готов к работе...")
    bot.infinity_polling(timeout=60, long_polling_timeout=60)
