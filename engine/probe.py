#!/usr/bin/env python3
"""
probe — первые цифры по WhatsApp на этом Маке. Только чтение, только счёт.

Открывает базу WhatsApp для Mac строго на чтение и печатает цифры: сколько
чатов и групп, сколько сообщений и за какой срок, сколько людей в группах,
сколько раз отправлялись условия и сколько пришло анкет. Текст сообщений не
печатается. Названия групп — только с флагом --groups.

Запуск из корня базы:
    python3 engine/probe.py
    python3 engine/probe.py --groups
    python3 engine/probe.py --db /путь/к/ChatStorage.sqlite
"""
import argparse
import os
import sqlite3
import sys
from datetime import datetime, timezone
from urllib.parse import quote

CONTAINERS = {
    "WhatsApp": "~/Library/Group Containers/group.net.whatsapp.WhatsApp.shared",
    "WhatsApp Business": "~/Library/Group Containers/group.net.whatsapp.WhatsAppSMB.shared",
}
CORE_DATA_EPOCH = 978307200  # даты в базе WhatsApp считаются от 2001-01-01 UTC
SESSION_USER, SESSION_GROUP = 0, 1
STALE_DAYS = 2

# Метки анкеты Айгуль (meta-vault/business/анкета.md), без эмодзи.
ANKETA_LABELS = ("Имя:", "Город:", "Возраст:", "Рост", "Образование:", "Профессия",
                 "Семейное положение:", "Кого ищу", "Цель знакомства:", "Интересы",
                 "Фото", "переезд", "о себе")
ANKETA_MIN_LABELS = 7
CONDITIONS_MARKERS = ("Условия участия", "Қатысу шарттары")

ACCESS_HINT = (
    "Скорее всего macOS не дала доступ к данным WhatsApp.\n"
    "Когда macOS спросит про доступ к данным другого приложения — разреши.\n"
    "Не спросила или отказано: Системные настройки → Конфиденциальность и безопасность →\n"
    "Полный доступ к диску → включи приложение, из которого запускаешь (Claude или Терминал),\n"
    "и перезапусти его.")


def die(message):
    print(message, file=sys.stderr)
    sys.exit(1)


def resolve_db(arg):
    if arg:
        path = os.path.expanduser(arg)
    else:
        path = os.path.join(os.path.expanduser(CONTAINERS["WhatsApp"]), "ChatStorage.sqlite")
    if os.path.exists(path):
        return path
    lines = [f"База WhatsApp не найдена: {path}"]
    if not arg:
        lines.append("Проверь: WhatsApp для Mac установлен, привязан к телефону и хотя бы раз открыт.")
        business = os.path.join(os.path.expanduser(CONTAINERS["WhatsApp Business"]), "ChatStorage.sqlite")
        if os.path.exists(business) and os.path.getsize(business) > 0:
            lines.append(f'Есть база WhatsApp Business — если на Маке стоит он: --db "{business}"')
    die("\n".join(lines))


def open_readonly(path):
    """SQLite открывает файл в режиме ro: запись в базу WhatsApp невозможна.

    WhatsApp закрыт и журнала рядом нет — файл открывается как неизменяемый:
    иначе ro-подключению пришлось бы создать рядом служебный -shm.
    """
    uri = f"file:{quote(path)}?mode=ro"
    try:
        conn = sqlite3.connect(uri, uri=True, timeout=5)
        conn.execute("SELECT count(*) FROM sqlite_master").fetchone()
        return conn
    except sqlite3.OperationalError:
        if os.path.exists(path + "-wal"):
            raise
    conn = sqlite3.connect(uri + "&immutable=1", uri=True, timeout=5)
    conn.execute("SELECT count(*) FROM sqlite_master").fetchone()
    return conn


def connect(path):
    try:
        return open_readonly(path)
    except (sqlite3.Error, OSError) as e:
        die(f"Не открылась база: {e}\n{ACCESS_HINT}")


def tables(conn):
    return {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}


def columns(conn, table):
    return [r[1] for r in conn.execute(f'PRAGMA table_info("{table}")')]


def to_datetime(ts):
    if ts is None:
        return None
    if ts > 4e9:  # редкие строки хранят миллисекунды
        ts /= 1000
    try:
        return datetime.fromtimestamp(ts + CORE_DATA_EPOCH, tz=timezone.utc).astimezone()
    except (ValueError, OSError, OverflowError):
        return None


