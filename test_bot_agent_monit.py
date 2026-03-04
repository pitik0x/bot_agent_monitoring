#!/usr/bin/env python3
"""
Unit tests untuk bot_agent_monit.py (versi refactored: config.py + utils.py)

Flow yang diuji:
1. "Disimpan terlebih dahulu"  — Saat agent Disconnected, TIDAK langsung kirim Telegram.
   Dicatat dulu ke pending list (disconnected_since). Begitu juga log macet, timer tidak bertambah.

2. "Jika sebelum 6 jam kembali connect, dihapus dari list" — Saat agent kembali Active
   sebelum 6 jam, agent dihapus dari pending list dan timer last_seen di-reset.

3. "Setelah 6 jam baru dikirim listnya" — Jika agent masih Disconnected/bermasalah
   selama >= 6 jam, baru dikirim alert ke Telegram.
"""

import json
import os
import sys
import time
import unittest
from unittest.mock import patch, MagicMock, mock_open, call

# Import modul yang akan ditest
import bot_agent_monit
import utils


# ─── Helper: state kosong (fresh) ──────────────────────────────────────────
def fresh_state():
    return {
        "offset": 0,
        "inode": None,
        "last_seen_any": {},
        "last_seen_nids": {},
        "disconnected_since": {},
        "last_alert_sent": {},
    }


# ─── Helper: agent dummy ──────────────────────────────────────────────────
def make_agents(overrides=None):
    """Return dict agent standar. Override status per agent_id via dict."""
    base = {
        "001": {"name": "server-web-01", "status": "Active", "is_sensor": False},
        "002": {"name": "sensor-ids-01", "status": "Active", "is_sensor": True},
        "003": {"name": "server-db-01", "status": "Active", "is_sensor": False},
    }
    if overrides:
        for aid, fields in overrides.items():
            if aid in base:
                base[aid].update(fields)
            else:
                base[aid] = fields
    return base


# ═══════════════════════════════════════════════════════════════════════════
# TEST CLASS: FLOW 1 — "Disimpan terlebih dahulu"
# ═══════════════════════════════════════════════════════════════════════════
class TestFlow1_DisimpanDulu(unittest.TestCase):
    """
    Saat agent Disconnected terdeteksi, script harus:
    - Memasukkan agent ke pending list (disconnected_since)
    - TIDAK mengirim Telegram karena belum 6 jam
    """

    @patch("bot_agent_monit.send_telegram_alert")
    @patch("bot_agent_monit.process_logs", side_effect=lambda s: s)
    @patch("bot_agent_monit.get_all_agents")
    @patch("bot_agent_monit.save_state")
    @patch("bot_agent_monit.load_state")
    def test_disconnected_masuk_pending_list_tanpa_kirim_telegram(
        self, mock_load, mock_save, mock_agents, mock_logs, mock_telegram
    ):
        """Jam 10:00 agent disconnected → dicatat, Telegram TIDAK terkirim."""
        JAM_10 = 1000000000

        agents = make_agents({"001": {"status": "Disconnected"}})
        mock_agents.return_value = agents
        mock_load.return_value = fresh_state()

        with patch("bot_agent_monit.time.time", return_value=JAM_10):
            bot_agent_monit.main()

        # ASSERT: Telegram TIDAK dipanggil
        mock_telegram.assert_not_called()

        # ASSERT: State yang disimpan punya agent 001 di disconnected_since
        saved_state = mock_save.call_args[0][1]
        self.assertIn("001", saved_state["disconnected_since"])
        self.assertEqual(saved_state["disconnected_since"]["001"], JAM_10)

    @patch("bot_agent_monit.send_telegram_alert")
    @patch("bot_agent_monit.process_logs", side_effect=lambda s: s)
    @patch("bot_agent_monit.get_all_agents")
    @patch("bot_agent_monit.save_state")
    @patch("bot_agent_monit.load_state")
    def test_disconnected_3_jam_masih_belum_kirim_telegram(
        self, mock_load, mock_save, mock_agents, mock_logs, mock_telegram
    ):
        """Jam 13:00 (3 jam setelah disconnect) — masih pending, belum kirim."""
        JAM_10 = 1000000000
        JAM_13 = JAM_10 + (3 * 3600)

        agents = make_agents({"001": {"status": "Disconnected"}})
        mock_agents.return_value = agents

        state = fresh_state()
        state["last_seen_any"] = {"001": JAM_10, "002": JAM_10, "003": JAM_10}
        state["last_seen_nids"] = {"002": JAM_10}
        state["disconnected_since"] = {"001": JAM_10}
        mock_load.return_value = state

        with patch("bot_agent_monit.time.time", return_value=JAM_13):
            bot_agent_monit.main()

        # ASSERT: Telegram tetap TIDAK dipanggil (baru 3 jam)
        mock_telegram.assert_not_called()

        saved_state = mock_save.call_args[0][1]
        self.assertIn("001", saved_state["disconnected_since"])

    @patch("bot_agent_monit.send_telegram_alert")
    @patch("bot_agent_monit.process_logs", side_effect=lambda s: s)
    @patch("bot_agent_monit.get_all_agents")
    @patch("bot_agent_monit.save_state")
    @patch("bot_agent_monit.load_state")
    def test_log_macet_timer_tidak_update(
        self, mock_load, mock_save, mock_agents, mock_logs, mock_telegram
    ):
        """Jika tidak ada log masuk, last_seen_any TIDAK bertambah/di-reset."""
        JAM_10 = 1000000000
        JAM_11 = JAM_10 + 3600

        agents = make_agents()  # semua Active
        mock_agents.return_value = agents

        state = fresh_state()
        state["last_seen_any"] = {"001": JAM_10, "002": JAM_10, "003": JAM_10}
        state["last_seen_nids"] = {"002": JAM_10}
        mock_load.return_value = state

        with patch("bot_agent_monit.time.time", return_value=JAM_11):
            bot_agent_monit.main()

        saved_state = mock_save.call_args[0][1]
        # ASSERT: last_seen_any agent 001 masih jam 10, bukan jam 11
        self.assertEqual(saved_state["last_seen_any"]["001"], JAM_10)


