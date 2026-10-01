import os
import re
import time
import random
import threading
import requests
import urllib3
from datetime import datetime, timedelta
import vk_api
from vk_api.bot_longpoll import VkBotLongPoll, VkBotEventType
from apscheduler.schedulers.background import BackgroundScheduler

TOKEN = "vk1.a.Nc7xpeoCZz3VQDoDHv-ExhOat7LJwv0Ik_cZU_tLYasVC8T-heOyYSiNf5FKsZ5iDoauRmoMP1NzeHQHJBDdR9DKQQ4PqUNecq-nnDE8leTfR0ohrgJ980jnH0vQhoSsgpWePo2EDhnFKxBLgR9koEOeI2sZmU0hjDk-5mQo7z7YS9MtQzYhFuC6YZaTpD7eJAAZ_in_-bgRTm6ILTjG5w"
GROUP_ID = 241918176
PEER_ID = 2000000002
BOARD_TOPIC_NAME = "Участники"
COLLECT_MINUTES = 10
PARTICIPANTS_FILE = "participants.txt"

LOCATIONS_BRANCHES = ["виноградник", "розарий", "ягодник"]
LOCATIONS_HERBS = ["виноградник", "розарий", "ягодник", "уголок"]

BRANCHES_TAKEN_DEADLINE = "12:10"
BRANCHES_WISH_DEADLINE = "12:30"
HERBS_TAKEN_DEADLINE = "16:10"
HERBS_WISH_DEADLINE = "16:30"

vk_session = vk_api.VkApi(token=TOKEN, api_version="5.199")
vk = vk_session.get_api()
longpoll = VkBotLongPoll(vk_session, GROUP_ID)

responders = {}
game_names = {}
game_moons = {}
lock = threading.RLock()
current_task = None
collection_active = False
order_counter = 0
scheduler = BackgroundScheduler()

task_start_times = {}
hunt_count = 2
current_hunt_x = 2

last_branches_cmid = None
last_branches_text = None
last_herbs_cmid = None
last_herbs_text = None
last_ohota_cmid = None
last_ohota_text = None
last_delivery_cmid = None
last_delivery_text = None


def new_random_id():
    return random.randint(1, 2**31 - 1)


def send_message(text):
    """Отправляет сообщение в беседу.
    Использует peer_ids (множественное) — только так messages.send
    возвращает conversation_message_id, пригодный для messages.edit
    в беседах. Возвращает cmid или None."""
    try:
        response = vk.messages.send(
            peer_ids=str(PEER_ID),
            message=text,
            random_id=new_random_id()
        )
        if isinstance(response, list) and response:
            cmid = response[0].get("conversation_message_id")
            print(f"[debug] send_message: cmid={cmid}")
            return cmid
        return None
    except vk_api.exceptions.ApiError as e:
        print(f"[error] send_message [{getattr(e, 'code', '?')}]: {e}")
        return None
    except Exception as e:
        print(f"[error] send_message: {e}")
        return None


def send_pm(user_id, text):
    try:
        return vk.messages.send(
            peer_id=int(user_id),
            message=text,
            random_id=new_random_id()
        )
    except vk_api.exceptions.ApiError as e:
        print(f"[error] send_pm [{getattr(e, 'code', '?')}]: {e}")
        return 0
    except Exception as e:
        print(f"[error] send_pm: {e}")
        return 0


def edit_message(cmid, text):
    """Редактирует сообщение в беседе по conversation_message_id.
    Для бесед messages.edit требует именно cmid, а не глобальный message_id."""
    try:
        vk.messages.edit(
            peer_id=int(PEER_ID),
            conversation_message_id=int(cmid),
            message=text
        )
        print(f"[debug] edit_message: cmid={cmid} отредактирован")
        return True
    except vk_api.exceptions.ApiError as e:
        print(f"[error] edit_message [{getattr(e, 'code', '?')}]: {e}")
        return False
    except Exception as e:
        print(f"[error] edit_message: {e}")
        return False


def _is_bot_message(item):
    """Сообщение принадлежит боту, если оно исходящее (out=1).
    В беседах from_id бота может быть как -GROUP_ID, так и другим,
    поэтому надёжнее ориентироваться на out=1."""
    return item.get("out") == 1


