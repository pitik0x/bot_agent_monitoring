#!/usr/bin/env python3
"""
utils.py — Fungsi utilitas (I/O, Telegram, parsing agent & log).

Dipisahkan agar bot_agent_monit.py hanya berisi logic evaluasi,
sehingga ringan dan mudah di-maintain.
"""

import json
import os
import subprocess
import time

import requests

from config import (
    TELEGRAM_TOKEN,
    TELEGRAM_CHAT_ID,
    LOG_FILE,
    AGENT_CONTROL_BIN,
)


# ── Telegram ────────────────────────────────────────────────────────────────
def send_telegram_alert(message):
    """Kirim pesan Markdown ke Telegram. Gagal = print error, tidak crash."""
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": message, "parse_mode": "Markdown"}
    try:
        requests.post(url, json=payload, timeout=10)
    except Exception as e:
        print(f"Gagal mengirim alert ke Telegram: {e}")


# ── Agent Control ───────────────────────────────────────────────────────────
def get_all_agents():
    """Parse output agent_control -l menjadi dict {agent_id: {name, status, is_sensor}}."""
    try:
        result = subprocess.run(
            [AGENT_CONTROL_BIN, "-l"], capture_output=True, text=True
        )
        agents = {}
        for line in result.stdout.split("\n"):
            if "ID:" in line and "Name:" in line:
                parts = line.split(", ")
                agent_id = parts[0].split(": ")[1].strip()
                agent_name = parts[1].split(": ")[1].strip()
                status = parts[3].strip()

                is_sensor = (
                    "sensor" in agent_name.lower()
                    or "sesnsor" in agent_name.lower()
                )

                agents[agent_id] = {
                    "name": agent_name,
                    "status": status,
                    "is_sensor": is_sensor,
                }
        return agents
    except Exception as e:
        print(f"Error menjalankan agent_control: {e}")
        return {}


# ── Log Processing ──────────────────────────────────────────────────────────
def process_logs(state):
    """Baca log alerts.json secara incremental, update last_seen_any & last_seen_nids."""
    if not os.path.exists(LOG_FILE):
        return state

    current_inode = os.stat(LOG_FILE).st_ino
    current_size = os.path.getsize(LOG_FILE)

    # Deteksi log rotation (inode berubah atau file mengecil)
    if state.get("inode") != current_inode or current_size < state.get("offset", 0):
        state["offset"] = 0
        state["inode"] = current_inode

    current_time = int(time.time())

    with open(LOG_FILE, "r") as f:
        f.seek(state["offset"])
        for line in f:
            try:
                log = json.loads(line)
                agent_id = log.get("agent", {}).get("id")

                if agent_id:
                    # Setiap kali log masuk, reset timer no-log
                    state["last_seen_any"][agent_id] = current_time
                    if log.get("location") == "/var/log/suricata/eve.json":
                        state["last_seen_nids"][agent_id] = current_time
            except Exception:
                continue

        state["offset"] = f.tell()

    return state


# ── State I/O ───────────────────────────────────────────────────────────────
def load_state(state_file):
    """Muat state dari file JSON. Jika belum ada, return state kosong."""
    default = {
        "offset": 0,
        "inode": None,
        "last_seen_any": {},
        "last_seen_nids": {},
        "disconnected_since": {},
        "last_alert_sent": {},
    }
    if os.path.exists(state_file):
        with open(state_file, "r") as f:
            return json.load(f)
    return default


def save_state(state_file, state):
    """Simpan state ke file JSON."""
    with open(state_file, "w") as f:
        json.dump(state, f)