# ═══════════════════════════════════════════════════════════════════════════
# TEST CLASS: FLOW 2 — "Jika sebelum 6 jam kembali connect, dihapus dari list"
# ═══════════════════════════════════════════════════════════════════════════
class TestFlow2_KembaliConnectDihapus(unittest.TestCase):
    """
    Jika agent yang tadinya Disconnected kembali Active sebelum 6 jam,
    dia harus dihapus dari pending list disconnected_since.
    """

    @patch("bot_agent_monit.send_telegram_alert")
    @patch("bot_agent_monit.process_logs", side_effect=lambda s: s)
    @patch("bot_agent_monit.get_all_agents")
    @patch("bot_agent_monit.save_state")
    @patch("bot_agent_monit.load_state")
    def test_agent_kembali_active_dihapus_dari_pending(
        self, mock_load, mock_save, mock_agents, mock_logs, mock_telegram
    ):
        """
        Jam 10:00 agent 001 Disconnected → masuk pending.
        Jam 12:00 agent 001 kembali Active → DIHAPUS dari pending.
        """
        JAM_10 = 1000000000
        JAM_12 = JAM_10 + (2 * 3600)

        agents = make_agents({"001": {"status": "Active"}})
        mock_agents.return_value = agents

        state = fresh_state()
        state["last_seen_any"] = {"001": JAM_10, "002": JAM_10, "003": JAM_10}
        state["last_seen_nids"] = {"002": JAM_10}
        state["disconnected_since"] = {"001": JAM_10}
        mock_load.return_value = state

        with patch("bot_agent_monit.time.time", return_value=JAM_12):
            bot_agent_monit.main()

        saved_state = mock_save.call_args[0][1]
        self.assertNotIn("001", saved_state["disconnected_since"])
        mock_telegram.assert_not_called()

    @patch("bot_agent_monit.send_telegram_alert")
    @patch("bot_agent_monit.process_logs", side_effect=lambda s: s)
    @patch("bot_agent_monit.get_all_agents")
    @patch("bot_agent_monit.save_state")
    @patch("bot_agent_monit.load_state")
    def test_agent_reconnect_last_alert_sent_dibersihkan(
        self, mock_load, mock_save, mock_agents, mock_logs, mock_telegram
    ):
        """
        Jika agent sudah sehat kembali (tidak error apapun),
        last_alert_sent juga harus dibersihkan agar tidak tersisa.
        """
        JAM_10 = 1000000000
        JAM_12 = JAM_10 + (2 * 3600)

        agents = make_agents({"001": {"status": "Active"}})
        mock_agents.return_value = agents

        state = fresh_state()
        state["last_seen_any"] = {"001": JAM_12, "002": JAM_12, "003": JAM_12}
        state["last_seen_nids"] = {"002": JAM_12}
        state["disconnected_since"] = {"001": JAM_10}
        state["last_alert_sent"] = {"001": JAM_10}
        mock_load.return_value = state

        with patch("bot_agent_monit.time.time", return_value=JAM_12):
            bot_agent_monit.main()

        saved_state = mock_save.call_args[0][1]
        self.assertNotIn("001", saved_state["disconnected_since"])
        self.assertNotIn("001", saved_state["last_alert_sent"])

    @patch("bot_agent_monit.send_telegram_alert")
    @patch("bot_agent_monit.process_logs", side_effect=lambda s: s)
    @patch("bot_agent_monit.get_all_agents")
    @patch("bot_agent_monit.save_state")
    @patch("bot_agent_monit.load_state")
    def test_multiple_agent_satu_reconnect_satu_masih_disconnected(
        self, mock_load, mock_save, mock_agents, mock_logs, mock_telegram
    ):
        """
        Agent 001 dan 003 Disconnected jam 10.
        Jam 12: agent 001 kembali Active, 003 masih Disconnected.
        → 001 dihapus dari list, 003 tetap di pending.
        """
        JAM_10 = 1000000000
        JAM_12 = JAM_10 + (2 * 3600)

        agents = make_agents({
            "001": {"status": "Active"},
            "003": {"status": "Disconnected"},
        })
        mock_agents.return_value = agents

        state = fresh_state()
        state["last_seen_any"] = {"001": JAM_10, "002": JAM_10, "003": JAM_10}
        state["last_seen_nids"] = {"002": JAM_10}
        state["disconnected_since"] = {"001": JAM_10, "003": JAM_10}
        mock_load.return_value = state

        with patch("bot_agent_monit.time.time", return_value=JAM_12):
            bot_agent_monit.main()

        saved_state = mock_save.call_args[0][1]
        self.assertNotIn("001", saved_state["disconnected_since"])
        self.assertIn("003", saved_state["disconnected_since"])
        self.assertEqual(saved_state["disconnected_since"]["003"], JAM_10)
        mock_telegram.assert_not_called()