def find_last_bot_message(matcher, count=200):
    """Ищет последнее сообщение бота в беседе по предикату matcher.
    Возвращает (conversation_message_id, text) или (None, None)."""
    try:
        history = vk.messages.getHistory(peer_id=int(PEER_ID), count=count)
        items = history.get("items", [])
        print(f"[debug] getHistory: получено {len(items)} сообщений")
        for item in items:
            if not _is_bot_message(item):
                continue
            text = item.get("text", "")
            if matcher(text):
                cmid = item.get("conversation_message_id")
                print(f"[debug] найдено в истории: cmid={cmid}, text={text!r}")
                return cmid, text
    except Exception as e:
        print(f"[warn] find_last_bot_message: {e}")
    return None, None


def find_last_branches_distribution():
    def matcher(text):
        return "Уборка до" in text and ("12:1" in text or "12:3" in text)
    return find_last_bot_message(matcher)


def find_last_herbs_distribution():
    def matcher(text):
        return "Уборка до" in text and ("16:1" in text or "16:3" in text)
    return find_last_bot_message(matcher)


def find_last_ohota_distribution():
    def matcher(text):
        return "мышек" in text
    return find_last_bot_message(matcher)


def find_last_delivery():
    def matcher(text):
        return text.lstrip().startswith("Доставка")
    return find_last_bot_message(matcher)


def get_name(user_id):
    try:
        info = vk.users.get(user_ids=user_id)[0]
        return f"{info['first_name']} {info['last_name']}"
    except Exception as e:
        print(f"[warn] get_name для {user_id}: {e}")
        return f"ID {user_id}"


def get_display_name(user_id):
    return game_names.get(user_id) or get_name(user_id)


def message_still_exists(cmid):
    try:
        res = vk.messages.getByConversationMessageId(
            peer_id=int(PEER_ID),
            conversation_message_ids=[int(cmid)]
        )
        return bool(res.get("items"))
    except Exception as e:
        print(f"[warn] getByConversationMessageId: {e}")
        return False


def send_like(cmid):
    try:
        vk.messages.sendReaction(
            v="5.204",
            peer_id=int(PEER_ID),
            cmid=int(cmid),
            reaction_id=1
        )
        print(f"[ok] Лайк поставлен на cmid={cmid}")
    except vk_api.exceptions.ApiError as api_err:
        code = getattr(api_err, "code", "?")
        if code == 15:
            print("[error] Ошибка 15: бот должен быть администратором беседы!")
        elif code == 901:
            print("[warn] Ошибка 901: нет разрешения на реакции")
        else:
            print(f"[error] send_like [{code}]: {api_err}")
    except Exception as e:
        print(f"[error] send_like: {e}")


def replace_name_ci(text, old, new):
    pattern = re.compile(
        r'(?<![^\W\d_])' + re.escape(old) + r'(?![^\W\d_])',
        re.IGNORECASE | re.UNICODE
    )
    return pattern.sub(new, text)


def load_participants():
    global game_names, game_moons
    if not os.path.exists(PARTICIPANTS_FILE):
        print(f"[warn] Файл {PARTICIPANTS_FILE} не найден")
        return
    try:
        with open(PARTICIPANTS_FILE, "r", encoding="utf-8") as f:
            text = f.read()
        game_names.clear()
        game_moons.clear()
        pattern_full = re.compile(r'^\[\s*(\d+)\s*\]\s*(.+?)\s*\[\s*(\d+)\s*\]\s*$')
        pattern_short = re.compile(r'^\[\s*(\d+)\s*\]\s*(.+?)\s*$')
        for raw_line in text.splitlines():
            line = raw_line.strip()
            if not line or not line.startswith("["):
                continue
            m = pattern_full.match(line)
            if m:
                user_id = int(m.group(1).strip())
                name = m.group(2).strip()
                moons = int(m.group(3).strip())
            else:
                m = pattern_short.match(line)
                if not m:
                    continue
                user_id = int(m.group(1).strip())
                name = m.group(2).strip()
                moons = None
            if name:
                game_names[user_id] = name
            if moons is not None:
                game_moons[user_id] = moons
        print(f"[ok] Участники загружены: {len(game_names)}")
    except Exception as e:
        print(f"[error] Ошибка парсинга файла: {e}")


def start_collection(task_name, text, x=None):
    global responders, current_task, collection_active, order_counter, current_hunt_x
    load_participants()
    with lock:
        responders = {}
        current_task = task_name
        collection_active = True
        order_counter = 0
        if task_name == "Охота":
            current_hunt_x = x
        task_start_times[task_name] = datetime.now()
    send_message(text)
    scheduler.add_job(
        finish_collection,
        trigger="date",
        run_date=datetime.now() + timedelta(minutes=COLLECT_MINUTES),
        args=[task_name],
        id=f"finish_{task_name}_{int(time.time()*1000)}",
        replace_existing=False,
    )
    print(f"[{datetime.now():%H:%M:%S}] Сбор запущен: {task_name}")


