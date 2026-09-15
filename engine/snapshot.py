#!/usr/bin/env python3
"""
snapshot — копия базы WhatsApp в transit/. Оригинал не трогается.

Копия делается штатным механизмом SQLite (backup): в неё попадает и то, что
WhatsApp ещё держит в журнале. Рядом ложится LID.sqlite, если есть (по нему
сопоставляются участники групп без номера), и snapshot.txt — когда снято,
откуда и какого числа последнее сообщение. Медиафайлы не копируются.
transit/ закрыт от git.

Запуск из корня базы:
    python3 engine/snapshot.py
    python3 engine/snapshot.py --db /путь/к/ChatStorage.sqlite --out /куда
"""
import argparse
import os
import shutil
import sqlite3
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from probe import (ACCESS_HINT, CORE_DATA_EPOCH, STALE_DAYS, days_ago, die,  # noqa: E402
                   open_readonly, resolve_db, to_date)

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def copy_db(src_path, dst_path):
    """Согласованная копия через backup; не вышло — копия файлами вместе с журналом."""
    src = open_readonly(src_path)
    try:
        dst = sqlite3.connect(dst_path)
        try:
            src.backup(dst)
        finally:
            dst.close()
        return "backup"
    except sqlite3.Error:
        for suffix in ("", "-wal", "-shm"):
            if os.path.exists(src_path + suffix):
                shutil.copy2(src_path + suffix, dst_path + suffix)
        return "файлами"
    finally:
        src.close()


def summary(db_path):
    high = datetime.now(timezone.utc).timestamp() - CORE_DATA_EPOCH + 86400
    conn = sqlite3.connect(db_path)
    try:
        chats = conn.execute("SELECT COUNT(*) FROM ZWACHATSESSION").fetchone()[0]
        messages, last = conn.execute(
            "SELECT COUNT(*), MAX(CASE WHEN ZMESSAGEDATE < ? THEN ZMESSAGEDATE END) FROM ZWAMESSAGE",
            (high,)).fetchone()
    finally:
        conn.close()
    return chats, messages, last


def main():
    parser = argparse.ArgumentParser(description="Копия базы WhatsApp в transit/ (оригинал не трогается).")
    parser.add_argument("--db", help="путь к ChatStorage.sqlite (по умолчанию — WhatsApp для Mac)")
    parser.add_argument("--out", default=os.path.join(BASE, "transit"),
                        help="куда класть снимок (по умолчанию transit/ базы)")
    args = parser.parse_args()

    src = resolve_db(args.db)
    now = datetime.now()
    target = os.path.join(args.out, f"whatsapp-{now:%Y-%m-%d-%H%M}")
    if os.path.exists(target):
        target += f"{now:%S}"
    os.makedirs(target, mode=0o700)

    snapshot_db = os.path.join(target, "ChatStorage.sqlite")
    try:
        how = copy_db(src, snapshot_db)
    except (sqlite3.Error, OSError) as e:
        shutil.rmtree(target, ignore_errors=True)
        die(f"Снимок не снят: {e}\n{ACCESS_HINT}")

    lid_src = os.path.join(os.path.dirname(src), "LID.sqlite")
    lid_note = "нет у источника"
    if os.path.exists(lid_src):
        try:
            copy_db(lid_src, os.path.join(target, "LID.sqlite"))
            lid_note = "скопирован"
        except (sqlite3.Error, OSError) as e:
            lid_note = f"не скопирован: {e}"

    chats, messages, last = summary(snapshot_db)
    lines = [
        f"снято: {now:%Y-%m-%d %H:%M}",
        f"источник: {src}",
        f"способ: {how}",
        f"чатов: {chats} · сообщений: {messages}",
        f"последнее сообщение: {to_date(last)}",
        f"LID.sqlite: {lid_note}",
    ]
    with open(os.path.join(target, "snapshot.txt"), "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")

    print(f"# snapshot → {target}")
    print("\n".join(lines))
    age = days_ago(last)
    if age is not None and age >= STALE_DAYS:
        print(f"⚠ последнее сообщение {age} дн. назад — открой WhatsApp на Маке, дай подтянуть "
              f"новое и сними заново")


if __name__ == "__main__":
    main()