# ═══════════════════════════════════════════════════════════════════════════
# TEST CLASS: FLOW 3 — "Setelah 6 jam baru dikirim listnya"
# ═══════════════════════════════════════════════════════════════════════════
class TestFlow3_Setelah6JamKirimTelegram(unittest.TestCase):
    """
    Jika agent masih Disconnected / bermasalah selama >= 6 jam,
    barulah alert dikirim ke Telegram.
    """

    @patch("bot_agent_monit.send_telegram_alert")
    @patch("bot_agent_monit.process_logs", side_effect=lambda s: s)
    @patch("bot_agent_monit.get_all_agents")
    @patch("bot_agent_monit.save_state")
    @patch("bot_agent_monit.load_state")
    def test_setelah_6_jam_disconnected_kirim_telegram(
        self, mock_load, mock_save, mock_agents, mock_logs, mock_telegram
    ):
        """
        Jam 10:00 agent 001 Disconnected.
        Jam 16:00 (6 jam kemudian) masih Disconnected → Telegram DIKIRIM.
        """
        JAM_10 = 1000000000
        JAM_16 = JAM_10 + (6 * 3600)

        agents = make_agents({"001": {"status": "Disconnected"}})
        mock_agents.return_value = agents

        state = fresh_state()
        state["last_seen_any"] = {"001": JAM_10, "002": JAM_10, "003": JAM_10}
        state["last_seen_nids"] = {"002": JAM_10}
        state["disconnected_since"] = {"001": JAM_10}
        mock_load.return_value = state

        with patch("bot_agent_monit.time.time", return_value=JAM_16):
            bot_agent_monit.main()

        # ASSERT: Telegram DIKIRIM
        mock_telegram.assert_called_once()

        pesan = mock_telegram.call_args[0][0]
        self.assertIn("001", pesan)
        self.assertIn("server-web-01", pesan)
        self.assertIn("Disconnected", pesan)

        saved_state = mock_save.call_args[0][1]
        self.assertIn("001", saved_state["last_alert_sent"])
        self.assertEqual(saved_state["last_alert_sent"]["001"], JAM_16)

    @patch("bot_agent_monit.send_telegram_alert")
    @patch("bot_agent_monit.process_logs", side_effect=lambda s: s)
    @patch("bot_agent_monit.get_all_agents")
    @patch("bot_agent_monit.save_state")
    @patch("bot_agent_monit.load_state")
    def test_setelah_7_jam_no_log_wazuh_kirim_telegram(
        self, mock_load, mock_save, mock_agents, mock_logs, mock_telegram
    ):
        """
        Agent Active tapi tidak ada log selama 7 jam → Telegram dikirim
        karena sudah melewati threshold 6 jam.
        """
        JAM_10 = 1000000000
        JAM_17 = JAM_10 + (7 * 3600)

        agents = make_agents()  # semua Active
        mock_agents.return_value = agents

        state = fresh_state()
        state["last_seen_any"] = {"001": JAM_10, "002": JAM_17, "003": JAM_17}
        state["last_seen_nids"] = {"002": JAM_17}
        mock_load.return_value = state

        with patch("bot_agent_monit.time.time", return_value=JAM_17):
            bot_agent_monit.main()

        mock_telegram.assert_called_once()
        pesan = mock_telegram.call_args[0][0]
        self.assertIn("001", pesan)
        self.assertIn("No Wazuh events", pesan)

    @patch("bot_agent_monit.send_telegram_alert")
    @patch("bot_agent_monit.process_logs", side_effect=lambda s: s)
    @patch("bot_agent_monit.get_all_agents")
    @patch("bot_agent_monit.save_state")
    @patch("bot_agent_monit.load_state")
    def test_setelah_6_jam_no_suricata_log_sensor_kirim_telegram(
        self, mock_load, mock_save, mock_agents, mock_logs, mock_telegram
    ):
        """
        Sensor (NIDS) Active tapi tidak ada log Suricata selama 7 jam → kirim alert.
        """
        JAM_10 = 1000000000
        JAM_17 = JAM_10 + (7 * 3600)

        agents = make_agents()
        mock_agents.return_value = agents

        state = fresh_state()
        state["last_seen_any"] = {"001": JAM_17, "002": JAM_17, "003": JAM_17}
        state["last_seen_nids"] = {"002": JAM_10}
        mock_load.return_value = state

        with patch("bot_agent_monit.time.time", return_value=JAM_17):
            bot_agent_monit.main()

        mock_telegram.assert_called_once()
        pesan = mock_telegram.call_args[0][0]
        self.assertIn("002", pesan)
        self.assertIn("No Suricata events", pesan)
        self.assertIn("NIDS", pesan)

    @patch("bot_agent_monit.send_telegram_alert")
    @patch("bot_agent_monit.process_logs", side_effect=lambda s: s)
    @patch("bot_agent_monit.get_all_agents")
    @patch("bot_agent_monit.save_state")
    @patch("bot_agent_monit.load_state")
    def test_5_jam_59_menit_belum_kirim_telegram(
        self, mock_load, mock_save, mock_agents, mock_logs, mock_telegram
    ):
        """Tepat sebelum 6 jam (5 jam 59 menit) — masih TIDAK kirim."""
        JAM_10 = 1000000000
        JAM_HAMPIR_16 = JAM_10 + (6 * 3600) - 60

        agents = make_agents({"001": {"status": "Disconnected"}})
        mock_agents.return_value = agents

        state = fresh_state()
        state["last_seen_any"] = {"001": JAM_10, "002": JAM_10, "003": JAM_10}
        state["last_seen_nids"] = {"002": JAM_10}
        state["disconnected_since"] = {"001": JAM_10}
        mock_load.return_value = state

        with patch("bot_agent_monit.time.time", return_value=JAM_HAMPIR_16):
            bot_agent_monit.main()

        mock_telegram.assert_not_called()