def _format_branches_report(user_ids):
    locations = LOCATIONS_BRANCHES
    taken_parts = []
    for i, uid in enumerate(user_ids):
        if i < len(locations):
            taken_parts.append(f"{get_display_name(uid)} {locations[i]}")
    used = min(len(user_ids), len(locations))
    remaining = locations[used:]
    taken_line = ", ".join(taken_parts) if taken_parts else ""
    taken_dl = f"(уборка до {BRANCHES_TAKEN_DEADLINE})"
    if remaining:
        wish_line = f"{', '.join(remaining)} желающие"
        wish_dl = f"(уборка до {BRANCHES_WISH_DEADLINE})"
        return f"{taken_line} {taken_dl}, {wish_line} {wish_dl}"
    return f"{taken_line} {taken_dl}"


def _format_herbs_report(user_ids):
    locations = LOCATIONS_HERBS
    taken_parts = []
    wish_parts = []
    angle_index = None
    if len(user_ids) == 4:
        moons_list = [game_moons.get(uid, 999) for uid in user_ids]
        angle_index = min(range(4), key=lambda i: moons_list[i])
    if angle_index is not None:
        angle_uid = user_ids[angle_index]
        other_uids = [uid for i, uid in enumerate(user_ids) if i != angle_index]
        for i, uid in enumerate(other_uids):
            taken_parts.append(f"{get_display_name(uid)} {locations[i]}")
        taken_parts.append(f"{get_display_name(angle_uid)} {locations[3]}")
    else:
        for i, uid in enumerate(user_ids):
            if i < 3:
                taken_parts.append(f"{get_display_name(uid)} {locations[i]}")
        used = min(len(user_ids), 3)
        remaining = locations[used:]
        if remaining:
            wish_parts.append(f"{', '.join(remaining)} желающие")
    taken_line = ", ".join(taken_parts)
    taken_dl = f"(уборка до {HERBS_TAKEN_DEADLINE})"
    if wish_parts:
        wish_line = ", ".join(wish_parts)
        wish_dl = f"(уборка до {HERBS_WISH_DEADLINE})"
        return f"{taken_line} {taken_dl}, {wish_line} {wish_dl}"
    return f"{taken_line} {taken_dl}"


def finish_collection(task_name):
    global collection_active
    global last_branches_cmid, last_branches_text
    global last_herbs_cmid, last_herbs_text
    global last_ohota_cmid, last_ohota_text

    with lock:
        if not collection_active:
            return
        if current_task != task_name:
            return
        collection_active = False
        snapshot = list(responders.values())

    valid = [item for item in snapshot if message_still_exists(item["msg_id"])]
    valid.sort(key=lambda x: x["order"])
    user_ids = [item["user_id"] for item in valid]

    if task_name == "Мох":
        print(f"[{datetime.now():%H:%M:%S}] Мох завершён")
        return

    if task_name == "Ветки":
        if not valid:
            send_message("Все локации убираются в режиме конкуренции до 12:30")
        else:
            report = _format_branches_report(user_ids)
            cmid = send_message(report)
            last_branches_cmid = cmid
            last_branches_text = report
            print(f"[debug] last_branches_cmid = {cmid}")
        print(f"[{datetime.now():%H:%M:%S}] Финал Ветки: {len(user_ids)}")
        return

    if task_name == "Травы":
        if not valid:
            send_message("Все локации убираются в режиме конкуренции до 16:30")
        else:
            report = _format_herbs_report(user_ids)
            cmid = send_message(report)
            last_herbs_cmid = cmid
            last_herbs_text = report
            print(f"[debug] last_herbs_cmid = {cmid}")
        print(f"[{datetime.now():%H:%M:%S}] Финал Травы: {len(user_ids)}")
        return
    if task_name == "Охота":
        if not valid:
            send_message("Слоты свободны, может занять любой желающий")
            return
        n = len(valid)
        x = current_hunt_x
        base = x // n
        rem = x % n
        allocations = []
        for i, item in enumerate(valid):
            uid = item["user_id"]
            slots = base + (1 if i < rem else 0)
            allocations.append((uid, slots * 5))
        groups = []
        for uid, mice in allocations:
            name = get_display_name(uid)
            if groups and groups[-1][0] == mice:
                groups[-1][1].append(name)
            else:
                groups.append((mice, [name]))
        parts = []
        for mice, names in groups:
            if len(names) == 1:
                parts.append(f"{names[0]} {mice} мышек")
            else:
                parts.append(f"{' '.join(names)} по {mice} мышек")
        report = ", ".join(parts)
        cmid = send_message(report)
        last_ohota_cmid = cmid
        last_ohota_text = report
        print(f"[debug] last_ohota_cmid = {cmid}")
        print(f"[{datetime.now():%H:%M:%S}] Финал Охота: {len(user_ids)}")
        return


