"""Regression coverage for automatic releases within the Moscow window."""

import os
import unittest
from datetime import timedelta
from unittest.mock import patch

import schedule


class DenseScheduleTests(unittest.TestCase):
    def test_half_hours_remain_first_choice_then_initial_gaps_are_subdivided(self):
        start = schedule.local_time("2026-11-19T15:00", "RU")
        regular = [start + timedelta(minutes=30 * index) for index in range(10)]
        before = start - timedelta(minutes=1)
        with patch.dict(os.environ, STREAMCUT_YOUTUBE_DAILY_LIMIT="12"):
            self.assertEqual(schedule.available_slot(before, "RU", regular[:-1]), regular[-1])
            first_gap = schedule.available_slot(before, "RU", regular)
            self.assertEqual(first_gap, start + timedelta(minutes=15))
            self.assertTrue(schedule.slot_is_available(first_gap, "RU", regular))
            second_gap = schedule.available_slot(before, "RU", [*regular, first_gap])
            self.assertEqual(second_gap, start + timedelta(minutes=45))
            full_day = [*regular, first_gap, second_gap]
            self.assertFalse(schedule.slot_is_available(start + timedelta(minutes=75), "RU", full_day))
            self.assertEqual(schedule.available_slot(before, "RU", full_day), start + timedelta(days=1))

    def test_existing_slots_and_late_start_leave_only_safe_future_gaps(self):
        start = schedule.local_time("2026-11-19T15:00", "RU")
        after = schedule.local_time("2026-11-19T19:34", "RU")
        reserved = [start, start + timedelta(hours=4, minutes=30)]
        with patch.dict(os.environ, STREAMCUT_YOUTUBE_DAILY_LIMIT="10"):
            self.assertEqual(schedule.available_slot(after, "RU", reserved), after + timedelta(minutes=11))
            self.assertFalse(schedule.slot_is_available(reserved[-1], "RU", reserved))
            self.assertFalse(schedule.slot_is_available(start + timedelta(hours=5), "RU", reserved))

    def test_ru_daily_limit_is_configured_separately_from_other_languages(self):
        with patch.dict(os.environ, STREAMCUT_YOUTUBE_DAILY_LIMIT="3"):
            self.assertEqual(schedule.daily_limit(), 3)
            self.assertEqual(schedule.daily_limit("EN"), 10)
            self.assertEqual(schedule.daily_limit("ESP"), 10)
        for invalid in ("0", "101", "unlimited"):
            with patch.dict(os.environ, STREAMCUT_YOUTUBE_DAILY_LIMIT=invalid):
                with self.assertRaisesRegex(ValueError, "STREAMCUT_YOUTUBE_DAILY_LIMIT"):
                    schedule.daily_limit()