# ═══════════════════════════════════════════════════════════════════════════
# TEST CLASS: SPAM CONTROL — Tidak mengirim ulang sebelum 6 jam
# ═══════════════════════════════════════════════════════════════════════════
class TestSpamControl(unittest.TestCase):
    """
    Setelah alert dikirim, script tidak boleh spam.
    Alert berikutnya baru dikirim setelah 6 jam dari alert terakhir.
    """

    @patch("bot_agent_monit.send_telegram_alert")
    @patch("bot_agent_monit.process_logs", side_effect=lambda s: s)
    @patch("bot_agent_monit.get_all_agents")
    @patch("bot_agent_monit.save_state")
    @patch("bot_agent_monit.load_state")
    def test_tidak_spam_jika_sudah_kirim_kurang_dari_6_jam(
        self, mock_load, mock_save, mock_agents, mock_logs, mock_telegram
    ):
        """
        Alert sudah dikirim jam 16:00.
        Jam 17:00 (1 jam kemudian) script jalan lagi → TIDAK kirim ulang.
        """
        JAM_10 = 1000000000
        JAM_16 = JAM_10 + (6 * 3600)
        JAM_17 = JAM_10 + (7 * 3600)

        agents = make_agents({"001": {"status": "Disconnected"}})
        mock_agents.return_value = agents

        state = fresh_state()
        state["last_seen_any"] = {"001": JAM_10, "002": JAM_17, "003": JAM_17}
        state["last_seen_nids"] = {"002": JAM_17}
        state["disconnected_since"] = {"001": JAM_10}
        state["last_alert_sent"] = {"001": JAM_16}
        mock_load.return_value = state

        with patch("bot_agent_monit.time.time", return_value=JAM_17):
            bot_agent_monit.main()

        mock_telegram.assert_not_called()

    @patch("bot_agent_monit.send_telegram_alert")
    @patch("bot_agent_monit.process_logs", side_effect=lambda s: s)
    @patch("bot_agent_monit.get_all_agents")
    @patch("bot_agent_monit.save_state")
    @patch("bot_agent_monit.load_state")
    def test_kirim_ulang_setelah_6_jam_dari_alert_terakhir(
        self, mock_load, mock_save, mock_agents, mock_logs, mock_telegram
    ):
        """
        Alert pertama jam 16:00. Jam 22:00 (6 jam setelah alert) masih Disconnected
        → kirim ulang alert kedua.
        """
        JAM_10 = 1000000000
        JAM_16 = JAM_10 + (6 * 3600)
        JAM_22 = JAM_16 + (6 * 3600)

        agents = make_agents({"001": {"status": "Disconnected"}})
        mock_agents.return_value = agents

        state = fresh_state()
        state["last_seen_any"] = {"001": JAM_10, "002": JAM_22, "003": JAM_22}
        state["last_seen_nids"] = {"002": JAM_22}
        state["disconnected_since"] = {"001": JAM_10}
        state["last_alert_sent"] = {"001": JAM_16}
        mock_load.return_value = state

        with patch("bot_agent_monit.time.time", return_value=JAM_22):
            bot_agent_monit.main()

        mock_telegram.assert_called_once()
        saved_state = mock_save.call_args[0][1]
        self.assertEqual(saved_state["last_alert_sent"]["001"], JAM_22)


