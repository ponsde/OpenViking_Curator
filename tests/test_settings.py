"""Tests for curator.settings — Pydantic Settings v2."""

import os

import pytest


class TestCuratorSettingsDefaults:
    """Default values match the legacy config.py behavior."""

    def test_default_thresholds(self):
        from curator.settings import CuratorSettings

        s = CuratorSettings()
        assert s.threshold_cov_sufficient == 0.55
        assert s.threshold_l0_sufficient == 0.62
        assert s.threshold_l1_sufficient == 0.5
        assert s.feedback_weight == 0.10
        assert s.max_l2_depth == 2

    def test_default_circuit_breaker(self):
        from curator.settings import CuratorSettings

        s = CuratorSettings()
        assert s.cb_enabled == "1"
        assert s.cb_threshold == 3
        assert s.cb_recovery_sec == 30.0

    def test_default_cache(self):
        from curator.settings import CuratorSettings

        s = CuratorSettings()
        assert s.cache_enabled == "0"
        assert s.cache_ttl == 3600
        assert s.cache_max_entries == 200


class TestCuratorSettingsEnvOverride:
    """Env vars override defaults."""

    def test_env_overrides_threshold(self, monkeypatch):
        monkeypatch.setenv("CURATOR_THRESHOLD_COV_SUFFICIENT", "0.80")
        from curator.settings import CuratorSettings

        s = CuratorSettings()
        assert s.threshold_cov_sufficient == 0.80

    def test_env_overrides_oai_base(self, monkeypatch):
        monkeypatch.setenv("CURATOR_OAI_BASE", "http://test:8080/v1")
        from curator.settings import CuratorSettings

        s = CuratorSettings()
        assert s.oai_base == "http://test:8080/v1"

    def test_openviking_config_alias(self, monkeypatch):
        """OPENVIKING_CONFIG_FILE has no CURATOR_ prefix — uses alias."""
        monkeypatch.setenv("OPENVIKING_CONFIG_FILE", "/tmp/test.conf")
        from curator.settings import CuratorSettings

        s = CuratorSettings()
        assert s.openviking_config_file == "/tmp/test.conf"


class TestCuratorSettingsValidation:
    """Type validation and constraints."""

    def test_threshold_rejects_out_of_range(self):
        from curator.settings import CuratorSettings

        with pytest.raises(ValueError):
            CuratorSettings(threshold_cov_sufficient=1.5)

    def test_threshold_rejects_negative(self):
        from curator.settings import CuratorSettings

        with pytest.raises(ValueError):
            CuratorSettings(feedback_weight=-0.1)

    def test_cb_threshold_rejects_zero(self):
        from curator.settings import CuratorSettings

        with pytest.raises(ValueError):
            CuratorSettings(cb_threshold=0)

    def test_provider_timeout_clamped(self):
        """Provider timeout is auto-clamped when >= search_timeout."""
        from curator.settings import CuratorSettings

        s = CuratorSettings(search_timeout=30.0, search_provider_timeout=35.0)
        assert s.search_provider_timeout < 30.0
        assert s.search_provider_timeout == pytest.approx(24.0)  # 30 * 0.8

    def test_provider_timeout_valid_passthrough(self):
        """Provider timeout passes through when < search_timeout."""
        from curator.settings import CuratorSettings

        s = CuratorSettings(search_timeout=60.0, search_provider_timeout=45.0)
        assert s.search_provider_timeout == 45.0


class TestConfigBackwardCompat:
    """Module-level aliases in config.py stay intact."""

    def test_config_module_attrs_exist(self):
        """All critical config attributes exist as module-level names."""
        from curator import config

        attrs = [
            "OAI_BASE",
            "OAI_KEY",
            "JUDGE_MODEL",
            "JUDGE_MODELS",
            "SEARCH_PROVIDERS",
            "SEARCH_TIMEOUT",
            "SEARCH_PROVIDER_TIMEOUT",
            "THRESHOLD_COV_SUFFICIENT",
            "THRESHOLD_L0_SUFFICIENT",
            "FEEDBACK_WEIGHT",
            "MAX_L2_DEPTH",
            "CB_ENABLED",
            "CB_THRESHOLD",
            "CB_RECOVERY_SEC",
            "CACHE_ENABLED",
            "CACHE_TTL",
            "CHAT_RETRY_MAX",
            "CHAT_RETRY_BACKOFF_SEC",
            "DATA_PATH",
            "OPENVIKING_CONFIG_FILE",
            "chat",
            "validate_config",
            "env",
            "log",
        ]
        for attr in attrs:
            assert hasattr(config, attr), f"config.{attr} missing"

    def test_monkeypatch_still_works(self, monkeypatch):
        """Existing test pattern: monkeypatch module attr on config."""
        monkeypatch.setattr("curator.config.OAI_BASE", "http://patched")
        from curator.config import OAI_BASE

        assert OAI_BASE == "http://patched"
