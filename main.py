import pytz
import time
from datetime import datetime, timedelta, time as dt_time

import json
import logging
from typing import Optional, List, Dict, Any

import requests
from requests.exceptions import ConnectionError as ReqConnectionError

import config

# Логирование
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

MSK = pytz.timezone("Europe/Moscow")
UTC = pytz.UTC


def load_state() -> dict:
    """Загружает состояние из JSON-файла. Возвращает пустой словарь при ошибке."""
    try:
        with open(config.STATE_FILE, 'r') as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_state(state: dict) -> None:
    """Сохраняет текущее состояние в JSON-файл."""
    with open(config.STATE_FILE, 'w') as f:
        json.dump(state, f, indent=2, ensure_ascii=False)


def get_token_for_company(session: requests.Session, email: str, password: str) -> str:
    """
    Получает токен авторизации для конкретной компании с кэшированием.

    Сначала проверяет наличие валидного токена в state.json (если ему меньше 11 часов).
    Если токен устарел или отсутствует — запрашивает новый через API.

    Args:
        session (requests.Session): HTTP-сессия для выполнения запроса.
        email (str): Email компании.
        password (str): Пароль компании.

    Returns:
        str: Строка с access_token.

    Raises:
        ValueError: Если API не вернул токен.
    """
    state = load_state()
    now_msk = datetime.now(MSK)

    # Проверяем кэш для этого email
    token_data = state.get("tokens", {}).get(email)
    if token_data:
        token = token_data.get("access_token")
        token_time_str = token_data.get("obtained_at")

        if token and token_time_str:
            token_time = datetime.fromisoformat(token_time_str)

            if (now_msk - token_time).total_seconds() < 11 * 3600:
                logger.info(f"Используем сохранённый токен для {email}")
                return token

    # Запрашиваем новый
    logger.info(f"Запрашиваем новый токен для {email}")
    resp = session.post(
        config.AUTH_URL,
        json={"email": email, "password": password, "ttl": 43200},
        timeout=30
    )
    resp.raise_for_status()
    data = resp.json()
    new_token = data.get("token")
    if not new_token:
        raise ValueError(f"Не удалось получить токен для {email}")

    # Сохраняем в state
    if "tokens" not in state:
        state["tokens"] = {}

    state["tokens"][email] = {
        "access_token": new_token,
        "obtained_at": now_msk.isoformat()
    }
    save_state(state)

    return new_token


def get_last_run_utc() -> datetime:
    """Возвращает время последнего успешного запуска в UTC. Если запусков не было — начало текущего дня по МСК."""
    state = load_state()
    last_str = state.get("last_run_utc")

    if last_str:
        return datetime.fromisoformat(last_str)

    now_msk = datetime.now(MSK)
    start_msk = now_msk.replace(hour=0, minute=0, second=0, microsecond=0)

    return start_msk.astimezone(UTC)


def set_last_run_utc(time_utc: datetime) -> None:
    """Сохраняет время текущего запуска в state.json."""
    state = load_state()
    state["last_run_utc"] = time_utc.isoformat()
    save_state(state)


def send_bitrix_message(session: requests.Session, text: str) -> None:
    """
    Отправляет текстовое сообщение в чат Bitrix24 через вебхук.

    Если вебхук или ID чата не заданы в конфиге, функция молча завершается.
    """
    if not config.BITRIX_WEBHOOK or not config.BITRIX_CHAT_ID:
        return

    url = f"{config.BITRIX_WEBHOOK}/im.message.add"
    params = {"DIALOG_ID": config.BITRIX_CHAT_ID, "MESSAGE": text, "SYSTEM": "Y"}

    try:
        resp = session.get(url, params=params, timeout=30)
        resp.raise_for_status()
        logger.info("Сообщение отправлено в Bitrix")
    except Exception as e:
        logger.error(f"Ошибка отправки в Bitrix: {e}")