def task_branches():
    start_collection("Ветки", "Ветки!")


def task_herbs():
    start_collection("Травы", "Травы!")


def task_moss():
    start_collection("Мох", "Мох!")


def task_ohota():
    global hunt_count
    x = hunt_count
    start_collection("Охота", f"Охота {x}", x)
    hunt_count = 2
    print(f"[debug] hunt_count сброшен в 2 (использовался {x})")


def handle_private_command(user_id, raw_text):
    global hunt_count
    global last_branches_cmid, last_branches_text
    global last_herbs_cmid, last_herbs_text
    global last_ohota_cmid, last_ohota_text
    global last_delivery_cmid, last_delivery_text

    lines = raw_text.splitlines()
    if not lines:
        return
    first_line_cleaned = lines[0].strip().lower().strip(" :,!?;.")

    if first_line_cleaned == "участники":
        new_list = "\n".join(lines[1:]).strip()
        try:
            with open(PARTICIPANTS_FILE, "w", encoding="utf-8") as f:
                f.write(new_list)
            load_participants()
            send_pm(user_id, f"Список обновлён! Участников: {len(game_names)}")
        except Exception as fe:
            send_pm(user_id, f"Ошибка записи: {fe}")
        return

    if first_line_cleaned == "список":
        if os.path.exists(PARTICIPANTS_FILE):
            with open(PARTICIPANTS_FILE, "r", encoding="utf-8") as f:
                content = f.read().strip()
            send_pm(user_id, f"Текущий список:\n\n{content}" if content else "Файл пуст.")
        else:
            send_pm(user_id, "Список ещё не создан.")
        return

    if first_line_cleaned == "сбор ветки":
        task_branches()
        send_pm(user_id, "Ручной сбор [Ветки] запущен!")
        return

    if first_line_cleaned == "сбор травы":
        task_herbs()
        send_pm(user_id, "Ручной сбор [Травы] запущен!")
        return

    if first_line_cleaned == "сбор мох":
        task_moss()
        send_pm(user_id, "Ручной сбор [Мох] запущен!")
        return

    if first_line_cleaned == "сбор охота":
        task_ohota()
        send_pm(user_id, "Ручной сбор [Охота] запущен!")
        return

    if first_line_cleaned == "доставка":
        cmid = send_message("Доставка!")
        last_delivery_cmid = cmid
        last_delivery_text = "Доставка!"
        print(f"[debug] last_delivery_cmid = {cmid}")
        send_pm(user_id, "Доставка отправлена")
        return

    if first_line_cleaned.startswith("охота отмена"):
        match = re.match(r'^охота\s+отмена\s+(.+)$', raw_text, re.IGNORECASE | re.UNICODE)
        if match:
            name = match.group(1).strip().strip(" .,!?:;")
            target_cmid = last_ohota_cmid
            target_text = last_ohota_text
            if not target_cmid:
                print("[debug] last_ohota_cmid пуст, ищу в истории...")
                target_cmid, target_text = find_last_ohota_distribution()
            if target_cmid and target_text:
                parts = [p.strip() for p in target_text.split(",")]
                pat = re.compile(r'^' + re.escape(name) + r'(?=\s|$)', re.IGNORECASE | re.UNICODE)
                new_parts = [p for p in parts if not pat.match(p)]
                new_text = ", ".join(new_parts)
                if edit_message(target_cmid, new_text):
                    last_ohota_cmid = target_cmid
                    last_ohota_text = new_text
                    send_pm(user_id, "Отмена выполнена")
                else:
                    send_pm(user_id, "Не удалось отредактировать сообщение")
            else:
                send_pm(user_id, "Нет последнего распределения охоты")
        return

    if first_line_cleaned.startswith("охота"):
        match = re.match(r'^охота\s+(\d+)$', raw_text, re.IGNORECASE)
        if match:
            x = int(match.group(1))
            if x < 1:
                send_pm(user_id, "Число должно быть >= 1")
                return
            hunt_count = x
            print(f"[debug] hunt_count = {hunt_count}")
            send_pm(user_id, f"Охота установлена на {x}")
        return

    if first_line_cleaned.startswith("замена"):
        match = re.match(
            r'^замена\s+(травы|трава|трав|ветки|ветка|веток)\s+(.+?)\s*=\s*(.+)$',
            raw_text, re.IGNORECASE | re.UNICODE
        )
        if not match:
            send_pm(user_id, "Не понял команду замена")
            return
        task_type = match.group(1).lower().strip()
        name1 = match.group(2).strip().strip(" .,!?:;")
        name2 = match.group(3).strip().strip(" .,!?:;")
        print(f"[debug] замена: type={task_type}, {name1!r}={name2!r}")

        if task_type.startswith("вет"):
            target_cmid = last_branches_cmid
            target_text = last_branches_text
            if not target_cmid:
                print("[debug] last_branches_cmid пуст, ищу в истории...")
                target_cmid, target_text = find_last_branches_distribution()
            if target_cmid and target_text:
                new_text = replace_name_ci(target_text, name1, name2)
                print(f"[debug] было:\n{target_text}\nстало:\n{new_text}")
                if edit_message(target_cmid, new_text):
                    last_branches_cmid = target_cmid
                    last_branches_text = new_text
                    send_pm(user_id, "Замена выполнена")
                else:
                    send_pm(user_id, "Не удалось отредактировать сообщение")
            else:
                send_pm(user_id, "Нет последнего распределения веток")

        elif task_type.startswith("трав"):
            target_cmid = last_herbs_cmid
            target_text = last_herbs_text
            if not target_cmid:
                print("[debug] last_herbs_cmid пуст, ищу в истории...")
                target_cmid, target_text = find_last_herbs_distribution()
            if target_cmid and target_text:
                new_text = replace_name_ci(target_text, name1, name2)
                print(f"[debug] было:\n{target_text}\nстало:\n{new_text}")
                if edit_message(target_cmid, new_text):
                    last_herbs_cmid = target_cmid
                    last_herbs_text = new_text
                    send_pm(user_id, "Замена выполнена")
                else:
                    send_pm(user_id, "Не удалось отредактировать сообщение")
            else:
                send_pm(user_id, "Нет последнего распределения трав")
        return

    if first_line_cleaned.startswith("внеси доставка") or first_line_cleaned.startswith("внеси  доставка"):
        match = re.match(r'^внеси\s+доставка\s+(.+)$', raw_text, re.IGNORECASE | re.UNICODE)
        if not match:
            return
        entries_str = match.group(1).strip()
        entries = []
        for part in entries_str.split(","):
            part = part.strip()
            if not part:
                continue
            tokens = part.rsplit(None, 1)
            if len(tokens) == 2 and tokens[1].isdigit():
                entries.append(f"{tokens[0]} {tokens[1]}")
        if not entries:
            send_pm(user_id, "Не удалось распознать записи")
            return
        print(f"[debug] внеси доставка: {entries}")

        target_cmid = last_delivery_cmid
        target_text = last_delivery_text
        if not target_cmid:
            print("[debug] last_delivery_cmid пуст, ищу в истории...")
            target_cmid, target_text = find_last_delivery()

        if target_cmid and target_text is not None:
            stripped = (target_text or "").strip()
            if stripped == "Доставка!" or stripped == "":
                new_text = "Доставка\n" + "\n".join(entries)
            else:
                new_text = target_text.rstrip() + "\n" + "\n".join(entries)
            print(f"[debug] новое содержимое доставки:\n{new_text}")
            if edit_message(target_cmid, new_text):
                last_delivery_cmid = target_cmid
                last_delivery_text = new_text
                send_pm(user_id, "Доставка обновлена")
            else:
                send_pm(user_id, "Не удалось отредактировать сообщение")
        else:
            new_text = "Доставка\n" + "\n".join(entries)
            cmid = send_message(new_text)
            last_delivery_cmid = cmid
            last_delivery_text = new_text
            print(f"[debug] создана новая доставка, cmid={cmid}")
            send_pm(user_id, "Доставка создана")
        return


