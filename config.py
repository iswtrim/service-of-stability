import os
from dotenv import load_dotenv

load_dotenv()

# Функция для загрузки обязательных переменных из .env
def get_required_env(var_name: str) -> str:
    value = os.getenv(var_name)
    if not value:
        raise ValueError(f"Переменная {var_name} не задана в .env")
    return value

# Количество компаний
COMPANIES_COUNT = int(os.getenv("COMPANIES_COUNT", 1))

# Формируем список учётных данных компаний
COMPANIES = []
for i in range(1, COMPANIES_COUNT + 1):
    name = get_required_env(f"COMPANY_NAME_{i}")
    email = get_required_env(f"COMPANY_EMAIL_{i}")
    password = get_required_env(f"COMPANY_PASSWORD_{i}")

    name = name.replace("_", " ")

    COMPANIES.append({
        "name": name,
        "email": email,
        "password": password
    })

# Вебхук Bitrix и ID чата
BITRIX_WEBHOOK = os.getenv("BITRIX_WEBHOOK")
BITRIX_CHAT_ID = os.getenv("BITRIX_CHAT_ID")

# Временной пояс (MSK)
MSK_OFFSET = 3  # UTC+3

# Боевые пороги и статусы (исходящие)
CONGESTION_CHANUNAVAIL_THRESHOLD = 30
NOANSWER_THRESHOLD = 100

SERIES_STATUSES = ["CONGESTION", "CHANUNAVAIL"]
NOANSWER_STATUS = "NOANSWER"
FAILED_STATUS = "FAILED"
TASK_ALERT_STATUS = "TIMEOUT"

# Входящие боты
INCOMING_BOTS = [
    {
        "company_name": "Test_Company_1",  # название компании из .env (без _) !!!
        "botId": "123_456_789",  # идентификатор бота
        "name": "Incoming_Robot_1",  # название робота для уведомлений
        "noanswer_threshold": 10,  # порог для статуса "Нет ответа"
        "count_leads": True  # считать количество лидов за день
    },
    {
        "company_name": "Test_Company_2",
        "botId": "987_654_321",
        "name": "Incoming_Robot_2",
        "noanswer_threshold": 10,
        "count_leads": False
    }
]

# URL API (загружаются из .env)
AUTH_URL = get_required_env("API_AUTH_URL")
TASKS_URL = get_required_env("API_TASKS_URL")
TASK_STATUS_URL = get_required_env("API_TASK_STATUS_URL")
CALLS_URL = get_required_env("API_CALLS_URL")

# Файл состояния
STATE_FILE = "state.json"