import unittest
from datetime import datetime, timedelta
from unittest.mock import patch

import main


class EgxPriceDataTests(unittest.TestCase):
    def setUp(self):
        self.start = datetime(2026, 1, 1)
        self.history = [
            {
                "tradeDate": (self.start + timedelta(days=index)).isoformat(),
                "closePrice": 500 + index,
                "tradeVolume": 1000,
            }
            for index in range(60)
        ]

    def test_same_day_quote_replaces_history_without_rescaling_price(self):
        quote = {
            "lastTradeDate": self.history[-1]["tradeDate"],
            "lastPrice": 559,
            "volume": 5000,
        }

        frame = main.build_egx_price_frame(self.history, quote)

        self.assertEqual(len(frame), 60)
        self.assertEqual(frame.iloc[-1]["Close"], 559)
        self.assertEqual(frame.iloc[-1]["Volume"], 5000)

    def test_newer_quote_is_appended_but_stale_quote_is_ignored(self):
        next_day = (self.start + timedelta(days=60)).isoformat()
        frame = main.build_egx_price_frame(
            self.history,
            {"lastTradeDate": next_day, "lastPrice": 560, "volume": 1200},
        )
        self.assertEqual(len(frame), 61)
        self.assertEqual(frame.iloc[-1]["Close"], 560)

        stale = main.build_egx_price_frame(
            self.history,
            {
                "lastTradeDate": (self.start + timedelta(days=58)).isoformat(),
                "lastPrice": 999,
                "volume": 9000,
            },
        )
        self.assertEqual(len(stale), 60)
        self.assertEqual(stale.iloc[-1]["Close"], 559)

    def test_zero_snapshot_volume_does_not_erase_historical_volume(self):
        frame = main.build_egx_price_frame(
            self.history,
            {
                "lastTradeDate": self.history[-1]["tradeDate"],
                "lastPrice": 559,
                "volume": 0,
            },
        )

        self.assertEqual(frame.iloc[-1]["Volume"], 1000)

    def test_analysis_uses_egx_price_points_for_signal(self):
        self.history[-1]["tradeVolume"] = 5000
        quote = {
            "lastTradeDate": self.history[-1]["tradeDate"],
            "lastPrice": 559,
            "volume": 5000,
        }

        result = main.analyze_stock("TEST.CA", quote, self.history)

        self.assertEqual(result["price"], 559)
        self.assertEqual(result["score"], 75)
        self.assertEqual(result["data_date"], self.history[-1]["tradeDate"][:10])
        self.assertIn("RSI", result["current_status"])

    def test_analysis_preserves_three_decimal_egp_prices(self):
        history = [
            {
                "tradeDate": (self.start + timedelta(days=index)).isoformat(),
                "closePrice": 0.2 + index / 1000,
                "tradeVolume": 1000,
            }
            for index in range(60)
        ]
        history[-1]["tradeVolume"] = 5000
        quote = {
            "lastTradeDate": history[-1]["tradeDate"],
            "lastPrice": 0.259,
            "volume": 5000,
        }

        result = main.analyze_stock("TEST.CA", quote, history)

        self.assertEqual(result["price"], 0.259)

    def test_incomplete_history_is_rejected(self):
        with self.assertRaises(ValueError):
            main.build_egx_price_frame([{"tradeDate": "2026-01-01"}], {})

    def test_pattern_forecast_uses_similar_history_for_up_and_down_trends(self):
        rising = [
            {
                "tradeDate": (self.start + timedelta(days=index)).isoformat(),
                "closePrice": 100 + index,
                "tradeVolume": 1000,
            }
            for index in range(300)
        ]
        falling = [
            {
                "tradeDate": (self.start + timedelta(days=index)).isoformat(),
                "closePrice": 500 - index,
                "tradeVolume": 1000,
            }
            for index in range(300)
        ]
        flat = [
            {
                "tradeDate": (self.start + timedelta(days=index)).isoformat(),
                "closePrice": 100,
                "tradeVolume": 1000,
            }
            for index in range(300)
        ]
        quote = {
            "lastTradeDate": rising[-1]["tradeDate"],
            "lastPrice": rising[-1]["closePrice"],
            "volume": rising[-1]["tradeVolume"],
        }

        rising_forecast = main.forecast_similar_patterns("UP.CA", quote, rising)
        falling_quote = {
            **quote,
            "lastPrice": falling[-1]["closePrice"],
        }
        flat_quote = {
            **quote,
            "lastTradeDate": flat[-1]["tradeDate"],
            "lastPrice": flat[-1]["closePrice"],
        }
        falling_forecast = main.forecast_similar_patterns("DOWN.CA", falling_quote, falling)
        flat_forecast = main.forecast_similar_patterns("FLAT.CA", flat_quote, flat)

        self.assertEqual(rising_forecast["direction"], "up")
        self.assertEqual(rising_forecast["horizon"], 5)
        self.assertEqual(rising_forecast["samples"], 15)
        self.assertEqual(falling_forecast["direction"], "down")
        self.assertEqual(flat_forecast["direction"], "neutral")

    def test_pattern_forecast_is_unavailable_without_enough_history(self):
        quote = {
            "lastTradeDate": self.history[-1]["tradeDate"],
            "lastPrice": self.history[-1]["closePrice"],
            "volume": self.history[-1]["tradeVolume"],
        }

        self.assertIsNone(main.forecast_similar_patterns("TEST.CA", quote, self.history))

    def test_forecast_window_skips_egx_weekend(self):
        self.assertEqual(
            main.forecast_session_window("2026-11-12", 5),
            ("2026-11-15", "2026-11-19"),
        )