def fetch_tasks(token: str, session: requests.Session) -> List[Dict[str, Any]]:
    """
    Получает список исходящих заданий за период (начиная со вчерашнего дня, 8:00 МСК).

    Args:
        token (str): Токен авторизации.
        session (requests.Session): HTTP-сессия.

    Returns:
        List[Dict[str, Any]]: Список словарей с данными заданий.
    """
    now_msk = datetime.now(MSK)
    yesterday = now_msk.date() - timedelta(days=1)
    from_msk = MSK.localize(datetime.combine(yesterday, dt_time(hour=8, minute=0)))
    from_utc = from_msk.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S+00:00")

    headers = {"Authorization": f"Bearer {token}"}
    params = {"from": from_utc}

    resp = session.get(config.TASKS_URL, headers=headers, params=params, timeout=60)
    resp.raise_for_status()
    data = resp.json()
    tasks = data.get("items", [])

    logger.info(f"Получено {len(tasks)} заданий, созданных с {from_msk.isoformat()}")

    return tasks


def get_task_status(token: str, session: requests.Session, task_id: str) -> Optional[str]:
    """
    Получает текущий статус конкретного задания с механизмом повторных попыток (retry).

    Args:
        token (str): Токен авторизации.
        session (requests.Session): HTTP-сессия.
        task_id (str): Уникальный ID задания.

    Returns:
        Optional[str]: Статус задания (например, 'PROCESS', 'TIMEOUT') или None, если получить не удалось.
    """
    url = config.TASK_STATUS_URL.format(task_id=task_id)
    headers = {"Authorization": f"Bearer {token}"}
    params = {"fields": "status"}

    for attempt in range(4):
        try:
            resp = session.get(url, headers=headers, params=params, timeout=120)
            resp.raise_for_status()

            return resp.json().get("status")
        except ReqConnectionError as e:
            wait = (2 ** attempt) * 3
            logger.warning(f"ConnectionError попытка {attempt + 1}/4 статус {task_id}: {e}")

            if attempt < 3:
                time.sleep(wait)
            else:
                raise
        except Exception as e:
            wait = 2 ** attempt
            logger.warning(f"Попытка {attempt + 1}/4 статус {task_id}: {e}")

            if attempt < 3:
                time.sleep(wait)
            else:
                raise

    return None


def fetch_calls(token: str, session: requests.Session, task_id: str, from_utc: datetime) -> List[Dict[str, Any]]:
    """
    Получает все исходящие звонки для конкретного задания за период с пагинацией.

    Args:
        token (str): Токен авторизации.
        session (requests.Session): HTTP-сессия.
        task_id (str): ID задания.
        from_utc (datetime): Время начала периода в UTC.

    Returns:
        List[Dict[str, Any]]: Список словарей с данными звонков.
    """
    headers = {"Authorization": f"Bearer {token}"}
    all_calls = []
    offset = 0
    limit = 1000

    while True:
        params = {
            "fields": "id,startedAt,currentStatusName,number",
            "taskId": task_id,
            "from": from_utc.strftime("%Y-%m-%dT%H:%M:%S+00:00"),
            "sort": "+startedAt",
            "limit": limit,
            "offset": offset
        }
        for attempt in range(3):
            try:
                resp = session.get(config.CALLS_URL, headers=headers, params=params, timeout=120)
                resp.raise_for_status()
                break
            except Exception as e:
                logger.warning(f"Попытка {attempt + 1}/3 звонки {task_id}, offset {offset}: {e}")
                if attempt < 2:
                    time.sleep(5)
                else:
                    raise

        items = resp.json().get("items", [])
        all_calls.extend(items)
        time.sleep(0.5)

        if len(items) < limit:
            break
        offset += limit

    time.sleep(1)
    logger.info(f"Задание {task_id}: получено {len(all_calls)} звонков")

    return all_calls