# ═══════════════════════════════════════════════════════════════════════════
# TEST CLASS: FULL SCENARIO — Simulasi end-to-end
# ═══════════════════════════════════════════════════════════════════════════
class TestFullScenario(unittest.TestCase):
    """
    Simulasi 3 tahap lengkap:
      Tahap 1 (Jam 10): Agent disconnected → dicatat, tidak kirim
      Tahap 2 (Jam 12): Agent reconnect → dihapus dari pending
      Tahap 3: Agent lain tetap disconnected 6 jam → kirim telegram
    """

    def _run_main_with(self, agents, state, current_time):
        """Helper untuk jalankan main() dengan parameter tertentu."""
        with patch("bot_agent_monit.get_all_agents", return_value=agents), \
             patch("bot_agent_monit.process_logs", side_effect=lambda s: s), \
             patch("bot_agent_monit.send_telegram_alert") as mock_telegram, \
             patch("bot_agent_monit.load_state", return_value=state), \
             patch("bot_agent_monit.save_state") as mock_save, \
             patch("bot_agent_monit.time.time", return_value=current_time):

            bot_agent_monit.main()

            saved_state = mock_save.call_args[0][1]
            telegram_called = mock_telegram.called
            telegram_msg = mock_telegram.call_args[0][0] if telegram_called else None
            return saved_state, telegram_called, telegram_msg

    def test_skenario_lengkap_disconnect_reconnect_dan_alert(self):
        """
        Tahap 1: Jam 10 — agent 001 & 003 Disconnected. Disimpan ke pending.
        Tahap 2: Jam 12 — agent 001 kembali Active. Dihapus. 003 masih Disconnected.
        Tahap 3: Jam 16 — 003 sudah 6 jam Disconnected. Telegram DIKIRIM.
        """
        JAM_10 = 1000000000
        JAM_12 = JAM_10 + (2 * 3600)
        JAM_16 = JAM_10 + (6 * 3600)

        # ── TAHAP 1: Jam 10:00 ──
        agents_t1 = make_agents({
            "001": {"status": "Disconnected"},
            "003": {"status": "Disconnected"},
        })
        state_t1 = fresh_state()
        result_t1, tg_sent_t1, _ = self._run_main_with(
            agents_t1, state_t1, JAM_10
        )

        self.assertFalse(tg_sent_t1, "Tahap 1: Telegram seharusnya TIDAK dikirim")
        self.assertIn("001", result_t1["disconnected_since"])
        self.assertIn("003", result_t1["disconnected_since"])

        # ── TAHAP 2: Jam 12:00 ──
        agents_t2 = make_agents({
            "001": {"status": "Active"},
            "003": {"status": "Disconnected"},
        })
        result_t2, tg_sent_t2, _ = self._run_main_with(
            agents_t2, result_t1, JAM_12
        )

        self.assertFalse(tg_sent_t2, "Tahap 2: Telegram seharusnya TIDAK dikirim")
        self.assertNotIn("001", result_t2["disconnected_since"],
                         "Tahap 2: 001 seharusnya DIHAPUS dari pending")
        self.assertIn("003", result_t2["disconnected_since"],
                      "Tahap 2: 003 seharusnya MASIH di pending")

        # Simulasi: setelah reconnect, log dari agent 001 kembali mengalir
        # dan sensor 002 juga masih aktif mengirim log.
        result_t2["last_seen_any"]["001"] = JAM_12
        result_t2["last_seen_any"]["002"] = JAM_12
        result_t2["last_seen_any"]["003"] = JAM_12
        result_t2["last_seen_nids"]["002"] = JAM_12

        # ── TAHAP 3: Jam 16:00 ──
        agents_t3 = make_agents({
            "001": {"status": "Active"},
            "003": {"status": "Disconnected"},
        })
        result_t3, tg_sent_t3, tg_msg_t3 = self._run_main_with(
            agents_t3, result_t2, JAM_16
        )

        self.assertTrue(tg_sent_t3, "Tahap 3: Telegram HARUS dikirim")
        self.assertIn("003", tg_msg_t3, "Tahap 3: Pesan harus menyebut agent 003")
        self.assertIn("Disconnected", tg_msg_t3)
        self.assertNotIn("001", tg_msg_t3,
                         "Tahap 3: Agent 001 sudah sehat, tidak boleh ada di alert")


