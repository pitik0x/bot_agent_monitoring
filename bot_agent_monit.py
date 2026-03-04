#!/usr/bin/env python3
"""
bot_agent_monit.py — Logic utama evaluasi pending list & alert.

File ini HANYA berisi logic. Semua konfigurasi di config.py,
semua fungsi I/O di utils.py.
"""

import time

from config import STATE_FILE, WAIT_HOURS
from utils import (
    get_all_agents,
    load_state,
    logger,
    process_logs,
    save_state,
    send_telegram_alert,
)


def main():
    logger.info("=" * 50)
    logger.info("Bot agent monitoring MULAI dijalankan")

    agents = get_all_agents()
    if not agents:
        logger.warning("Tidak ada agent ditemukan, skip.")
        return

    # Muat state dari file (atau state kosong jika belum ada)
    state = load_state(STATE_FILE)

    current_time = int(time.time())
    
    # Inisialisasi awal agar tidak langsung alert saat script pertama kali jalan
    for agent_id, data in agents.items():
        if agent_id not in state["last_seen_any"]:
            state["last_seen_any"][agent_id] = current_time
        if data["is_sensor"] and agent_id not in state["last_seen_nids"]:
            state["last_seen_nids"][agent_id] = current_time

    # 1. UPDATE PENDING LIST DISCONNECTED
    for agent_id, data in agents.items():
        if agent_id == "000" or data["status"] == "Never connected":
            continue
            
        if data["status"] == "Disconnected":
            # MASUKAN KE LIST: Catat jam berapa mulai putus (jika belum dicatat)
            if agent_id not in state["disconnected_since"]:
                state["disconnected_since"][agent_id] = current_time
                logger.info(f"[PENDING +] {data['name']} ({agent_id}) masuk list disconnected")
        else:
            # HAPUS DARI LIST: Jika sebelum 6 jam dia kembali Active, hapus dari daftar!
            if agent_id in state["disconnected_since"]:
                logger.info(f"[PENDING -] {data['name']} ({agent_id}) kembali Active, dihapus dari list")
                del state["disconnected_since"][agent_id]

    # 2. UPDATE PENDING LIST LOGS
    state = process_logs(state)

    alert_messages = []
    
    # --- EVALUASI PENDING LIST (Tembak ke Telegram jika > 6 Jam) ---
    for agent_id, data in agents.items():
        if agent_id == "000" or data["status"] == "Never connected":
            continue

        is_sensor = data["is_sensor"]
        prefix = "NIDS" if is_sensor else "AGENT"
        icon = "🔴" if is_sensor else "⚠️"
        
        last_alert_time = state["last_alert_sent"].get(agent_id, 0)
        can_send_alert = (current_time - last_alert_time) / 3600 >= WAIT_HOURS
        is_still_error = False

        # Cek Kondisi 1: Disconnected (Apakah sudah mengendap > 6 jam di Pending List?)
        if agent_id in state["disconnected_since"]:
            hours_disconnected = (current_time - state["disconnected_since"][agent_id]) / 3600
            if hours_disconnected >= WAIT_HOURS:
                is_still_error = True
                if can_send_alert:
                    alert_messages.append(f"{icon} *{prefix} ALERT: Disconnected*\n"
                                          f"├ Name: `{data['name']}`\n"
                                          f"├ ID: `{agent_id}`\n"
                                          f"└ Issue: Disconnected for `{hours_disconnected:.1f} Hours`.")
        
        # Cek Kondisi 2: Not Sending Events (Hanya dievaluasi jika status jaringannya Active)
        elif data["status"] == "Active":
            # Cek NIDS (Suricata)
            if is_sensor:
                hours_no_nids = (current_time - state["last_seen_nids"].get(agent_id, current_time)) / 3600
                if hours_no_nids >= WAIT_HOURS:
                    is_still_error = True
                    if can_send_alert:
                        alert_messages.append(f"{icon} *{prefix} ALERT: Not sending events*\n"
                                              f"├ Name: `{data['name']}`\n"
                                              f"├ ID: `{agent_id}`\n"
                                              f"└ Issue: No Suricata events for `{hours_no_nids:.1f} Hours`.")
            # Cek Agent Biasa (Wazuh)
            else:
                hours_no_wazuh = (current_time - state["last_seen_any"].get(agent_id, current_time)) / 3600
                if hours_no_wazuh >= WAIT_HOURS:
                    is_still_error = True
                    if can_send_alert:
                        alert_messages.append(f"⚠️ *AGENT ALERT: Not sending events*\n"
                                              f"├ Name: `{data['name']}`\n"
                                              f"├ ID: `{agent_id}`\n"
                                              f"└ Issue: No Wazuh events for `{hours_no_wazuh:.1f} Hours`.")

        # Update status Spam Control
        if is_still_error:
            if can_send_alert:
                state["last_alert_sent"][agent_id] = current_time # Catat kapan dikirim agar tidak spam
        else:
            # Jika agent sudah sehat (tidak error apapun), pastikan dia bersih dari daftar spam
            if agent_id in state["last_alert_sent"]:
                del state["last_alert_sent"][agent_id]

    # Simpan kembali state ke file
    save_state(STATE_FILE, state)

    # Kirim ke Telegram 
    if alert_messages:
        logger.info(f"ALERT DIKIRIM: {len(alert_messages)} issue ditemukan")
        full_message = "\n\n".join(alert_messages)
        send_telegram_alert(full_message)
    else:
        logger.info("Tidak ada alert — semua agent sehat atau masih dalam masa tunggu")

    logger.info("Bot agent monitoring SELESAI")

if __name__ == "__main__":
    main()