def fetch_incoming_calls(token: str, session: requests.Session, bot_id: str, from_utc: datetime, fields: str) -> List[
    Dict[str, Any]]:
    """
    Получает входящие звонки конкретного бота за период с пагинацией.

    Args:
        token (str): Токен авторизации.
        session (requests.Session): HTTP-сессия.
        bot_id (str): ID голосового робота.
        from_utc (datetime): Время начала периода в UTC.
        fields (str): Список запрашиваемых полей через запятую.

    Returns:
        List[Dict[str, Any]]: Список словарей с данными звонков.
    """
    headers = {"Authorization": f"Bearer {token}"}
    all_calls = []
    offset = 0
    limit = 1000

    while True:
        params = {
            "fields": fields,
            "botId": bot_id,
            "isIncoming": "true",
            "from": from_utc.strftime("%Y-%m-%dT%H:%M:%S+00:00"),
            "sort": "+startedAt",
            "limit": limit,
            "offset": offset
        }
        for attempt in range(3):
            try:
                resp = session.get(config.CALLS_URL, headers=headers, params=params, timeout=120)
                resp.raise_for_status()
                break
            except Exception as e:
                logger.warning(f"Попытка {attempt + 1}/3 входящие {bot_id}, offset {offset}: {e}")
                if attempt < 2:
                    time.sleep(5)
                else:
                    raise
        items = resp.json().get("items", [])
        all_calls.extend(items)
        time.sleep(0.5)

        if len(items) < limit:
            break

        offset += limit

    time.sleep(1)
    logger.info(f"Входящий бот {bot_id}: получено {len(all_calls)} звонков")

    return all_calls


def _format_msk_time(utc_str: str) -> str:
    """Вспомогательная функция: конвертирует строку UTC в форматированную строку МСК."""
    utc_dt = datetime.fromisoformat(utc_str.replace("Z", "+00:00"))
    msk_dt = utc_dt.astimezone(MSK)
    return msk_dt.strftime("%Y-%m-%d %H:%M:%S")


def _format_msk_datetime(dt: datetime) -> str:
    """Вспомогательная функция: конвертирует объект datetime (UTC) в строку МСК."""
    if dt.tzinfo is None:
        dt = UTC.localize(dt)
    msk_dt = dt.astimezone(MSK)
    return msk_dt.strftime("%Y-%m-%d %H:%M:%S")


def _plural_leads(n: int) -> str:
    """Вспомогательная функция: возвращает строку с правильным склонением слова 'лид'."""
    if 11 <= n % 100 <= 14:
        return f"[B]{n}[/B] лидов"
    last_digit = n % 10
    if last_digit == 1:
        return f"[B]{n}[/B] лид"
    elif 2 <= last_digit <= 4:
        return f"[B]{n}[/B] лида"
    else:
        return f"[B]{n}[/B] лидов"


def analyze_calls(calls: List[Dict[str, Any]]) -> Dict[str, Any]:
    """
    Анализирует список звонков на наличие аномалий (серии ошибок, превышение порогов).

    Делает один проход по списку и возвращает не более одной проблемы каждого типа,
    чтобы не спамить в чат.

    Args:
        calls (List[Dict[str, Any]]): Список звонков.

    Returns:
        Dict[str, Any]: Словарь с найденными проблемами ('series', 'noanswer', 'failed') 
                        или пустой словарь, если проблем нет.
    """
    result = {}
    found_series = False
    found_noanswer = False
    found_failed = False

    current_series = 0
    series_first_time = None
    series_first_number = None
    current_noanswer = 0
    noanswer_first_time = None
    noanswer_first_number = None

    for call in calls:
        status = call.get("currentStatusName")
        started_at = call.get("startedAt")
        number = call.get("number")

        # --- Серия CONGESTION/CHANUNAVAIL ---
        if status in config.SERIES_STATUSES:
            if current_series == 0:
                series_first_time = started_at
                series_first_number = number
            current_series += 1

            current_noanswer = 0
            noanswer_first_time = None
            noanswer_first_number = None
        else:
            if not found_series and current_series >= config.CONGESTION_CHANUNAVAIL_THRESHOLD:
                result["series"] = {
                    "type": "SERIES",
                    "count": current_series,
                    "first_time": _format_msk_time(series_first_time),
                    "first_number": series_first_number
                }
                found_series = True

            current_series = 0
            series_first_time = None
            series_first_number = None

        # --- Серия NOANSWER ---
        if status == config.NOANSWER_STATUS:
            if current_noanswer == 0:
                noanswer_first_time = started_at
                noanswer_first_number = number

            current_noanswer += 1
        else:
            if not found_noanswer and current_noanswer >= config.NOANSWER_THRESHOLD:
                result["noanswer"] = {
                    "type": "NOANSWER",
                    "count": current_noanswer,
                    "first_time": _format_msk_time(noanswer_first_time),
                    "first_number": noanswer_first_number
                }
                found_noanswer = True

            current_noanswer = 0
            noanswer_first_time = None
            noanswer_first_number = None

        # --- FAILED (один звонок) ---
        if not found_failed and status == config.FAILED_STATUS:
            result["failed"] = {
                "type": "FAILED",
                "id": call.get("id"),
                "time": _format_msk_time(started_at),
                "number": number
            }
            found_failed = True

    # Проверка хвостов
    if not found_series and current_series >= config.CONGESTION_CHANUNAVAIL_THRESHOLD:
        result["series"] = {
            "type": "SERIES",
            "count": current_series,
            "first_time": _format_msk_time(series_first_time),
            "first_number": series_first_number
        }
    if not found_noanswer and current_noanswer >= config.NOANSWER_THRESHOLD:
        result["noanswer"] = {
            "type": "NOANSWER",
            "count": current_noanswer,
            "first_time": _format_msk_time(noanswer_first_time),
            "first_number": noanswer_first_number
        }

    return result


