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

    def test_profile_is_opt_in(self):
        with patch.dict(os.environ, {"DRY_RUN": "true"}, clear=True):
            config = ml_gen.load_config()
        ml_gen.validate_config(config)
        profiles = ml_gen.build_entity_profiles(config, random.Random(42))
        self.assertFalse(any(profile.qualification for profile in profiles))

    def test_pair_has_expected_roles_and_shared_shape(self):
        _, pair = self.build_pair()
        self.assertEqual(
            {"full", "incremental"},
            {profile.qualification["role"] for profile in pair},
        )
        self.assertEqual(pair[0].baseline, pair[1].baseline)
        self.assertTrue(all(profile.flat for profile in pair))
        self.assertTrue(all(profile.scale_factor == 1.0 for profile in pair))

    def test_phase_boundaries_follow_recurring_regime_timeline(self):
        _, pair = self.build_pair()
        qualification = pair[0].qualification
        start = datetime(2026, 7, 30, 12, 0, tzinfo=timezone.utc)
        qualification["start_time"] = start

        cases = (
            (start - timedelta(minutes=1), "bootstrap", "A", None),
            (start, "warmup", "A", 0),
            (start + timedelta(minutes=9, seconds=59), "warmup", "A", 9),
            (start + timedelta(minutes=10), "new_regime", "B", 10),
            (start + timedelta(minutes=49, seconds=59), "new_regime", "B", 49),
            (start + timedelta(minutes=50), "return_regime", "A", 50),
            (start + timedelta(minutes=65), "complete", "A", 65),
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

        for offset in (-60, 0, 10 * 60, 50 * 60, 65 * 60):
            timestamp = start + timedelta(seconds=offset)
            full = ml_gen.generate_metric(
                timestamp, pair[0], 75, "test", "curve", random.Random(1)
            )
            incremental = ml_gen.generate_metric(
                timestamp, pair[1], 75, "test", "stdev", random.Random(999)
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

            repeated = ml_gen.generate_metric(
                timestamp, pair[0], 0, "test", "curve", random.Random(123)
            )
            self.assertEqual(full["dcount_hosts"], repeated["dcount_hosts"])
            self.assertEqual(full["events_count"], repeated["events_count"])

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
        config["qualification_new_regime_minutes"] = 30
        with self.assertRaisesRegex(
            ValueError, "must be greater than QUALIFICATION_WINDOW_MINUTES"
        ):
            ml_gen.validate_config(config)


if __name__ == "__main__":
    unittest.main()
