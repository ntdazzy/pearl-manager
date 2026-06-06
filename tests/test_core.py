from __future__ import annotations

import asyncio
import importlib
import os
import re
import shutil
import subprocess
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock, patch


class ConfigTests(unittest.TestCase):
    def load_with_text(self, text: str):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.env"
            path.write_text(text, encoding="utf-8")
            with patch.dict(os.environ, {"PEARL_CONFIG": str(path)}, clear=False):
                import config

                importlib.reload(config)
                return config.load_config()

    def test_legacy_wallet_pool_keys_override_defaults(self):
        cfg = self.load_with_text(
            "WALLET=prl1legacy\n"
            "POOL=sg1.alphapool.tech:5566\n"
        )
        self.assertEqual(cfg["WALLET_ADDRESS"], "prl1legacy")
        self.assertEqual(cfg["POOL_HOST"], "sg1.alphapool.tech")
        self.assertEqual(cfg["POOL_PORT"], "5566")

    def test_explicit_new_pool_keys_win_over_legacy_pool(self):
        cfg = self.load_with_text(
            "POOL=old.example:1\n"
            "POOL_HOST=sg1.alphapool.tech\n"
            "POOL_PORT=5566\n"
        )
        self.assertEqual(cfg["POOL_HOST"], "sg1.alphapool.tech")
        self.assertEqual(cfg["POOL_PORT"], "5566")

    def test_database_url_loaded_from_environment_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "missing.env"
            with patch.dict(os.environ, {"PEARL_CONFIG": str(path), "DATABASE_URL": "postgresql://example"}, clear=False):
                import config

                importlib.reload(config)
                cfg = config.load_config()
        self.assertEqual(cfg["DATABASE_URL"], "postgresql://example")

    def test_config_file_environment_alias_is_supported(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "custom.env"
            path.write_text("WALLET_ADDRESS=prl1custom\nPOOL_HOST=custom.pool\nPOOL_PORT=1234\n", encoding="utf-8")
            with patch.dict(os.environ, {"CONFIG_FILE": str(path)}, clear=True):
                import config

                importlib.reload(config)
                cfg = config.load_config()
        self.assertEqual(cfg["WALLET_ADDRESS"], "prl1custom")
        self.assertEqual(cfg["POOL_HOST"], "custom.pool")

    def test_default_wallet_is_placeholder_without_config(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "missing.env"
            with patch.dict(os.environ, {"PEARL_CONFIG": str(path)}, clear=True):
                import config

                importlib.reload(config)
                cfg = config.load_config()
        self.assertEqual(cfg["WALLET_ADDRESS"], "CHANGE_ME_PEARL_WALLET")

    def test_malformed_oc_profiles_fall_back_to_defaults(self):
        import config

        profiles = config.get_oc_profiles({"OC_PROFILES_JSON": '{"bad":{"power_limit":"oops"}}'})
        self.assertIn("balance", profiles)
        self.assertNotIn("bad", profiles)


class ServiceParsingTests(unittest.TestCase):
    def test_alphapool_miner_parser(self):
        import miner_services

        raw = {
            "balance_prl": 0.5,
            "total_paid_prl": 2.0,
            "shares24h": 12,
            "workers": [{"hashrate_live": "32.79 TH/s", "online": True}],
            "mode": "PPLNS",
        }
        with patch.object(miner_services, "fetch_json", return_value=raw):
            data = miner_services.fetch_pool_miner_stats(
                {
                    "WALLET_ADDRESS": "prl1abc.worker",
                    "POOL_API_URL": "https://example.test/{wallet}",
                }
            )
        self.assertTrue(data["available"])
        self.assertEqual(data["wallet"], "prl1abc")
        self.assertEqual(data["balance_prl"], 0.5)
        self.assertAlmostEqual(data["hashrate_hps"], 32.79e12)

    def test_price_parser_prlscan(self):
        import miner_services

        with patch.object(miner_services, "fetch_json", return_value={"price_usd": 0.74, "source": "Pearl OTC"}):
            data = miner_services.fetch_price({"PRICE_API_URL": "https://price.test", "USD_VND_RATE": "25000"})
        self.assertEqual(data["price_usd"], 0.74)
        self.assertEqual(data["price_vnd"], 18500)

    def test_price_parser_nested_payload(self):
        import miner_services

        with patch.object(miner_services, "fetch_json", return_value={"market": {"price_usd": "7.4e-1"}}):
            data = miner_services.fetch_price({"PRICE_API_URL": "https://price.test", "USD_VND_RATE": "25000"})
        self.assertAlmostEqual(data["price_usd"], 0.74)

    def test_pool_summary_parser(self):
        import miner_services

        responses = [
            {
                "feePercent": 5,
                "coins": [{"reward": 2642.5, "network_hash": "30 EH/s"}],
                "pool": {"hashrate": "4 EH/s", "miners24h": 10, "workers": 20, "blocks24h": 3},
                "stratum": {"standardPort": 5566},
            },
            {"estimated_hashrate_hps": 31e18, "estimated_pool_hashrate_hps": 4.1e18, "avg_block_time_seconds": 132},
        ]
        with patch.object(miner_services, "fetch_json", side_effect=responses):
            summary = miner_services.fetch_pool_summary({"POOL_STATS_URL": "stats", "CHAIN_API_URL": "chain"})
        self.assertTrue(summary["available"])
        self.assertEqual(summary["fee_percent"], 5)
        self.assertEqual(summary["workers"], 20)
        self.assertEqual(summary["stratum"]["standardPort"], 5566)
        self.assertEqual(summary["network_hashrate_hps"], 31e18)

    def test_scientific_notation_hashrate_parser(self):
        import miner_services

        self.assertEqual(miner_services.safe_float("3.1e19"), 3.1e19)
        self.assertEqual(miner_services.hashrate_to_hps("3.1e1 TH/s"), 31e12)

    def test_workers_dict_parser(self):
        import miner_services

        raw = {
            "balance_grain": 50000000,
            "workers": {"rig": {"hashrate_live": "1 TH/s", "online": True}},
        }
        with patch.object(miner_services, "fetch_json", return_value=raw):
            data = miner_services.fetch_pool_miner_stats({"WALLET_ADDRESS": "prl1abc", "POOL_API_URL": "https://example.test/{wallet}"})
        self.assertEqual(data["balance_prl"], 0.5)
        self.assertAlmostEqual(data["hashrate_hps"], 1e12)

    def test_pool_miner_stats_tries_fallback_urls_and_miningcore_payload(self):
        import miner_services

        responses = [
            None,
            {
                "data": {
                    "pendingShares": 1.25,
                    "totalPaid": 2.5,
                    "performance": {"hashrate": "9.5 TH/s"},
                    "validShares": 17,
                    "workers": {"rig": {"hashrateLive": "10 TH/s", "online": True}},
                    "paymentProcessing": "PPLNS",
                }
            },
        ]

        with patch.object(miner_services, "fetch_json", side_effect=responses) as fetch:
            data = miner_services.fetch_pool_miner_stats(
                {
                    "WALLET_ADDRESS": "prl1abc.rig",
                    "POOL_API_URL": "https://primary/{wallet}",
                    "POOL_API_FALLBACK_URLS": "https://fallback/{wallet}",
                }
            )

        self.assertEqual([call.args[0] for call in fetch.call_args_list], ["https://primary/prl1abc", "https://fallback/prl1abc"])
        self.assertTrue(data["available"])
        self.assertEqual(data["url"], "https://fallback/prl1abc")
        self.assertEqual(data["balance_prl"], 1.25)
        self.assertEqual(data["total_paid_prl"], 2.5)
        self.assertEqual(data["shares24h"], 17)
        self.assertAlmostEqual(data["hashrate_hps"], 10e12)

    def test_pool_miner_stats_fallback_when_api_unavailable(self):
        import miner_services

        with patch.object(miner_services, "fetch_json", return_value=None):
            data = miner_services.fetch_pool_miner_stats(
                {
                    "WALLET_ADDRESS": "prl1abc.worker",
                    "POOL_API_URL": "https://example.test/{wallet}",
                }
            )
        self.assertFalse(data["available"])
        self.assertEqual(data["balance_prl"], 0.0)
        self.assertEqual(data["hashrate_label"], "N/A")

    def test_price_fallback_when_api_unavailable(self):
        import miner_services

        with patch.object(miner_services, "fetch_json", return_value=None):
            data = miner_services.fetch_price({"PRICE_API_URL": "https://price.test", "USD_VND_RATE": "25000"})
        self.assertFalse(data["available"])
        self.assertEqual(data["price_usd"], 0.0)

    def test_control_miner_uses_restricted_systemctl(self):
        import miner_services

        calls = []

        def fake_run(args, timeout=0, env=None, sudo=False):
            calls.append((args, sudo))
            return miner_services.CommandResult(True, "ok")

        with patch.object(miner_services, "run_command", side_effect=fake_run), patch.object(miner_services, "record_event"):
            result = miner_services.control_miner("restart", {"MINER_SERVICE": "pearl-miner.service"})
        self.assertTrue(result["ok"])
        self.assertEqual(calls[0], (["systemctl", "restart", "pearl-miner.service"], True))

    def test_control_miner_notifies_telegram_on_start(self):
        import miner_services

        with patch.object(miner_services, "run_command", return_value=miner_services.CommandResult(True, "systemctl")), \
            patch.object(miner_services, "record_event"), \
            patch.object(miner_services, "notify_miner_started") as notify:
            result = miner_services.control_miner("start", {"MINER_SERVICE": "pearl-miner.service"})
        self.assertTrue(result["ok"])
        notify.assert_called_once_with("start", {"MINER_SERVICE": "pearl-miner.service"})

    def test_control_miner_does_not_notify_telegram_on_stop(self):
        import miner_services

        with patch.object(miner_services, "run_command", return_value=miner_services.CommandResult(True, "systemctl")), \
            patch.object(miner_services, "record_event"), \
            patch.object(miner_services, "notify_miner_started") as notify:
            result = miner_services.control_miner("stop", {"MINER_SERVICE": "pearl-miner.service"})
        self.assertTrue(result["ok"])
        notify.assert_not_called()

    def test_telegram_notification_skips_missing_config(self):
        import miner_services

        with patch.object(miner_services.requests, "post") as post:
            result = miner_services.send_telegram_notification("hello", {"TELEGRAM_TOKEN": "", "TELEGRAM_CHAT_ID": ""})
        self.assertFalse(result["ok"])
        self.assertTrue(result["skipped"])
        post.assert_not_called()

    def test_sudo_commands_are_non_interactive(self):
        import miner_services

        with patch("subprocess.run") as run:
            run.return_value.returncode = 0
            run.return_value.stdout = ""
            run.return_value.stderr = ""
            miner_services.run_command(["systemctl", "stop", "pearl-miner.service"], sudo=True)
        self.assertEqual(run.call_args.args[0][:3], ["sudo", "-n", "systemctl"])

    def test_miner_status_uses_systemd_main_pid_for_alpha_miner(self):
        import miner_services

        responses = [
            miner_services.CommandResult(True, "systemctl", stdout="active"),
            miner_services.CommandResult(
                True,
                "systemctl",
                stdout="ActiveEnterTimestamp=now\nSubState=running\nMainPID=12345\n",
            ),
        ]

        with patch.object(miner_services, "run_command", side_effect=responses) as run:
            status = miner_services.get_miner_status({"MINER_SERVICE": "pearl-miner.service", "MINER_TYPE": "alpha", "MINER_EXEC": "/opt/alpha-miner"})
        self.assertTrue(status["is_active"])
        self.assertTrue(status["process_running"])
        self.assertEqual(status["pid"], "12345")
        self.assertEqual(run.call_count, 2)

    def test_miner_status_handles_inactive_alpha_miner_with_no_main_pid(self):
        import miner_services

        responses = [
            miner_services.CommandResult(False, "systemctl", stdout="inactive", returncode=3),
            miner_services.CommandResult(
                True,
                "systemctl",
                stdout="ActiveEnterTimestamp=now\nSubState=dead\nMainPID=0\n",
            ),
            miner_services.CommandResult(False, "pgrep", stdout="", returncode=1),
        ]

        with patch.object(miner_services, "run_command", side_effect=responses) as run:
            status = miner_services.get_miner_status({"MINER_SERVICE": "pearl-miner.service", "MINER_TYPE": "alpha", "MINER_EXEC": "/opt/alpha-miner"})
        self.assertFalse(status["is_active"])
        self.assertFalse(status["process_running"])
        self.assertEqual(status["pid"], "")
        self.assertEqual(run.call_args.args[0], ["pgrep", "-f", "alpha-miner"])

    def test_oc_profile_commands_include_expected_offsets(self):
        import miner_services

        calls = []

        def fake_run(args, timeout=0, env=None, sudo=False):
            calls.append((args, sudo, env))
            return miner_services.CommandResult(True, "ok")

        config = {
            "GPU_INDEX": "0",
            "DISPLAY": ":0",
            "XAUTHORITY": "/tmp/xauth",
        }
        with patch.object(miner_services, "run_command", side_effect=fake_run), patch.object(miner_services, "record_event"):
            result = miner_services.apply_oc_profile("balance", config)
        self.assertTrue(result["ok"])
        self.assertIn(["nvidia-smi", "--id=0", "--power-limit=115"], [call[0] for call in calls])
        self.assertIn(["nvidia-smi", "--id=0", "--lock-gpu-clocks=1450,1450"], [call[0] for call in calls])
        self.assertIn(["nvidia-settings", "-c", ":0", "-a", "[gpu:0]/GPUGraphicsClockOffset[3]=200"], [call[0] for call in calls])
        self.assertIn(["nvidia-settings", "-c", ":0", "-a", "[gpu:0]/GPUMemoryTransferRateOffset[3]=1000"], [call[0] for call in calls])
        self.assertEqual(calls[-1][2]["DISPLAY"], ":0")
        self.assertEqual(calls[-1][2]["XAUTHORITY"], "/tmp/xauth")

    def test_oc_profile_falls_back_to_alternate_display_for_offsets(self):
        import miner_services

        calls = []

        def fake_run(args, timeout=0, env=None, sudo=False):
            calls.append((args, sudo, env))
            if args[0] == "nvidia-settings" and args[2] == ":0":
                return miner_services.CommandResult(False, "settings", stderr="No target", returncode=1)
            return miner_services.CommandResult(True, "ok")

        with patch.dict(os.environ, {"DISPLAY": ":1"}, clear=False), \
            patch.object(miner_services, "run_command", side_effect=fake_run), \
            patch.object(miner_services, "record_event"):
            result = miner_services.apply_oc_profile("balance", {"GPU_INDEX": "0", "DISPLAY": ":0"})

        self.assertTrue(result["ok"])
        self.assertEqual(result["warnings"], [])
        commands = [call[0] for call in calls]
        self.assertIn(["nvidia-settings", "-c", ":0", "-a", "[gpu:0]/GPUGraphicsClockOffset[3]=200"], commands)
        self.assertIn(["nvidia-settings", "-c", ":1", "-a", "[gpu:0]/GPUGraphicsClockOffset[3]=200"], commands)

    def test_oc_profile_succeeds_when_optional_nvidia_settings_fails(self):
        import miner_services

        def fake_run(args, timeout=0, env=None, sudo=False):
            if args[0] == "nvidia-settings":
                return miner_services.CommandResult(False, "settings", stderr="Coolbits unavailable", returncode=1)
            return miner_services.CommandResult(True, "ok")

        with patch.object(miner_services, "run_command", side_effect=fake_run), patch.object(miner_services, "record_event"):
            result = miner_services.apply_oc_profile("balance", {"GPU_INDEX": "0", "DISPLAY": ":0"})

        self.assertTrue(result["ok"])
        self.assertIn("Coolbits unavailable", result["warnings"][0])

    def test_oc_profile_without_clock_lock_resets_previous_lock(self):
        import miner_services

        calls = []

        def fake_run(args, timeout=0, env=None, sudo=False):
            calls.append((args, sudo))
            return miner_services.CommandResult(True, "ok")

        custom = {"quiet": {"label": "Quiet", "power_limit": 90, "gpu_clock_min": 0, "gpu_clock_max": 0, "core_offset": 0, "memory_offset": 100}}
        with patch.object(miner_services, "get_oc_profiles", return_value=custom), \
            patch.object(miner_services, "run_command", side_effect=fake_run), \
            patch.object(miner_services, "record_event"):
            result = miner_services.apply_oc_profile("quiet", {"GPU_INDEX": "0", "DISPLAY": ":0"})

        self.assertTrue(result["ok"])
        self.assertIn((["nvidia-smi", "--id=0", "--reset-gpu-clocks"], True), calls)

    def test_malformed_oc_profile_returns_error_without_running_commands(self):
        import miner_services

        with patch.object(miner_services, "get_oc_profiles", return_value={"bad": {"power_limit": "oops"}}), \
            patch.object(miner_services, "run_command") as run:
            result = miner_services.apply_oc_profile("bad", {"GPU_INDEX": "0", "DISPLAY": ":0"})
        self.assertFalse(result["ok"])
        self.assertIn("Invalid profile values", result["error"])
        run.assert_not_called()

    def test_record_reward_upserts_current_hour_value(self):
        import miner_services
        from sqlalchemy.dialects import postgresql

        statements = []

        class FakeDB:
            def execute(self, stmt):
                statements.append(str(stmt.compile(dialect=postgresql.dialect())))

            def commit(self):
                pass

        manager = unittest.mock.MagicMock()
        manager.__enter__.return_value = FakeDB()
        manager.__exit__.return_value = None
        with patch.object(miner_services, "SessionLocal", return_value=manager), \
            patch.object(miner_services, "fetch_pool_miner_stats", return_value={}), \
            patch.object(miner_services, "calculate_hourly_reward", return_value=1.25):
            miner_services.record_reward_if_due({})
        self.assertTrue(statements)
        self.assertIn("ON CONFLICT", statements[0])
        self.assertIn("DO UPDATE SET", statements[0])

    def test_record_journal_snapshot_stores_new_lines(self):
        import miner_services

        class FakeDB:
            def __init__(self):
                self.rows = []
                self.committed = False

            def add(self, row):
                self.rows.append(row)

            def commit(self):
                self.committed = True

        fake_db = FakeDB()
        manager = unittest.mock.MagicMock()
        manager.__enter__.return_value = fake_db
        manager.__exit__.return_value = None
        output = "2026 line one\n2026 line two\n"
        result = miner_services.CommandResult(True, "journalctl", stdout=output)
        with patch.object(miner_services, "_LAST_JOURNAL_LINE", ""), \
            patch.object(miner_services, "run_command", return_value=result), \
            patch.object(miner_services, "SessionLocal", return_value=manager):
            miner_services.record_journal_snapshot({"MINER_SERVICE": "pearl-miner.service"})
        self.assertEqual(len(fake_db.rows), 2)
        self.assertTrue(fake_db.committed)

    def test_estimate_revenue_zeroes_stale_pool_hashrate_when_service_stopped(self):
        import miner_services

        with patch.object(miner_services, "fetch_pool_miner_stats", return_value={"available": True, "hashrate_hps": 10e12}), \
            patch.object(miner_services, "fetch_pool_summary", return_value={"available": True, "network_hashrate_hps": 100e12, "reward_prl": 100, "block_time_seconds": 100, "fee_percent": 0}), \
            patch.object(miner_services, "fetch_price", return_value={"available": True, "price_usd": 1.0, "price_vnd": 25000.0}), \
            patch.object(miner_services, "get_gpu_metrics", return_value={"temp_c": 50}), \
            patch.object(miner_services, "get_local_miner_stats", return_value={"available": True, "hashrate_hps": 25e12, "stale": False}), \
            patch.object(miner_services, "get_miner_status", return_value={"is_active": False, "process_running": False}):
            prediction = miner_services.estimate_revenue({})

        self.assertEqual(prediction["prl_24h"], 0.0)
        self.assertIn("Miner đang dừng", prediction["assessment"])

    def test_local_miner_journal_parser_extracts_hashrate_and_shares(self):
        import miner_services

        log = (
            "2026-06-06T10:15:45.336Z level=INFO gpu=0 component=miner status attempts=1680 hits=350 "
            "hashrate_th_s=24.30 tmac_s=24.30 share_equiv_th_s=20.37\n"
            "2026-06-06T10:15:50.106Z level=INFO gpu=0 component=share submitted job=1\n"
        )
        stats = miner_services.parse_local_miner_journal(log, stale_after=999999)
        self.assertTrue(stats["available"])
        self.assertAlmostEqual(stats["hashrate_hps"], 24.30e12)
        self.assertAlmostEqual(stats["share_equiv_hps"], 20.37e12)
        self.assertEqual(stats["submitted_shares"], 1)

    def test_snapshot_prefers_fresh_local_hashrate_over_pool(self):
        import miner_services

        with patch.object(miner_services, "get_gpu_metrics", return_value={"temp_c": 60}), \
            patch.object(miner_services, "get_miner_status", return_value={"is_active": True, "process_running": True}), \
            patch.object(miner_services, "get_local_miner_stats", return_value={"available": True, "hashrate_hps": 24e12, "stale": False}), \
            patch.object(miner_services, "fetch_pool_miner_stats", return_value={"available": True, "hashrate_hps": 5e12, "balance_prl": 1.0}), \
            patch.object(miner_services, "fetch_pool_summary", return_value={"available": True, "network_hashrate_hps": 100e12, "reward_prl": 100, "block_time_seconds": 100, "fee_percent": 0}), \
            patch.object(miner_services, "fetch_price", return_value={"available": True, "price_usd": 1.0, "price_vnd": 25000.0}):
            snapshot = miner_services.collect_telemetry_snapshot({"SNAPSHOT_CACHE_SECONDS": "0"}, use_cache=False)

        self.assertEqual(snapshot["effective_hashrate"]["source"], "local")
        self.assertAlmostEqual(snapshot["effective_hashrate"]["hashrate_hps"], 24e12)


class WatchdogTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        import telegram_bot

        telegram_bot.STATE.update(
            {
                "hot_count": 0,
                "zero_hash_count": 0,
                "crash_count": 0,
                "alerted_hot": False,
                "alerted_zero": False,
                "alerted_crash": False,
            }
        )

    async def test_hot_watchdog_stops_miner(self):
        import telegram_bot

        context = type("Ctx", (), {"bot": type("Bot", (), {"send_message": AsyncMock()})()})()
        sample = {
            "gpu": {"temp_c": 90},
            "miner": {"hashrate_hps": 1, "available": True},
            "status": {"is_active": True, "systemd_state": "active", "process_running": True},
        }
        with patch.object(telegram_bot, "cfg", return_value={"TELEGRAM_CHAT_ID": "42", "TEMP_SHUTDOWN_C": "80", "HOT_LIMIT_COUNT": "1", "HASHRATE_ZERO_LIMIT": "2"}), \
            patch.object(telegram_bot, "collect_and_store_sample", return_value=sample), \
            patch.object(telegram_bot, "record_reward_if_due"), \
            patch.object(telegram_bot, "control_miner", return_value={"ok": True}) as control:
            await telegram_bot.watchdog_task(context)
        control.assert_called_with("stop", unittest.mock.ANY)
        context.bot.send_message.assert_awaited()

    async def test_zero_hash_watchdog_alerts_without_stopping_by_default(self):
        import telegram_bot

        context = type("Ctx", (), {"bot": type("Bot", (), {"send_message": AsyncMock()})()})()
        sample = {
            "gpu": {"temp_c": 60},
            "miner": {"hashrate_hps": 0, "available": True},
            "local_miner": {"hashrate_hps": 0, "available": False, "stale": True},
            "effective_hashrate": {"hashrate_hps": 0, "source": "none"},
            "status": {"is_active": True, "systemd_state": "active", "process_running": True},
        }
        with patch.object(telegram_bot, "cfg", return_value={"TELEGRAM_CHAT_ID": "42", "TEMP_SHUTDOWN_C": "80", "HOT_LIMIT_COUNT": "3", "HASHRATE_ZERO_LIMIT": "2", "HASHRATE_ZERO_STOP": "0"}), \
            patch.object(telegram_bot, "collect_and_store_sample", return_value=sample), \
            patch.object(telegram_bot, "record_reward_if_due"), \
            patch.object(telegram_bot, "control_miner", return_value={"ok": True}) as control:
            await telegram_bot.watchdog_task(context)
            await telegram_bot.watchdog_task(context)
        control.assert_not_called()
        context.bot.send_message.assert_awaited()

    async def test_local_zero_hash_watchdog_can_stop_when_enabled(self):
        import telegram_bot

        context = type("Ctx", (), {"bot": type("Bot", (), {"send_message": AsyncMock()})()})()
        sample = {
            "gpu": {"temp_c": 60},
            "miner": {"hashrate_hps": 0, "available": True},
            "local_miner": {"hashrate_hps": 0, "available": True, "stale": False, "last_status_line": "hashrate_th_s=0"},
            "effective_hashrate": {"hashrate_hps": 0, "source": "none"},
            "status": {"is_active": True, "systemd_state": "active", "process_running": True},
        }
        with patch.object(telegram_bot, "cfg", return_value={"TELEGRAM_CHAT_ID": "42", "TEMP_SHUTDOWN_C": "80", "HOT_LIMIT_COUNT": "3", "HASHRATE_ZERO_LIMIT": "2", "HASHRATE_ZERO_STOP": "1"}), \
            patch.object(telegram_bot, "collect_and_store_sample", return_value=sample), \
            patch.object(telegram_bot, "record_reward_if_due"), \
            patch.object(telegram_bot, "control_miner", return_value={"ok": True}) as control:
            await telegram_bot.watchdog_task(context)
            await telegram_bot.watchdog_task(context)
        control.assert_called_with("stop", unittest.mock.ANY)

    async def test_zero_hash_watchdog_ignores_pool_api_outage(self):
        import telegram_bot

        context = type("Ctx", (), {"bot": type("Bot", (), {"send_message": AsyncMock()})()})()
        sample = {
            "gpu": {"temp_c": 60},
            "miner": {"hashrate_hps": 0, "available": False},
            "status": {"is_active": True, "systemd_state": "active", "process_running": True},
        }
        with patch.object(telegram_bot, "cfg", return_value={"TELEGRAM_CHAT_ID": "42", "TEMP_SHUTDOWN_C": "80", "HOT_LIMIT_COUNT": "3", "HASHRATE_ZERO_LIMIT": "1"}), \
            patch.object(telegram_bot, "collect_and_store_sample", return_value=sample), \
            patch.object(telegram_bot, "record_reward_if_due"), \
            patch.object(telegram_bot, "control_miner", return_value={"ok": True}) as control:
            await telegram_bot.watchdog_task(context)
        control.assert_not_called()

    async def test_hot_watchdog_stops_even_if_telegram_send_fails(self):
        import telegram_bot

        context = type("Ctx", (), {"bot": type("Bot", (), {"send_message": AsyncMock(side_effect=RuntimeError("telegram down"))})()})()
        sample = {
            "gpu": {"temp_c": 90},
            "miner": {"hashrate_hps": 1, "available": True},
            "status": {"is_active": True, "systemd_state": "active", "process_running": True},
        }
        with patch.object(telegram_bot, "cfg", return_value={"TELEGRAM_CHAT_ID": "42", "TEMP_SHUTDOWN_C": "80", "HOT_LIMIT_COUNT": "1", "HASHRATE_ZERO_LIMIT": "2"}), \
            patch.object(telegram_bot, "collect_and_store_sample", return_value=sample), \
            patch.object(telegram_bot, "record_reward_if_due"), \
            patch.object(telegram_bot, "record_event"), \
            patch.object(telegram_bot, "control_miner", return_value={"ok": True}) as control:
            await telegram_bot.watchdog_task(context)
        control.assert_called_with("stop", unittest.mock.ANY)


class TelegramUiTests(unittest.IsolatedAsyncioTestCase):
    def test_main_menu_contains_prd_actions(self):
        import telegram_bot

        buttons = [button.text for row in telegram_bot.main_menu().inline_keyboard for button in row]
        for label in ["📊 Thống Kê", "🎮 Điều Khiển", "💰 Số Dư", "⚙️ Cài Đặt", "📈 Xem Biểu Đồ", "❓ Trợ Giúp"]:
            self.assertIn(label, buttons)

    def test_help_text_explains_key_actions(self):
        import telegram_bot

        with patch.object(
            telegram_bot,
            "cfg",
            return_value={
                "MINER_TYPE": "alpha",
                "POOL_HOST": "sg1.alphapool.tech",
                "POOL_PORT": "5566",
                "STARTUP_OC_PROFILE": "balance",
                "TEMP_WARN_C": "84",
                "TEMP_SHUTDOWN_C": "90",
            },
        ):
            text = telegram_bot.help_text()
        self.assertIn("/help", text)
        self.assertIn("Trên máy", text)
        self.assertIn("AlphaPool", text)
        self.assertIn("balance", text)

    def test_oc_menu_contains_default_profiles(self):
        import telegram_bot

        callbacks = [button.callback_data for row in telegram_bot.oc_menu().inline_keyboard for button in row]
        for callback in ["oc:eco", "oc:balance", "oc:max"]:
            self.assertIn(callback, callbacks)

    def test_oc_menu_uses_configured_profiles(self):
        import telegram_bot

        with patch.object(telegram_bot, "get_oc_profiles", return_value={"quiet": {"label": "Quiet", "power_limit": 90, "core_offset": 0, "memory_offset": 100}}):
            buttons = [button for row in telegram_bot.oc_menu().inline_keyboard for button in row]
        self.assertIn("oc:quiet", [button.callback_data for button in buttons])
        self.assertNotIn("oc:balance", [button.callback_data for button in buttons])

    def test_build_application_requires_token(self):
        import telegram_bot

        with patch.object(telegram_bot, "cfg", return_value={"TELEGRAM_TOKEN": ""}):
            with self.assertRaises(RuntimeError):
                telegram_bot.build_application()

    async def test_reply_or_edit_uses_caption_for_photo_messages(self):
        import telegram_bot

        message = unittest.mock.MagicMock()
        message.photo = [object()]
        message.caption = "old"
        query = unittest.mock.MagicMock()
        query.message = message
        query.edit_message_caption = AsyncMock()
        query.edit_message_text = AsyncMock()
        update = unittest.mock.MagicMock()
        update.callback_query = query
        update.message = None

        await telegram_bot.reply_or_edit(update, "<b>new</b>", telegram_bot.main_menu())

        query.edit_message_caption.assert_awaited_once()
        query.edit_message_text.assert_not_awaited()


class ChartTests(unittest.TestCase):
    def test_chart_data_shape_without_database_rows(self):
        import miner_services

        with patch.object(miner_services, "SessionLocal") as sessions:
            db = sessions.return_value.__enter__.return_value
            db.query.return_value.filter.return_value.order_by.return_value.all.return_value = []
            data = miner_services.get_chart_data(days=7)
        self.assertEqual(len(data["labels"]), 7)
        self.assertEqual(len(data["daily"]), 7)
        self.assertEqual(len(data["cumulative"]), 7)

    def test_render_hardware_chart_returns_png_when_rows_exist(self):
        import miner_services
        from models import HardwareLog

        row = HardwareLog(timestamp=datetime.now(timezone.utc), temp_c=70, power_w=110, fan_speed=80, hashrate_th=25, vram_gb=5)
        with patch.object(miner_services, "SessionLocal") as sessions:
            db = sessions.return_value.__enter__.return_value
            db.query.return_value.filter.return_value.order_by.return_value.all.return_value = [row]
            image = miner_services.render_hardware_chart(hours=24)
        self.assertIsNotNone(image)
        self.assertEqual(image.getvalue()[:8], b"\x89PNG\r\n\x1a\n")


class StreamingTests(unittest.TestCase):
    def test_stream_journal_lines_handles_process_that_exits_before_cleanup(self):
        import miner_services

        class FakeStdout:
            async def readline(self):
                return b""

        class FakeStderr:
            async def read(self):
                return b""

        class FakeProcess:
            stdout = FakeStdout()
            stderr = FakeStderr()
            returncode = 0

            def terminate(self):
                raise AssertionError("terminate should not be called for exited process")

            async def wait(self):
                return 0

        async def fake_exec(*args, **kwargs):
            return FakeProcess()

        async def consume():
            with patch.object(asyncio, "create_subprocess_exec", side_effect=fake_exec):
                return [line async for line in miner_services.stream_journal_lines("pearl-miner.service")]

        self.assertEqual(asyncio.run(consume()), [])

    def test_stream_journal_lines_stops_while_idle_when_client_disconnects(self):
        import miner_services

        class FakeStdout:
            async def readline(self):
                await asyncio.sleep(10)
                return b"late line\n"

        class FakeStderr:
            async def read(self):
                return b""

        class FakeProcess:
            stdout = FakeStdout()
            stderr = FakeStderr()
            returncode = None
            terminated = False

            def terminate(self):
                self.terminated = True
                self.returncode = 143

            async def wait(self):
                return self.returncode

            def kill(self):
                self.returncode = 137

        fake_process = FakeProcess()

        async def fake_exec(*args, **kwargs):
            return fake_process

        async def consume():
            with patch.object(asyncio, "create_subprocess_exec", side_effect=fake_exec):
                return [
                    line
                    async for line in miner_services.stream_journal_lines(
                        "pearl-miner.service",
                        stop_check=lambda: True,
                        idle_timeout=0.01,
                    )
                ]

        self.assertEqual(asyncio.run(consume()), [])
        self.assertTrue(fake_process.terminated)


class SchemaTests(unittest.TestCase):
    def test_required_table_and_column_names_exist(self):
        from models import HardwareLog, MiningReward, Setting

        self.assertEqual(HardwareLog.__tablename__, "hardware_logs")
        self.assertEqual(MiningReward.__tablename__, "mining_rewards")
        self.assertEqual(Setting.__tablename__, "settings")
        self.assertTrue(hasattr(HardwareLog, "temp_c"))
        self.assertTrue(hasattr(HardwareLog, "power_w"))
        self.assertTrue(hasattr(HardwareLog, "fan_speed"))
        self.assertTrue(hasattr(HardwareLog, "hashrate_th"))
        self.assertTrue(hasattr(HardwareLog, "vram_gb"))
        self.assertTrue(hasattr(MiningReward, "pearl_mined_hour"))
        self.assertTrue(hasattr(Setting, "wallet_address"))
        self.assertTrue(hasattr(Setting, "pool_url"))
        self.assertTrue(hasattr(Setting, "telegram_chat_id"))

    def test_init_db_raises_on_runtime_migration_failure(self):
        import database

        with patch.object(database.Base.metadata, "create_all"), \
            patch.object(database, "_add_missing_columns", side_effect=RuntimeError("migration boom")), \
            patch.object(database, "upsert_default_settings"):
            with self.assertRaises(RuntimeError):
                database.init_db()


class FrontendTests(unittest.TestCase):
    def test_dashboard_contains_required_api_calls(self):
        html = Path("templates/index.html").read_text(encoding="utf-8")
        required = [
            "/api/system/status",
            "/api/gpu/metrics",
            "/api/mining/finance",
            "/api/chart_data",
            "/api/gpu/profile",
            "/api/gpu/profiles",
            "/api/logs/stream",
            "/api/live",
            "/api/admin/snapshot",
            "/api/admin/events",
            "data.shares24h",
        ]
        for endpoint in required:
            self.assertIn(endpoint, html)

    def test_dashboard_sidebar_links_switch_views(self):
        html = Path("templates/index.html").read_text(encoding="utf-8")
        for view in ["overview", "finance", "control", "terminal"]:
            self.assertIn(f'data-view="{view}"', html)
            self.assertIn(f'id="{view}"', html)
        self.assertIn("function showView(view)", html)
        self.assertIn("function setupNavigation()", html)
        self.assertIn("hidden>", html)
        self.assertIn('id="workerRows"', html)

    def test_dashboard_has_floating_terminal_and_control_debounce(self):
        html = Path("templates/index.html").read_text(encoding="utf-8")
        css = Path("static/styles.css").read_text(encoding="utf-8")
        self.assertIn('id="floatingTerminal"', html)
        self.assertIn('id="floatingTerminalLog"', html)
        self.assertIn("function appendLogLine", html)
        self.assertIn("function setControlsDisabled", html)
        self.assertIn("control-action", html)
        self.assertIn(".floating-terminal", css)
        self.assertIn("position: fixed", css)
        self.assertIn("bottom: 18px", css)

    def test_dashboard_has_loading_animation_and_mobile_responsive_states(self):
        html = Path("templates/index.html").read_text(encoding="utf-8")
        css = Path("static/styles.css").read_text(encoding="utf-8")
        for marker in ["bootLoader", "liveChip", "finishInitialLoading", "value-updated", "setupResponsiveDefaults"]:
            self.assertIn(marker, html)
        for marker in ["@keyframes shimmer", "@keyframes viewIn", "@media (max-width: 760px)", "prefers-reduced-motion"]:
            self.assertIn(marker, css)

    def test_dashboard_inline_script_can_be_extracted(self):
        html = Path("templates/index.html").read_text(encoding="utf-8")
        scripts = re.findall(r"<script>(.*?)</script>", html, flags=re.S)
        self.assertGreaterEqual(len(scripts), 1)
        self.assertIn("refreshAll", scripts[-1])
        self.assertIn("startLiveMetrics", scripts[-1])
        self.assertIn("new EventSource(authUrl('/api/live'))", scripts[-1])
        self.assertIn("refreshSnapshot", scripts[-1])
        self.assertIn("renderEvents", scripts[-1])
        self.assertIn("authUrl('/api/live')", scripts[-1])
        self.assertIn("authUrl('/api/logs/stream')", scripts[-1])
        self.assertIn("dashboard_auth_required", scripts[-1])
        self.assertIn("URLSearchParams(window.location.search).get('token')", scripts[-1])
        self.assertIn("history.replaceState", scripts[-1])


class ApiTests(unittest.TestCase):
    def test_invalid_control_returns_json_error(self):
        from fastapi.testclient import TestClient
        import app

        client = TestClient(app.app)
        response = client.post("/api/control/invalid")
        self.assertEqual(response.status_code, 400)
        self.assertIn("application/json", response.headers["content-type"])

    def test_security_headers_present(self):
        from fastapi.testclient import TestClient
        import app

        client = TestClient(app.app)
        response = client.get("/api/health")
        self.assertEqual(response.headers["x-content-type-options"], "nosniff")
        self.assertEqual(response.headers["x-frame-options"], "DENY")

    def test_control_requires_token_for_non_local_request(self):
        from starlette.datastructures import Headers, QueryParams
        import app

        request = type("Req", (), {})()
        request.client = type("Client", (), {"host": "192.0.2.10"})()
        request.headers = Headers({})
        request.query_params = QueryParams("")
        with patch.object(app, "load_config", return_value={"CONTROL_API_TOKEN": ""}):
            with self.assertRaises(Exception):
                app.require_control_access(request)

    def test_control_token_allows_remote_request(self):
        from starlette.datastructures import Headers, QueryParams
        import app

        request = type("Req", (), {})()
        request.client = type("Client", (), {"host": "192.0.2.10"})()
        request.headers = Headers({"authorization": "Bearer secret"})
        request.query_params = QueryParams("")
        with patch.object(app, "load_config", return_value={"CONTROL_API_TOKEN": "secret"}):
            app.require_control_access(request)

    def test_dashboard_requires_token_for_non_local_request(self):
        from starlette.datastructures import Headers, QueryParams
        import app

        request = type("Req", (), {})()
        request.client = type("Client", (), {"host": "192.0.2.10"})()
        request.headers = Headers({})
        request.query_params = QueryParams("")
        with patch.object(app, "load_config", return_value={"CONTROL_API_TOKEN": ""}):
            with self.assertRaises(Exception):
                app.require_dashboard_access(request)

    def test_dashboard_token_allows_remote_request(self):
        from starlette.datastructures import Headers, QueryParams
        import app

        request = type("Req", (), {})()
        request.client = type("Client", (), {"host": "192.0.2.10"})()
        request.headers = Headers({})
        request.query_params = QueryParams("token=secret")
        with patch.object(app, "load_config", return_value={"CONTROL_API_TOKEN": "secret"}):
            app.require_dashboard_access(request)

    def test_cloudflare_tunnel_headers_are_not_treated_as_local(self):
        from starlette.datastructures import Headers, QueryParams
        import app

        request = type("Req", (), {})()
        request.client = type("Client", (), {"host": "127.0.0.1"})()
        request.headers = Headers({"cf-ray": "test-ray", "cf-connecting-ip": "203.0.113.10"})
        request.query_params = QueryParams("")
        with patch.object(app, "load_config", return_value={"CONTROL_API_TOKEN": ""}):
            with self.assertRaises(Exception):
                app.require_dashboard_access(request)

        request.query_params = QueryParams("token=secret")
        with patch.object(app, "load_config", return_value={"CONTROL_API_TOKEN": "secret"}):
            app.require_dashboard_access(request)

    def test_profiles_endpoint_uses_dashboard_access_policy(self):
        from fastapi.testclient import TestClient
        import app

        client = TestClient(app.app, client=("192.0.2.10", 12345))
        with patch.object(app, "load_config", return_value={"CONTROL_API_TOKEN": ""}):
            response = client.get("/api/gpu/profiles")
        self.assertEqual(response.status_code, 403)

        with patch.object(app, "load_config", return_value={"CONTROL_API_TOKEN": "secret"}):
            response = client.get("/api/gpu/profiles?token=secret")
        self.assertEqual(response.status_code, 200)

    def test_dashboard_html_uses_dashboard_access_policy(self):
        from fastapi.testclient import TestClient
        import app

        client = TestClient(app.app, client=("192.0.2.10", 12345))
        with patch.object(app, "load_config", return_value={"CONTROL_API_TOKEN": ""}):
            response = client.get("/")
        self.assertEqual(response.status_code, 403)

        with patch.object(app, "load_config", return_value={"CONTROL_API_TOKEN": "secret"}):
            response = client.get("/?token=secret")
        self.assertEqual(response.status_code, 200)

    def test_gpu_payload_zeroes_hashrate_when_service_stopped(self):
        import app

        with patch.object(app, "get_gpu_metrics", return_value={"available": True, "temp_c": 50}), \
            patch.object(app, "fetch_pool_miner_stats", return_value={"available": True, "hashrate_hps": 10e12}), \
            patch.object(app, "get_miner_status", return_value={"status": "Đã dừng", "is_active": False, "process_running": False}), \
            patch.object(app, "estimate_revenue", return_value={"prl_24h": 0.0, "prl_7d": 0.0, "usd_24h": 0.0, "vnd_24h": 0.0, "assessment": "Miner đang dừng"}), \
            patch.object(app, "today_reward_prl", return_value=0.0), \
            patch.object(app, "_finance_payload", return_value={}):
            payload = app._gpu_payload()

        self.assertEqual(payload["hashrate_th"], 0.0)
        self.assertEqual(payload["hashrate_label"], "0 H/s")

    def test_live_payload_contains_dashboard_sections(self):
        import app

        snapshot = {
            "timestamp": "2026-06-06T00:00:00+00:00",
            "system": {"status": "Đang chạy", "is_active": True, "process_running": True, "details": ""},
            "gpu": {"available": True, "gpu_name": "RTX 3060", "temp_c": 55},
            "local_miner": {"hashrate_hps": 2e12, "stale": False},
            "pool_miner": {"available": True, "hashrate_hps": 1e12, "balance_prl": 1.0},
            "pool": {"available": True},
            "price": {"price_usd": 0.5, "price_vnd": 12500, "source": "test"},
            "effective_hashrate": {"hashrate_hps": 2e12, "hashrate_th": 2.0, "hashrate_label": "2.00 TH/s", "source": "local", "stale": False},
            "prediction": {"prl_24h": 1.0, "prl_7d": 7.0, "usd_24h": 0.5, "vnd_24h": 12500, "assessment": "OK"},
            "finance": {"balance_prl": 1.0, "balance_usd": 0.5, "price": {"price_usd": 0.5, "raw": {"secret": True}}},
            "safety": {"level": "ok", "reasons": []},
        }
        with patch.object(app, "collect_telemetry_snapshot", return_value=snapshot), \
            patch.object(app, "today_reward_prl", return_value=0.25):
            payload = app._live_payload()

        self.assertIn("system", payload)
        self.assertIn("gpu", payload)
        self.assertIn("finance", payload)
        self.assertEqual(payload["gpu"]["hashrate_label"], "2.00 TH/s")
        self.assertEqual(payload["gpu"]["hashrate_source"], "local")
        self.assertEqual(payload["finance"]["balance_usd"], 0.5)
        self.assertNotIn("raw", payload["finance"]["price"])

    def test_admin_snapshot_sanitizes_raw_external_payloads(self):
        import app

        snapshot = {
            "pool_miner": {"available": True, "raw": {"secret": True}},
            "pool": {"available": True, "raw": {"secret": True}},
            "price": {"price_usd": 1.0, "raw": {"secret": True}},
            "finance": {"price": {"price_usd": 1.0, "raw": {"secret": True}}},
        }
        request = unittest.mock.Mock()
        with patch.object(app, "collect_telemetry_snapshot", return_value=snapshot), \
            patch.object(app, "require_dashboard_access"):
            payload = app.api_admin_snapshot(request)

        self.assertNotIn("raw", payload["pool_miner"])
        self.assertNotIn("raw", payload["pool"])
        self.assertNotIn("raw", payload["price"])
        self.assertNotIn("raw", payload["finance"]["price"])


class DeploymentScriptTests(unittest.TestCase):
    def _write_minimal_config(self, path: Path, wallet: str = "prl1testwallet", service: str = "pearl-miner.service") -> None:
        path.write_text(
            "\n".join(
                [
                    "WEB_HOST=127.0.0.1",
                    "WEB_PORT=8555",
                    "DB_USER=postgres",
                    "DB_PASS=postgres",
                    "DB_NAME=pearl_db",
                    f"WALLET_ADDRESS={wallet}",
                    "WORKER_NAME=Rig",
                    "POOL_HOST=sg1.alphapool.tech",
                    "POOL_PORT=5566",
                    "MINER_TYPE=alpha",
                    "MINER_DIR=/home/ntd/Downloads/alpha-miner",
                    "MINER_EXEC=/home/ntd/Downloads/alpha-miner/alpha-miner",
                    "MINER_ALGORITHM=pearlhash",
                    'MINER_PASSWORD="x;d=65536"',
                    'MINER_EXTRA_ARGS="--status-interval 60"',
                    f"MINER_SERVICE={service}",
                    "WEB_SERVICE=pearl-web.service",
                    "BOT_SERVICE=pearl-bot.service",
                    "DISPLAY=:0",
                    "XAUTHORITY=",
                ]
            ),
            encoding="utf-8",
        )

    def test_dry_run_service_files_handle_extra_args_and_percent(self):
        repo = Path.cwd()
        if shutil.which("systemd-analyze") is None or shutil.which("visudo") is None:
            self.skipTest("systemd-analyze or visudo unavailable")
        with tempfile.TemporaryDirectory() as tmp:
            dry_run = Path(tmp) / "dry"
            config_file = Path(tmp) / "config.env"
            config_file.write_text(
                "\n".join(
                    [
                        "WEB_HOST=127.0.0.1",
                        "WEB_PORT=8555",
                        "DB_USER=postgres",
                        "DB_PASS=postgres",
                        "DB_NAME=pearl_db",
                        "WALLET_ADDRESS=prl1testwallet",
                        "WORKER_NAME=Rig",
                        "POOL_HOST=sg1.alphapool.tech",
                        "POOL_PORT=5566",
                        "MINER_TYPE=srbminer",
                        "MINER_DIR=/home/ntd/Downloads/SRBMiner-Multi-3-3-4",
                        "MINER_EXEC=/home/ntd/Downloads/SRBMiner-Multi-3-3-4/SRBMiner-MULTI",
                        "MINER_ALGORITHM=pearlhash",
                        'MINER_EXTRA_ARGS="--api-enable --user-note=100%"',
                        "MINER_SERVICE=pearl-miner.service",
                        "WEB_SERVICE=pearl-web.service",
                        "BOT_SERVICE=pearl-bot.service",
                        "DISPLAY=:0",
                        "XAUTHORITY=",
                    ]
                ),
                encoding="utf-8",
            )
            env = os.environ.copy()
            env.update(
                {
                    "DRY_RUN": "1",
                    "DRY_RUN_DIR": str(dry_run),
                    "PEARL_CONFIG": str(config_file),
                }
            )
            result = subprocess.run([str(repo / "deploy.sh")], cwd=repo, env=env, capture_output=True, text=True, timeout=30)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            miner_service = (dry_run / "systemd" / "pearl-miner.service").read_text(encoding="utf-8")
            sudoers = (dry_run / "sudoers.d" / "pearl-miner-manager").read_text(encoding="utf-8")
            self.assertIn('"--api-enable"', miner_service)
            self.assertIn('"--user-note=100%%"', miner_service)
            self.assertNotIn("nvidia-smi *", sudoers)
            for limit in ["100", "115", "130"]:
                self.assertIn(f"--id=0 --power-limit={limit}", sudoers)

    def test_dry_run_alpha_miner_execstart_uses_alphapool_protocol(self):
        repo = Path.cwd()
        if shutil.which("systemd-analyze") is None or shutil.which("visudo") is None:
            self.skipTest("systemd-analyze or visudo unavailable")
        with tempfile.TemporaryDirectory() as tmp:
            dry_run = Path(tmp) / "dry"
            config_file = Path(tmp) / "config.env"
            self._write_minimal_config(config_file)
            env = os.environ.copy()
            env.update({"DRY_RUN": "1", "DRY_RUN_DIR": str(dry_run), "CONFIG_FILE": str(config_file)})
            result = subprocess.run([str(repo / "deploy.sh")], cwd=repo, env=env, capture_output=True, text=True, timeout=30)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            miner_service = (dry_run / "systemd" / "pearl-miner.service").read_text(encoding="utf-8")
            self.assertIn('"/home/ntd/Downloads/alpha-miner/alpha-miner"', miner_service)
            self.assertIn('"stratum+tcp://sg1.alphapool.tech:5566"', miner_service)
            self.assertIn('"--address"', miner_service)
            self.assertIn('"--worker"', miner_service)
            self.assertIn('"x;d=65536"', miner_service)
            self.assertIn('"--power-limit=115"', miner_service)
            self.assertIn('"--lock-gpu-clocks=1450,1450"', miner_service)
            self.assertNotIn("nvidia-settings", miner_service)

    def test_benchmark_dry_run_builds_both_miner_commands(self):
        repo = Path.cwd()
        with tempfile.TemporaryDirectory() as tmp:
            alpha = Path(tmp) / "alpha-miner"
            srb = Path(tmp) / "SRBMiner-MULTI"
            alpha.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            srb.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            alpha.chmod(0o755)
            srb.chmod(0o755)
            config_file = Path(tmp) / "config.env"
            self._write_minimal_config(config_file, wallet="prl1benchwallet")
            env = os.environ.copy()
            env.update(
                {
                    "PEARL_CONFIG": str(config_file),
                    "ALPHA_MINER_EXEC": str(alpha),
                    "SRBMINER_EXEC": str(srb),
                }
            )
            result = subprocess.run(
                [str(repo / "benchmark_miners.sh"), "--dry-run", "--duration", "60", "--yes"],
                cwd=repo,
                env=env,
                capture_output=True,
                text=True,
                timeout=15,
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn("alpha-miner:", result.stdout)
            self.assertIn("SRBMiner:", result.stdout)
            self.assertIn("--pool stratum+tcp://sg1.alphapool.tech:5566", result.stdout)
            self.assertIn("--algorithm pearlhash", result.stdout)
            self.assertIn("--wallet prl1benchwallet.Rig", result.stdout)

    def test_dry_run_uses_config_file_for_oc_profiles(self):
        repo = Path.cwd()
        if shutil.which("systemd-analyze") is None or shutil.which("visudo") is None:
            self.skipTest("systemd-analyze or visudo unavailable")
        with tempfile.TemporaryDirectory() as tmp:
            dry_run = Path(tmp) / "dry"
            config_file = Path(tmp) / "custom.env"
            config_file.write_text(
                "\n".join(
                    [
                        "WEB_HOST=127.0.0.1",
                        "WEB_PORT=8555",
                        "DB_USER=postgres",
                        "DB_PASS=postgres",
                        "DB_NAME=pearl_db",
                        "WALLET_ADDRESS=prl1testwallet",
                        "WORKER_NAME=Rig",
                        "POOL_HOST=sg1.alphapool.tech",
                        "POOL_PORT=5566",
                        "MINER_DIR=/home/ntd/Downloads/SRBMiner-Multi-3-3-4",
                        "MINER_EXEC=/home/ntd/Downloads/SRBMiner-Multi-3-3-4/SRBMiner-MULTI",
                        "MINER_ALGORITHM=pearlhash",
                        "MINER_SERVICE=pearl-miner.service",
                        "WEB_SERVICE=pearl-web.service",
                        "BOT_SERVICE=pearl-bot.service",
                        "DISPLAY=:0",
                        "XAUTHORITY=",
                        "OC_PROFILES_JSON='{\"eco\":{\"power_limit\":101,\"gpu_clock_lock\":\"1201,1201\"},\"max\":{\"power_limit\":102,\"gpu_clock_min\":1202,\"gpu_clock_max\":1202}}'",
                        "STARTUP_OC_PROFILE=max",
                    ]
                ),
                encoding="utf-8",
            )
            env = os.environ.copy()
            env.update({"DRY_RUN": "1", "DRY_RUN_DIR": str(dry_run), "CONFIG_FILE": str(config_file)})
            result = subprocess.run([str(repo / "deploy.sh")], cwd=repo, env=env, capture_output=True, text=True, timeout=30)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            miner_service = (dry_run / "systemd" / "pearl-miner.service").read_text(encoding="utf-8")
            sudoers = (dry_run / "sudoers.d" / "pearl-miner-manager").read_text(encoding="utf-8")
            self.assertIn('"--power-limit=102"', miner_service)
            self.assertIn('"--lock-gpu-clocks=1202,1202"', miner_service)
            self.assertIn("--id=0 --power-limit=101", sudoers)
            self.assertIn("--id=0 --power-limit=102", sudoers)
            self.assertIn("--id=0 --lock-gpu-clocks=1201\\,1201", sudoers)
            self.assertIn("--id=0 --lock-gpu-clocks=1202\\,1202", sudoers)
            self.assertNotIn("--id=0 --power-limit=115", sudoers)

    def test_dry_run_can_be_repeated_in_same_output_dir(self):
        repo = Path.cwd()
        if shutil.which("systemd-analyze") is None or shutil.which("visudo") is None:
            self.skipTest("systemd-analyze or visudo unavailable")
        with tempfile.TemporaryDirectory() as tmp:
            dry_run = Path(tmp) / "dry"
            config_file = Path(tmp) / "config.env"
            self._write_minimal_config(config_file)
            env = os.environ.copy()
            env.update({"DRY_RUN": "1", "DRY_RUN_DIR": str(dry_run), "CONFIG_FILE": str(config_file)})
            for _ in range(2):
                result = subprocess.run([str(repo / "deploy.sh")], cwd=repo, env=env, capture_output=True, text=True, timeout=30)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            sudoers = dry_run / "sudoers.d" / "pearl-miner-manager"
            self.assertTrue(sudoers.exists())

    def test_dry_run_sudoers_matches_runtime_scripts(self):
        repo = Path.cwd()
        if shutil.which("systemd-analyze") is None or shutil.which("visudo") is None:
            self.skipTest("systemd-analyze or visudo unavailable")
        with tempfile.TemporaryDirectory() as tmp:
            dry_run = Path(tmp) / "dry"
            config_file = Path(tmp) / "config.env"
            self._write_minimal_config(config_file)
            env = os.environ.copy()
            env.update({"DRY_RUN": "1", "DRY_RUN_DIR": str(dry_run), "CONFIG_FILE": str(config_file)})
            result = subprocess.run([str(repo / "deploy.sh")], cwd=repo, env=env, capture_output=True, text=True, timeout=30)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            sudoers = (dry_run / "sudoers.d" / "pearl-miner-manager").read_text(encoding="utf-8")
            for command in [
                "daemon-reload",
                "start postgresql.service",
                "start pearl-web.service",
                "stop pearl-web.service",
                "restart pearl-web.service",
                "start pearl-bot.service",
                "stop pearl-bot.service",
                "restart pearl-bot.service",
            ]:
                self.assertIn(command, sudoers)

    def test_dry_run_rejects_malicious_gpu_index(self):
        repo = Path.cwd()
        with tempfile.TemporaryDirectory() as tmp:
            dry_run = Path(tmp) / "dry"
            config_file = Path(tmp) / "config.env"
            self._write_minimal_config(config_file)
            with config_file.open("a", encoding="utf-8") as handle:
                handle.write("\nGPU_INDEX=0, /bin/sh -c id #\n")
            env = os.environ.copy()
            env.update({"DRY_RUN": "1", "DRY_RUN_DIR": str(dry_run), "CONFIG_FILE": str(config_file)})
            result = subprocess.run([str(repo / "deploy.sh")], cwd=repo, env=env, capture_output=True, text=True, timeout=30)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("Invalid GPU_INDEX", result.stderr)
            sudoers = dry_run / "sudoers.d" / "pearl-miner-manager"
            self.assertFalse(sudoers.exists())

    def test_start_all_refuses_placeholder_wallet_before_sudo(self):
        repo = Path.cwd()
        with tempfile.TemporaryDirectory() as tmp:
            temp_repo = Path(tmp) / "repo"
            shutil.copytree(repo, temp_repo, ignore=shutil.ignore_patterns("venv", ".git", "__pycache__", ".codegraph"))
            self._write_minimal_config(temp_repo / "config.env", wallet="CHANGE_ME_PEARL_WALLET")
            result = subprocess.run(["bash", "start_all.sh"], cwd=temp_repo, capture_output=True, text=True, timeout=10)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("Set WALLET_ADDRESS", result.stderr)
            self.assertNotIn("sudo", result.stderr.lower())

    def test_start_all_does_not_execute_config_command_substitution(self):
        repo = Path.cwd()
        with tempfile.TemporaryDirectory() as tmp:
            temp_repo = Path(tmp) / "repo"
            marker = Path(tmp) / "pwned"
            shutil.copytree(repo, temp_repo, ignore=shutil.ignore_patterns("venv", ".git", "__pycache__", ".codegraph", "config.env"))
            config_file = Path(tmp) / "custom.env"
            self._write_minimal_config(config_file, service="../bad.service")
            text = config_file.read_text(encoding="utf-8")
            config_file.write_text(text.replace("WALLET_ADDRESS=prl1testwallet", f'WALLET_ADDRESS="$(touch {marker})"'), encoding="utf-8")
            env = os.environ.copy()
            env["CONFIG_FILE"] = str(config_file)
            result = subprocess.run(["bash", "start_all.sh"], cwd=temp_repo, env=env, capture_output=True, text=True, timeout=10)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("Invalid systemd service name", result.stderr)
            self.assertFalse(marker.exists())

    def test_start_all_rejects_invalid_config_line_without_execution(self):
        repo = Path.cwd()
        with tempfile.TemporaryDirectory() as tmp:
            temp_repo = Path(tmp) / "repo"
            marker = Path(tmp) / "pwned"
            shutil.copytree(repo, temp_repo, ignore=shutil.ignore_patterns("venv", ".git", "__pycache__", ".codegraph", "config.env"))
            config_file = Path(tmp) / "custom.env"
            self._write_minimal_config(config_file)
            with config_file.open("a", encoding="utf-8") as handle:
                handle.write(f"\ntouch {marker}\n")
            env = os.environ.copy()
            env["CONFIG_FILE"] = str(config_file)
            result = subprocess.run(["bash", "start_all.sh"], cwd=temp_repo, env=env, capture_output=True, text=True, timeout=10)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("Invalid config line", result.stderr)
            self.assertFalse(marker.exists())

    def test_start_all_rejects_bad_service_name_before_sudo(self):
        repo = Path.cwd()
        with tempfile.TemporaryDirectory() as tmp:
            temp_repo = Path(tmp) / "repo"
            shutil.copytree(repo, temp_repo, ignore=shutil.ignore_patterns("venv", ".git", "__pycache__", ".codegraph"))
            self._write_minimal_config(temp_repo / "config.env", service="../bad.service")
            result = subprocess.run(["bash", "start_all.sh"], cwd=temp_repo, capture_output=True, text=True, timeout=10)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("Invalid systemd service name", result.stderr)

    def test_start_all_uses_config_file_override(self):
        repo = Path.cwd()
        with tempfile.TemporaryDirectory() as tmp:
            temp_repo = Path(tmp) / "repo"
            shutil.copytree(repo, temp_repo, ignore=shutil.ignore_patterns("venv", ".git", "__pycache__", ".codegraph", "config.env"))
            custom_config = Path(tmp) / "custom.env"
            self._write_minimal_config(custom_config, service="../bad.service")
            env = os.environ.copy()
            env["CONFIG_FILE"] = str(custom_config)
            result = subprocess.run(["bash", "start_all.sh"], cwd=temp_repo, env=env, capture_output=True, text=True, timeout=10)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("Invalid systemd service name", result.stderr)

    def test_stop_all_uses_config_file_override(self):
        repo = Path.cwd()
        with tempfile.TemporaryDirectory() as tmp:
            temp_repo = Path(tmp) / "repo"
            shutil.copytree(repo, temp_repo, ignore=shutil.ignore_patterns("venv", ".git", "__pycache__", ".codegraph", "config.env"))
            custom_config = Path(tmp) / "custom.env"
            self._write_minimal_config(custom_config, service="../bad.service")
            env = os.environ.copy()
            env["PEARL_CONFIG"] = str(custom_config)
            result = subprocess.run(["bash", "stop_all.sh"], cwd=temp_repo, env=env, capture_output=True, text=True, timeout=10)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("Invalid systemd service name", result.stderr)

    def test_pearl_manager_uses_config_file_override_before_menu(self):
        repo = Path.cwd()
        with tempfile.TemporaryDirectory() as tmp:
            temp_repo = Path(tmp) / "repo"
            shutil.copytree(repo, temp_repo, ignore=shutil.ignore_patterns("venv", ".git", "__pycache__", ".codegraph", "config.env"))
            custom_config = Path(tmp) / "custom.env"
            self._write_minimal_config(custom_config, service="../bad.service")
            fake_bin = Path(tmp) / "bin"
            fake_bin.mkdir()
            fake_whiptail = fake_bin / "whiptail"
            fake_whiptail.write_text(
                "#!/usr/bin/env bash\n"
                "printf '%s\\n' \"$*\" >&2\n"
                "case \" $* \" in *\" --menu \"*) exit 1 ;; *) exit 0 ;; esac\n",
                encoding="utf-8",
            )
            fake_whiptail.chmod(0o755)
            env = os.environ.copy()
            env["CONFIG_FILE"] = str(custom_config)
            env["PATH"] = f"{fake_bin}{os.pathsep}{env.get('PATH', '')}"
            result = subprocess.run(["bash", "pearl-manager.sh"], cwd=temp_repo, env=env, capture_output=True, text=True, timeout=10)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("Invalid service name", result.stderr)

    def test_verify_uses_config_file_override_and_validates_port(self):
        repo = Path.cwd()
        with tempfile.TemporaryDirectory() as tmp:
            temp_repo = Path(tmp) / "repo"
            marker = Path(tmp) / "pwned"
            shutil.copytree(repo, temp_repo, ignore=shutil.ignore_patterns("venv", ".git", "__pycache__", ".codegraph", "config.env"))
            config_file = Path(tmp) / "custom.env"
            self._write_minimal_config(config_file)
            with config_file.open("a", encoding="utf-8") as handle:
                handle.write(f"\nWEB_PORT=8555; touch {marker}\n")
            env = os.environ.copy()
            env["CONFIG_FILE"] = str(config_file)
            result = subprocess.run(["bash", "verify_deploy.sh"], cwd=temp_repo, env=env, capture_output=True, text=True, timeout=10)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("Invalid WEB_PORT", result.stdout + result.stderr)
            self.assertFalse(marker.exists())


if __name__ == "__main__":
    unittest.main()