def process_incoming(company: dict, session: requests.Session, last_run_utc: datetime) -> bool:
    """
    Проверяет состояние входящих ботов для конкретной компании.

    Отслеживает отсутствие активности и превышение порога неотвеченных звонков.
    Также подсчитывает лиды, если включен флаг count_leads.

    Args:
        company (dict): Данные компании.
        session (requests.Session): HTTP-сессия.
        last_run_utc (datetime): Время последнего запуска.

    Returns:
        bool: True, если был отправлен хотя бы один алерт, иначе False.
    """
    bots = [b for b in config.INCOMING_BOTS if b["company_name"] == company["name"]]
    if not bots:
        return False

    token = get_token_for_company(session, company["email"], company["password"])
    company_alert = False

    now_utc = datetime.now(UTC)
    from_msk_str = _format_msk_datetime(last_run_utc)
    to_msk_str = _format_msk_datetime(now_utc)

    for bot in bots:
        count_leads = bot.get("count_leads", False)
        fields = "id,startedAt,currentStatusName,callingNumber"
        if count_leads:
            fields += ",dialogResult"

        calls = fetch_incoming_calls(token, session, bot["botId"], last_run_utc, fields=fields)

        if count_leads:
            leads_this_run = sum(1 for c in calls if c.get("dialogResult") == "Лид")
            if leads_this_run > 0:
                state = load_state()
                lead_key = f"leads_{bot['botId']}"
                state[lead_key] = state.get(lead_key, 0) + leads_this_run
                save_state(state)
                logger.info(f"Бот {bot['name']}: +{leads_this_run} лидов за интервал, всего сегодня {state[lead_key]}")

        noanswer_count = sum(1 for c in calls if c.get("currentStatusName") == "NOANSWER")
        has_activity = len(calls) > 0

        if not has_activity:
            msg = (
                f"⚠️ Входящая линия: [B]{bot['name']}[/B]\n\n"
                f"За период {from_msk_str} – {to_msk_str} не было ни одного звонка"
            )
            send_bitrix_message(session, msg)
            company_alert = True

        elif noanswer_count >= bot["noanswer_threshold"]:
            first_noanswer = next(c for c in calls if c.get("currentStatusName") == "NOANSWER")
            first_time = _format_msk_time(first_noanswer["startedAt"])
            first_number = first_noanswer.get("callingNumber", "—")
            msg = (
                f"⚠️ Входящая линия: [B]{bot['name']}[/B]\n\n"
                f"За период {from_msk_str} – {to_msk_str} обнаружено {noanswer_count} звонков "
                f"со статусом \"Нет ответа\"\n\n"
                f"Время первого проблемного звонка: {first_time}\n"
                f"Номер абонента: {first_number}"
            )
            send_bitrix_message(session, msg)
            company_alert = True

    return company_alert