def handle_game_message(user_id, raw_text, msg):
    global collection_active, order_counter
    cleaned = raw_text.lower().strip(" .,!?:;")
    match = re.match(r'^я(?:\s+(\d+))?$', cleaned)
    if not match:
        return
    requested = None
    if match.group(1):
        requested = int(match.group(1))
    cm_id = msg.get("conversation_message_id")
    if not cm_id:
        return

    with lock:
        if not collection_active:
            return
        task_lower = str(current_task).lower().strip()

        if task_lower == "мох":
            if len(responders) == 0:
                order_counter += 1
                responders[user_id] = {"user_id": user_id, "msg_id": cm_id, "order": order_counter}
                print(f"--> {user_id} первый на Мох, ставлю лайк")
                send_like(cm_id)
                print(f"[{datetime.now():%H:%M:%S}] Мох забрали!")
                collection_active = False

        elif task_lower == "ветки":
            if user_id not in responders:
                if len(responders) < len(LOCATIONS_BRANCHES):
                    order_counter += 1
                    responders[user_id] = {"user_id": user_id, "msg_id": cm_id, "order": order_counter}
                    print(f"--> Записан на Ветки: {user_id}")
                    if len(responders) == len(LOCATIONS_BRANCHES):
                        print(f"[{datetime.now():%H:%M:%S}] Ветки заполнены, досрочно завершаю")
                        finish_collection("Ветки")

        elif task_lower == "травы":
            if user_id not in responders:
                if len(responders) < len(LOCATIONS_HERBS):
                    order_counter += 1
                    responders[user_id] = {"user_id": user_id, "msg_id": cm_id, "order": order_counter}
                    print(f"--> Записан на Травы: {user_id}")
                    if len(responders) == len(LOCATIONS_HERBS):
                        print(f"[{datetime.now():%H:%M:%S}] Травы заполнены, досрочно завершаю")
                        finish_collection("Травы")

        elif task_lower == "охота":
            if user_id not in responders:
                if len(responders) < current_hunt_x:
                    order_counter += 1
                    responders[user_id] = {
                        "user_id": user_id,
                        "msg_id": cm_id,
                        "order": order_counter,
                        "requested": requested
                    }
                    print(f"--> Записан на Охоту: {user_id} (запрошено: {requested})")
                    if len(responders) == current_hunt_x:
                        print(f"[{datetime.now():%H:%M:%S}] Охота заполнена, досрочно завершаю")
                        finish_collection("Охота")