# ═══════════════════════════════════════════════════════════════════════════
# TEST CLASS: EDGE CASES
# ═══════════════════════════════════════════════════════════════════════════
class TestEdgeCases(unittest.TestCase):

    @patch("bot_agent_monit.send_telegram_alert")
    @patch("bot_agent_monit.process_logs", side_effect=lambda s: s)
    @patch("bot_agent_monit.get_all_agents")
    @patch("bot_agent_monit.save_state")
    @patch("bot_agent_monit.load_state")
    def test_agent_000_diabaikan(
        self, mock_load, mock_save, mock_agents, mock_logs, mock_telegram
    ):
        """Agent ID 000 (manager) harus selalu diabaikan."""
        JAM = 1000000000 + (7 * 3600)
        agents = {
            "000": {"name": "manager", "status": "Disconnected", "is_sensor": False},
        }
        mock_agents.return_value = agents
        mock_load.return_value = fresh_state()

        with patch("bot_agent_monit.time.time", return_value=JAM):
            bot_agent_monit.main()

        mock_telegram.assert_not_called()
        saved_state = mock_save.call_args[0][1]
        self.assertNotIn("000", saved_state.get("disconnected_since", {}))

    @patch("bot_agent_monit.send_telegram_alert")
    @patch("bot_agent_monit.process_logs", side_effect=lambda s: s)
    @patch("bot_agent_monit.get_all_agents")
    @patch("bot_agent_monit.save_state")
    @patch("bot_agent_monit.load_state")
    def test_agent_never_connected_diabaikan(
        self, mock_load, mock_save, mock_agents, mock_logs, mock_telegram
    ):
        """Agent dengan status 'Never connected' harus selalu diabaikan."""
        JAM = 1000000000 + (7 * 3600)
        agents = {
            "099": {"name": "new-server", "status": "Never connected", "is_sensor": False},
        }
        mock_agents.return_value = agents
        mock_load.return_value = fresh_state()

        with patch("bot_agent_monit.time.time", return_value=JAM):
            bot_agent_monit.main()

        mock_telegram.assert_not_called()

    @patch("bot_agent_monit.send_telegram_alert")
    @patch("bot_agent_monit.process_logs", side_effect=lambda s: s)
    @patch("bot_agent_monit.get_all_agents")
    def test_get_all_agents_gagal_return_kosong(
        self, mock_agents, mock_logs, mock_telegram
    ):
        """Jika get_all_agents return {} (error), main() harus return tanpa crash."""
        mock_agents.return_value = {}
        bot_agent_monit.main()
        mock_telegram.assert_not_called()


