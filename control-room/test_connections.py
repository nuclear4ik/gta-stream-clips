from datetime import datetime, timezone
import unittest
from unittest.mock import patch

import connections
import transport


class ConnectionsTest(unittest.TestCase):
    def test_unconfigured_route_makes_no_request(self):
        with patch("transport.route_available", return_value=False), patch("transport.check_route") as check:
            result = connections.observe("ES", "TikTok")
        self.assertEqual(result["state"], "unconfigured")
        self.assertIsNone(result["country"])
        check.assert_not_called()

    def test_success_reports_region_for_exact_route_and_platform(self):
        with patch("transport.route_available", return_value=True), patch(
            "transport.check_route", return_value={"country": "ES", "api_status": 401}
        ) as check:
            result = connections.observe("ES", "Reels")
        check.assert_called_once_with("ES", "Reels")
        self.assertEqual((result["state"], result["country"]), ("ok", "ES"))
        self.assertLessEqual(datetime.fromisoformat(result["checked_at"]), datetime.now(timezone.utc))

    def test_wrong_region_stays_failed(self):
        mismatch = transport.ConnectionProblem("Страна выхода US, ожидается ES.")
        with patch("transport.route_available", return_value=True), patch("transport.check_route", side_effect=mismatch):
            result = connections.observe("ES", "YouTube")
        self.assertEqual((result["state"], result["country"]), ("failed", "US"))

    def test_raw_error_text_cannot_reach_panel(self):
        error = transport.ConnectionProblem("http://name:secret@example.test:8765")
        with patch("transport.route_available", return_value=True), patch("transport.check_route", side_effect=error):
            result = connections.observe("US", "YouTube")
        self.assertEqual(result["state"], "failed")
        self.assertNotIn("secret", result["detail"])


if __name__ == "__main__":
    unittest.main()