def process_company(company: dict, session: requests.Session, last_run_utc: datetime) -> bool:
    """
    Обрабатывает исходящие задания компании: проверяет статусы (TIMEOUT) и анализирует звонки.

    Args:
        company (dict): Данные компании.
        session (requests.Session): HTTP-сессия.
        last_run_utc (datetime): Время последнего запуска.

    Returns:
        bool: True, если был отправлен хотя бы один алерт, иначе False.
    """
    token = get_token_for_company(session, company["email"], company["password"])
    tasks = fetch_tasks(token, session)
    if not tasks:
        return False

    company_alert = False

    for task in tasks:
        task_id = task.get("id")
        task_name = task.get("name") or "Без названия"

        status = get_task_status(token, session, task_id)
        if status == config.TASK_ALERT_STATUS:
            state = load_state()
            timeout_tasks = state.setdefault("alerted_timeout_tasks", {})
            today_str = datetime.now(MSK).strftime("%Y-%m-%d")

            if task_id not in timeout_tasks:
                msg = (
                    f"🚨 Аварийная остановка задания\n\n"
                    f"Название задания: [B]{task_name}[/B]\n"
                    f"ID задания: {task_id}\n\n"
                    f"Статус: TIMEOUT"
                )
                send_bitrix_message(session, msg)
                timeout_tasks[task_id] = today_str
                save_state(state)
                logger.info(f"Задание {task_id} добавлено в alerted_timeout_tasks")
                company_alert = True
            elif timeout_tasks[task_id] == today_str:
                logger.info(f"Повторный TIMEOUT сегодня для задания {task_id}, сообщение не отправлено")
                company_alert = True
            else:
                logger.info(f"Старый TIMEOUT для задания {task_id}, игнорируем")
            continue

        calls = fetch_calls(token, session, task_id, last_run_utc)
        if not calls:
            continue

        problems = analyze_calls(calls)
        if not problems:
            continue

        if "series" in problems:
            company_alert = True
            p = problems["series"]
            msg = (
                f"⚠️ Обнаружена серия из {p['count']} звонков подряд со статусами "
                f"\"Вызов отклонен оператором связи\" / \"Нет связи с телефонной линией\"\n\n"
                f"Название задания: [B]{task_name}[/B]\n"
                f"ID задания: {task_id}\n\n"
                f"Время первого проблемного звонка: {p['first_time']}\n"
                f"Номер абонента: {p['first_number']}"
            )
            send_bitrix_message(session, msg)

        if "noanswer" in problems:
            company_alert = True
            p = problems["noanswer"]
            msg = (
                f"⚠️ Обнаружена серия из {p['count']} звонков подряд со статусом \"Нет ответа\"\n\n"
                f"Название задания: [B]{task_name}[/B]\n"
                f"ID задания: {task_id}\n\n"
                f"Время первого проблемного звонка: {p['first_time']}\n"
                f"Номер абонента: {p['first_number']}"
            )
            send_bitrix_message(session, msg)

        if "failed" in problems:
            company_alert = True
            p = problems["failed"]
            msg = (
                f"❌ Обнаружен звонок со статусом \"Ошибка\"\n\n"
                f"Название задания: [B]{task_name}[/B]\n"
                f"ID задания: {task_id}\n\n"
                f"ID звонка: {p['id']}\n"
                f"Время звонка: {p['time']}\n"
                f"Номер абонента: {p['number']}"
            )
            send_bitrix_message(session, msg)

    return company_alert


def get_call_count(token: str, session: requests.Session, task_id: str) -> Optional[int]:
    """Возвращает общее количество звонков в задании за всё время (из статистики)."""
    url = config.CALLS_URL
    headers = {"Authorization": f"Bearer {token}"}
    params = {
        "taskId": task_id,
        "limit": 1,
        "fields": "id"
    }
    try:
        resp = session.get(url, headers=headers, params=params, timeout=60)
        resp.raise_for_status()
        data = resp.json()
        return data.get("count")
    except Exception as e:
        logger.warning(f"Не удалось получить общее количество звонков для задания {task_id}: {e}")
        return None