# ═══════════════════════════════════════════════════════════════════════════
# TEST CLASS: process_logs — parsing log file (utils.py)
# ═══════════════════════════════════════════════════════════════════════════
class TestProcessLogs(unittest.TestCase):

    @patch("utils.os.path.exists", return_value=True)
    @patch("utils.os.stat")
    @patch("utils.os.path.getsize", return_value=500)
    def test_log_masuk_reset_last_seen(self, mock_size, mock_stat, mock_exists):
        """Ketika ada log JSON masuk dari agent, last_seen_any harus di-reset."""
        mock_stat.return_value = MagicMock(st_ino=12345)

        JAM_13 = 1000000000 + (3 * 3600)
        log_line = json.dumps({"agent": {"id": "001"}}) + "\n"

        state = {
            "offset": 0,
            "inode": 12345,
            "last_seen_any": {"001": 1000000000},
            "last_seen_nids": {},
            "disconnected_since": {},
            "last_alert_sent": {},
        }

        with patch("utils.time.time", return_value=JAM_13), \
             patch("builtins.open", mock_open(read_data=log_line)):
            result = utils.process_logs(state)
            self.assertEqual(result["last_seen_any"]["001"], JAM_13)

    @patch("utils.os.path.exists", return_value=True)
    @patch("utils.os.stat")
    @patch("utils.os.path.getsize", return_value=500)
    def test_suricata_log_masuk_reset_last_seen_nids(self, mock_size, mock_stat, mock_exists):
        """Log Suricata harus reset last_seen_nids."""
        mock_stat.return_value = MagicMock(st_ino=12345)

        JAM_13 = 1000000000 + (3 * 3600)
        log_line = json.dumps({
            "agent": {"id": "002"},
            "location": "/var/log/suricata/eve.json"
        }) + "\n"

        state = {
            "offset": 0,
            "inode": 12345,
            "last_seen_any": {"002": 1000000000},
            "last_seen_nids": {"002": 1000000000},
            "disconnected_since": {},
            "last_alert_sent": {},
        }

        with patch("utils.time.time", return_value=JAM_13), \
             patch("builtins.open", mock_open(read_data=log_line)):
            result = utils.process_logs(state)
            self.assertEqual(result["last_seen_any"]["002"], JAM_13)
            self.assertEqual(result["last_seen_nids"]["002"], JAM_13)

    @patch("utils.os.path.exists", return_value=False)
    def test_log_file_tidak_ada_return_state_asis(self, mock_exists):
        """Jika file log tidak ada, state dikembalikan tanpa perubahan."""
        state = {"offset": 0, "inode": None, "last_seen_any": {}, "last_seen_nids": {}}
        result = utils.process_logs(state)
        self.assertEqual(result, state)


if __name__ == "__main__":
    unittest.main()