def to_date(ts):
    dt = to_datetime(ts)
    return dt.strftime("%Y-%m-%d") if dt else "—"


def days_ago(ts):
    dt = to_datetime(ts)
    return (datetime.now(timezone.utc) - dt).days if dt else None


def date_window():
    """Окно правдоподобных дат — битые строки не портят первую и последнюю дату."""
    low = datetime(2009, 1, 1, tzinfo=timezone.utc).timestamp() - CORE_DATA_EPOCH
    high = datetime.now(timezone.utc).timestamp() - CORE_DATA_EPOCH + 86400
    return low, high


def alive(conn, alias=""):
    prefix = f"{alias}." if alias else ""
    return f"COALESCE({prefix}ZREMOVED, 0) = 0" if "ZREMOVED" in columns(conn, "ZWACHATSESSION") else "1"


def member_columns(conn):
    """Колонки таблицы участников групп ищутся по имени: схема WhatsApp меняется."""
    if "ZWAGROUPMEMBER" not in tables(conn):
        return None, None
    cols = columns(conn, "ZWAGROUPMEMBER")
    chat = next((c for c in cols if "CHATSESSION" in c), None)
    jid = "ZMEMBERJID" if "ZMEMBERJID" in cols else next((c for c in cols if "JID" in c), None)
    return chat, jid


def count_chats(conn):
    kinds = dict(conn.execute(
        f"SELECT ZSESSIONTYPE, COUNT(*) FROM ZWACHATSESSION WHERE {alive(conn)} GROUP BY ZSESSIONTYPE"))
    other = sum(n for kind, n in kinds.items() if kind not in (SESSION_USER, SESSION_GROUP))
    return kinds.get(SESSION_USER, 0), kinds.get(SESSION_GROUP, 0), other


def count_messages(conn):
    low, high = date_window()
    total, mine = conn.execute(
        "SELECT COUNT(*), COALESCE(SUM(ZISFROMME = 1), 0) FROM ZWAMESSAGE").fetchone()
    first, last = conn.execute(
        "SELECT MIN(ZMESSAGEDATE), MAX(ZMESSAGEDATE) FROM ZWAMESSAGE "
        "WHERE ZMESSAGEDATE BETWEEN ? AND ?", (low, high)).fetchone()
    by_kind = dict(conn.execute(
        "SELECT s.ZSESSIONTYPE, COUNT(*) FROM ZWAMESSAGE m "
        "JOIN ZWACHATSESSION s ON s.Z_PK = m.ZCHATSESSION GROUP BY s.ZSESSIONTYPE"))
    return total, mine, first, last, by_kind


def count_members(conn):
    chat, jid = member_columns(conn)
    if not (chat and jid):
        return None
    return conn.execute(
        f"SELECT COUNT(*), COUNT(DISTINCT {jid}), "
        f"COUNT(DISTINCT CASE WHEN {jid} LIKE '%@lid' THEN {jid} END) FROM ZWAGROUPMEMBER").fetchone()


def count_texts(conn):
    marker_sql = " OR ".join("m.ZTEXT LIKE ?" for _ in CONDITIONS_MARKERS)
    conditions = conn.execute(
        f"SELECT COUNT(*), COUNT(DISTINCT m.ZCHATSESSION) FROM ZWAMESSAGE m "
        f"WHERE m.ZISFROMME = 1 AND ({marker_sql})",
        [f"%{marker}%" for marker in CONDITIONS_MARKERS]).fetchone()

    anketas = {"пришли в личку": 0, "ушли от вас в личку": 0, "в группах": 0}
    incoming_chats = set()
    rows = conn.execute(
        "SELECT m.ZISFROMME, s.ZSESSIONTYPE, m.ZCHATSESSION, m.ZTEXT FROM ZWAMESSAGE m "
        "JOIN ZWACHATSESSION s ON s.Z_PK = m.ZCHATSESSION "
        "WHERE m.ZTEXT LIKE '%Кого ищу%' OR m.ZTEXT LIKE '%Семейное положение%'")
    for from_me, kind, chat, text in rows:
        if sum(label in text for label in ANKETA_LABELS) < ANKETA_MIN_LABELS:
            continue
        if kind == SESSION_GROUP:
            anketas["в группах"] += 1
        elif from_me:
            anketas["ушли от вас в личку"] += 1
        else:
            anketas["пришли в личку"] += 1
            incoming_chats.add(chat)
    return conditions, anketas, len(incoming_chats)