def send_evening_summary(company: dict, session: requests.Session, has_incoming_bots: bool) -> None:
    """
    Формирует и отправляет вечернюю сводку по компании, если за день не было алертов.

    Args:
        company (dict): Данные компании.
        session (requests.Session): HTTP-сессия.
        has_incoming_bots (bool): Есть ли у компании входящие боты.
    """
    token = get_token_for_company(session, company["email"], company["password"])
    tasks = fetch_tasks(token, session)
    active = []

    for t in tasks:
        st = get_task_status(token, session, t["id"])
        if st == "PROCESS":
            total_calls = get_call_count(token, session, t["id"])

            if total_calls is not None:
                active.append((t.get("name"), t["id"], total_calls))
            else:
                active.append((t.get("name"), t["id"], "не удалось узнать"))

    active_lines = []
    for item in active:
        name, task_id, calls = item
        active_lines.append(f"- {name} (ID: {task_id}), звонков: [B]{calls}[/B]")

    active_list = "\n".join(active_lines) if active_lines else "Нет активных заданий"

    msg = (
        f"✅ Последняя проверка по компании [B]{company['name']}[/B] завершена, проблем не обнаружено.\n\n"
        f"[B]Активные задания:[/B]\n{active_list}"
    )

    if has_incoming_bots:
        msg += "\n\nВо входящей линии также [B]проблем не обнаружено[/B]."

    send_bitrix_message(session, msg)


def main():
    """
    Точка входа. Запускает полную проверку по всем компаниям.

    В 17:00 МСК формирует и отправляет вечернюю сводку (если не было алертов).
    """
    session = requests.Session()
    now_utc = datetime.now(UTC)
    last_run_utc = get_last_run_utc()
    now_msk = datetime.now(MSK)

    companies_without_alerts = []

    for comp in config.COMPANIES:
        try:
            alert_company = process_company(comp, session, last_run_utc)
            alert_incoming = process_incoming(comp, session, last_run_utc)
            if not alert_company and not alert_incoming:
                companies_without_alerts.append(comp)
        except Exception as e:
            logger.exception(f"Ошибка обработки компании {comp['name']}")
            send_bitrix_message(session, f"🔴 Ошибка при обработке компании {comp['name']}: {e}")

    if now_msk.hour == 17:
        time.sleep(3)

        # Вечерняя сводка (только для компаний без алертов)
        for comp in companies_without_alerts:
            try:
                has_incoming = any(b["company_name"] == comp["name"] for b in config.INCOMING_BOTS)
                send_evening_summary(comp, session, has_incoming)
            except Exception as e:
                logger.error(f"Не удалось отправить сводку для {comp['name']}: {e}")
                send_bitrix_message(session, f"🔴 Ошибка вечерней сводки для {comp['name']}: {e}")

        # Итог по лидам (для всех компаний, где есть боты с count_leads)
        for comp in config.COMPANIES:
            for bot in config.INCOMING_BOTS:
                if bot["company_name"] == comp["name"] and bot.get("count_leads"):
                    try:
                        state = load_state()
                        lead_key = f"leads_{bot['botId']}"
                        total_leads = state.pop(lead_key, 0)
                        save_state(state)
                        format_leads = _plural_leads(total_leads)

                        if total_leads == 1:
                            msg = f"🤖 [B]Входящие {bot['name']}[/B]: сегодня получен {format_leads}"
                        else:
                            msg = f"🤖 [B]Входящие {bot['name']}[/B]: сегодня получено {format_leads}"

                        send_bitrix_message(session, msg)
                    except Exception as e:
                        logger.error(f"Ошибка отправки лидов для {comp['name']}: {e}")

    set_last_run_utc(now_utc)
    logger.info("Проверка завершена успешно")


if __name__ == "__main__":
    main()