class SignalNotificationTests(unittest.TestCase):
    def test_successful_signal_is_not_repeated_for_the_same_session(self):
        user = {"last_signals": {}}
        with patch("main.send_msg", return_value={"ok": True}) as send:
            main.send_signal_once(user, "BUY:COMI", "2026-11-12", "123", "signal", [])
            main.send_signal_once(user, "BUY:COMI", "2026-11-12", "123", "signal", [])

        send.assert_called_once()

    def test_failed_signal_send_can_be_retried(self):
        user = {"last_signals": {}}
        with patch("main.send_msg", return_value={}) as send:
            main.send_signal_once(user, "BUY:COMI", "2026-11-12", "123", "signal", [])
            main.send_signal_once(user, "BUY:COMI", "2026-11-12", "123", "signal", [])

        self.assertEqual(send.call_count, 2)
        self.assertNotIn("BUY:COMI", user["last_signals"])

class ForecastNotificationTests(unittest.TestCase):
    def test_summary_includes_current_state_and_estimated_forecast_window(self):
        user = {"last_signals": {}}
        result = {
            "symbol": "COMI",
            "price": 50.125,
            "current_status": "السعر فوق متوسطي 20 و50 جلسة",
            "forecast": {
                "direction": "up",
                "up_rate": 0.67,
                "down_rate": 0.33,
                "mean_return": 0.01,
                "samples": 15,
                "data_date": "2026-11-12",
                "horizon": 5,
            },
        }
        with patch("main.send_msg", return_value={"ok": True}) as send:
            main.send_forecast_summary(user, "123", [result])

        message = send.call_args.args[1]
        self.assertIn("الحالة حسب EGX", message)
        self.assertIn("50.125 ج.م", message)
        self.assertIn("2026-11-15", message)
        self.assertIn("2026-11-19", message)
        self.assertIn("15 حالات مشابهة", message)

class TelegramAccessControlTests(unittest.TestCase):
    def test_unauthorized_user_cannot_create_account_or_run_commands(self):
        db = {"last_update_id": 0, "users": {}}
        update = {
            "update_id": 1,
            "message": {
                "chat": {"id": 456},
                "from": {"id": 456},
                "text": "/start",
            },
        }
        with patch("main.ADMIN_CHAT_ID", "123"), \
                patch("main.tg_request", return_value={"result": [update]}), \
                patch("main.send_msg") as send:
            force_scan_users = main.process_telegram_updates(db)

        self.assertEqual(force_scan_users, [])
        self.assertEqual(db["users"], {})
        send.assert_called_once_with("456", "⛔ هذا البوت خاص وغير مصرح لك باستخدامه.")

    def test_only_the_configured_private_chat_can_request_a_scan(self):
        db = {"last_update_id": 0, "users": {}}
        update = {
            "update_id": 1,
            "message": {
                "chat": {"id": 123},
                "from": {"id": 123},
                "text": "/scan",
            },
        }
        with patch("main.ADMIN_CHAT_ID", "123"), \
                patch("main.tg_request", return_value={"result": [update]}), \
                patch("main.send_msg"):
            force_scan_users = main.process_telegram_updates(db)

        self.assertEqual(force_scan_users, ["123"])
        self.assertIn("123", db["users"])


if __name__ == "__main__":
    unittest.main()