def list_groups(conn):
    chat, _ = member_columns(conn)
    members = f"(SELECT COUNT(*) FROM ZWAGROUPMEMBER g WHERE g.{chat} = s.Z_PK)" if chat else "NULL"
    return conn.execute(
        f"SELECT s.ZPARTNERNAME, s.ZLASTMESSAGEDATE, {members}, "
        f"(SELECT COUNT(*) FROM ZWAMESSAGE m WHERE m.ZCHATSESSION = s.Z_PK) "
        f"FROM ZWACHATSESSION s WHERE s.ZSESSIONTYPE = {SESSION_GROUP} AND {alive(conn, 's')} "
        f"ORDER BY s.ZLASTMESSAGEDATE DESC").fetchall()


def file_size_mb(path):
    return sum(os.path.getsize(path + s) for s in ("", "-wal") if os.path.exists(path + s)) / 1e6


def main():
    parser = argparse.ArgumentParser(description="Первые цифры по WhatsApp на этом Маке (только чтение).")
    parser.add_argument("--db", help="путь к ChatStorage.sqlite (по умолчанию — WhatsApp для Mac)")
    parser.add_argument("--groups", action="store_true", help="показать группы: название, людей, сообщений")
    args = parser.parse_args()

    path = resolve_db(args.db)
    conn = connect(path)
    missing = {"ZWACHATSESSION", "ZWAMESSAGE"} - tables(conn)
    if missing:
        die(f"Это не база WhatsApp: нет таблиц {', '.join(sorted(missing))}")

    print("# probe — WhatsApp на этом Маке")
    print(f"снято: {datetime.now():%Y-%m-%d %H:%M} · база: {path} · {file_size_mb(path):.1f} МБ")

    users, groups, other = count_chats(conn)
    print("\n## Чаты")
    print(f"личных: {users} · групп: {groups} · прочих (рассылки, каналы): {other}")

    total, mine, first, last, by_kind = count_messages(conn)
    print("\n## Сообщения")
    print(f"всего: {total} · исходящих: {mine} · входящих: {total - mine}")
    print(f"в личных: {by_kind.get(SESSION_USER, 0)} · в группах: {by_kind.get(SESSION_GROUP, 0)}")
    print(f"с {to_date(first)} по {to_date(last)}")
    age = days_ago(last)
    if age is not None and age >= STALE_DAYS:
        print(f"⚠ последнее сообщение {age} дн. назад — WhatsApp на Маке мог быть закрыт "
              f"или ещё подтягивает историю")
    if "ZWAMEDIAITEM" in tables(conn):
        media = conn.execute("SELECT COUNT(*) FROM ZWAMEDIAITEM").fetchone()[0]
        print(f"медиа (фото, голосовые, файлы): {media}")

    print("\n## Люди в группах")
    members = count_members(conn)
    if members is None:
        print("таблицы участников групп нет или она устроена иначе — состав групп так не посчитать")
    else:
        rows, people, hidden = members
        print(f"записей: {rows} · разных людей: {people} · из них без номера: {hidden}")
    lid = os.path.join(os.path.dirname(path), "LID.sqlite")
    print(f"файл сопоставления людей без номера (LID.sqlite): {'есть' if os.path.exists(lid) else 'нет'}")

    (sent, sent_chats), anketas, incoming_chats = count_texts(conn)
    print("\n## Условия и анкеты")
    print(f"условия отправлены: {sent} · в чатах: {sent_chats}")
    print(" · ".join(f"{k}: {v}" for k, v in anketas.items())
          + f" (входящие — от {incoming_chats} человек)")

    if args.groups:
        print("\n## Группы")
        for name, last_ts, people_in, messages in list_groups(conn):
            people_text = "—" if people_in is None else people_in
            print(f"- {name or '?'} · людей: {people_text} · сообщений: {messages} · последнее: {to_date(last_ts)}")

    print("\n## Дальше")
    print("Цифры — в meta-vault/sources.md §2 с датой.")
    print("Форма чаты/data/: сотни людей — файлы, тысячи — SQLite (чаты/чаты-vault/зона-данных.md).")
    conn.close()


if __name__ == "__main__":
    main()