def handle_event(event):
    if event.type != VkBotEventType.MESSAGE_NEW:
        return
    msg = event.object.message
    peer_id = msg["peer_id"]
    user_id = msg["from_id"]
    raw_text = msg.get("text", "").strip()
    if user_id < 0:
        return

    if peer_id == user_id:
        try:
            handle_private_command(user_id, raw_text)
        except Exception as e:
            import traceback
            print(f"[error] Ошибка обработки ЛС-команды: {e}")
            traceback.print_exc()
        return

    if peer_id != PEER_ID:
        return
    if not collection_active:
        return
    try:
        handle_game_message(user_id, raw_text, msg)
    except Exception as e:
        import traceback
        print(f"[error] Ошибка обработки игрового сообщения: {e}")
        traceback.print_exc()


def main_loop():
    global longpoll
    while True:
        try:
            for event in longpoll.listen():
                handle_event(event)
        except requests.exceptions.ReadTimeout:
            continue
        except (requests.exceptions.ConnectionError,
                requests.exceptions.ChunkedEncodingError,
                urllib3.exceptions.ProtocolError) as e:
            print(f"[warn] LongPoll разорван: {e}. Переподключение через 3 сек...")
            time.sleep(3)
            try:
                longpoll = VkBotLongPoll(vk_session, GROUP_ID)
                print("[ok] LongPoll переподключён")
            except Exception as e2:
                print(f"[error] Переподключение не удалось: {e2}")
                time.sleep(5)
        except Exception as e:
            print(f"[error] Неожиданная ошибка LongPoll: {e}. Переподключение через 5 сек...")
            time.sleep(5)
            try:
                longpoll = VkBotLongPoll(vk_session, GROUP_ID)
                print("[ok] LongPoll переподключён")
            except Exception as e2:
                print(f"[error] Переподключение не удалось: {e2}")
                time.sleep(5)


scheduler.add_job(task_branches, "cron", hour=11, minute=0)
scheduler.add_job(task_herbs, "cron", hour=15, minute=0)
scheduler.add_job(task_moss, "cron", hour=16, minute=0)
scheduler.add_job(task_ohota, "cron", hour=12, minute=0)
scheduler.start()
load_participants()
print("Бот запущен и готов к работе!")
main_loop()