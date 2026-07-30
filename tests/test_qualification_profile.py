import logging
import os
import random
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import Mock, patch

import ml_gen


class QualificationProfileTests(unittest.TestCase):
    def load_qualification_config(self, **overrides):
        environment = {
            "DRY_RUN": "true",
            "QUALIFICATION_PROFILE": "incremental_ab",
        }
        environment.update(overrides)
        with patch.dict(os.environ, environment, clear=True):
            config = ml_gen.load_config()
        ml_gen.validate_config(config)
        return config

    def build_pair(self, **overrides):
        config = self.load_qualification_config(**overrides)
        profiles = ml_gen.build_entity_profiles(config, random.Random(42))
        pair = [profile for profile in profiles if profile.qualification is not None]
        self.assertEqual(2, len(pair))
        return config, pair

    def test_profile_is_enabled_by_default(self):
        with patch.dict(os.environ, {"DRY_RUN": "true"}, clear=True):
            config = ml_gen.load_config()
        ml_gen.validate_config(config)
        profiles = ml_gen.build_entity_profiles(config, random.Random(42))
        qualification_profiles = [
            profile for profile in profiles if profile.qualification
        ]
        self.assertEqual("incremental_ab", config["qualification_profile"])
        self.assertEqual(2, len(qualification_profiles))
        self.assertEqual(1440, config["qualification_window_minutes"])
        self.assertEqual(1500, config["qualification_new_regime_minutes"])
        self.assertEqual(180, config["qualification_return_minutes"])

    def test_profile_can_be_disabled_explicitly(self):
        with patch.dict(
            os.environ,
            {"DRY_RUN": "true", "QUALIFICATION_PROFILE": "off"},
            clear=True,
        ):
            config = ml_gen.load_config()
        ml_gen.validate_config(config)
        profiles = ml_gen.build_entity_profiles(config, random.Random(42))
        self.assertEqual("", config["qualification_profile"])
        self.assertFalse(any(profile.qualification for profile in profiles))

    def test_pair_has_expected_roles_and_shared_shape(self):
        _, pair = self.build_pair()
        self.assertEqual(
            {"full", "incremental"},
            {profile.qualification["role"] for profile in pair},
        )
        self.assertEqual(pair[0].baseline, pair[1].baseline)
        self.assertTrue(all(not profile.flat for profile in pair))
        self.assertTrue(all(profile.scale_factor == 1.0 for profile in pair))
        self.assertTrue(
            all(
                profile.qualification["seasonality"] == "daily_hour"
                for profile in pair
            )
        )

    def test_phase_boundaries_follow_recurring_regime_timeline(self):
        _, pair = self.build_pair()
        qualification = pair[0].qualification
        start = datetime(2026, 7, 30, 12, 0, tzinfo=timezone.utc)
        qualification["start_time"] = start
        warmup_end = qualification["warmup_minutes"]
        new_regime_end = (
            qualification["warmup_minutes"]
            + qualification["new_regime_minutes"]
        )
        return_end = new_regime_end + qualification["return_minutes"]

        cases = (
            (start - timedelta(minutes=1), "bootstrap", "A", None),
            (start, "warmup", "A", 0),
            (
                start + timedelta(minutes=warmup_end, seconds=-1),
                "warmup",
                "A",
                warmup_end - 1,
            ),
            (
                start + timedelta(minutes=warmup_end),
                "new_regime",
                "B",
                warmup_end,
            ),
            (
                start + timedelta(minutes=new_regime_end, seconds=-1),
                "new_regime",
                "B",
                new_regime_end - 1,
            ),
            (
                start + timedelta(minutes=new_regime_end),
                "return_regime",
                "A",
                new_regime_end,
            ),
            (
                start + timedelta(minutes=return_end),
                "complete",
                "A",
                return_end,
            ),
        )
        for timestamp, expected_phase, expected_regime, expected_elapsed in cases:
            phase, regime, _, elapsed = ml_gen.qualification_phase_at(
                timestamp, qualification
            )
            self.assertEqual(expected_phase, phase)
            self.assertEqual(expected_regime, regime)
            self.assertEqual(expected_elapsed, elapsed)

    def test_pair_values_are_exactly_equal_and_repeatable(self):
        _, pair = self.build_pair()
        start = datetime(2026, 7, 30, 12, 0, tzinfo=timezone.utc)
        for profile in pair:
            profile.qualification["start_time"] = start
        qualification = pair[0].qualification
        new_regime_end = (
            qualification["warmup_minutes"]
            + qualification["new_regime_minutes"]
        )
        return_end = new_regime_end + qualification["return_minutes"]

        for offset in (
            -60,
            0,
            qualification["warmup_minutes"] * 60,
            new_regime_end * 60,
            return_end * 60,
        ):
            timestamp = start + timedelta(seconds=offset)
            full = ml_gen.generate_metric(
                timestamp, pair[0], 75, "test", "curve", random.Random(1)
            )
            incremental = ml_gen.generate_metric(
                timestamp, pair[1], 75, "test", "curve", random.Random(999)
            )
            self.assertEqual(full["dcount_hosts"], incremental["dcount_hosts"])
            self.assertEqual(full["events_count"], incremental["events_count"])
            self.assertEqual(
                full["qualification_phase"],
                incremental["qualification_phase"],
            )
            self.assertEqual(
                full["qualification_regime"],
                incremental["qualification_regime"],
            )
            self.assertEqual("daily_hour", full["qualification_seasonality"])

            repeated = ml_gen.generate_metric(
                timestamp, pair[0], 0, "test", "curve", random.Random(123)
            )
            self.assertEqual(full["dcount_hosts"], repeated["dcount_hosts"])
            self.assertEqual(full["events_count"], repeated["events_count"])

    def test_pair_has_material_daily_hour_seasonality(self):
        _, pair = self.build_pair()
        profile = pair[0]
        profile.qualification["start_time"] = datetime(
            2026, 8, 1, tzinfo=timezone.utc
        )
        low = ml_gen.generate_metric(
            datetime(2026, 7, 30, 6, 0, tzinfo=timezone.utc),
            profile,
            0,
            "test",
            "curve",
            random.Random(),
        )
        high = ml_gen.generate_metric(
            datetime(2026, 7, 30, 18, 0, tzinfo=timezone.utc),
            profile,
            0,
            "test",
            "curve",
            random.Random(),
        )
        self.assertGreater(high["events_count"], low["events_count"] * 2.5)

    def test_regime_b_is_materially_higher_than_regime_a(self):
        _, pair = self.build_pair()
        profile = pair[0]
        start = datetime(2026, 7, 30, 12, 0, tzinfo=timezone.utc)
        profile.qualification["start_time"] = start

        regime_a = ml_gen.generate_metric(
            start, profile, 0, "test", "curve", random.Random()
        )
        regime_b = ml_gen.generate_metric(
            start + timedelta(minutes=10),
            profile,
            0,
            "test",
            "curve",
            random.Random(),
        )
        self.assertGreater(
            regime_b["events_count"],
            regime_a["events_count"] * 1.3,
        )

    def test_checkpoints_are_logged_once_per_pair_and_phase(self):
        _, pair = self.build_pair()
        start = datetime(2026, 7, 30, 12, 0, tzinfo=timezone.utc)
        for profile in pair:
            profile.qualification["start_time"] = start
        logger = Mock(spec=logging.Logger)
        previous_phases = {}

        ml_gen.log_qualification_checkpoints(
            pair, start, previous_phases, logger
        )
        ml_gen.log_qualification_checkpoints(
            pair, start + timedelta(minutes=1), previous_phases, logger
        )
        ml_gen.log_qualification_checkpoints(
            pair, start + timedelta(minutes=10), previous_phases, logger
        )

        self.assertEqual(2, logger.info.call_count)
        self.assertEqual("new_regime", previous_phases["recurring_regime_01"])

    def test_timeline_initialization_uses_configured_utc_time(self):
        config, pair = self.build_pair(
            QUALIFICATION_START_TIME="2026-07-30T12:00:00+02:00"
        )
        ml_gen.initialize_qualification_timeline(
            config, pair, Mock(spec=logging.Logger)
        )
        expected = datetime(2026, 7, 30, 10, 0, tzinfo=timezone.utc)
        self.assertTrue(
            all(profile.qualification["start_time"] == expected for profile in pair)
        )

    def test_invalid_profile_and_timeline_are_rejected(self):
        config = self.load_qualification_config()
        config["qualification_profile"] = "unknown"
        with self.assertRaisesRegex(ValueError, "QUALIFICATION_PROFILE"):
            ml_gen.validate_config(config)

        config = self.load_qualification_config()
        config["qualification_new_regime_minutes"] = config[
            "qualification_window_minutes"
        ]
        with self.assertRaisesRegex(
            ValueError, "must be greater than QUALIFICATION_WINDOW_MINUTES"
        ):
            ml_gen.validate_config(config)


if __name__ == "__main__":
    unittest.main()
