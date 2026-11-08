"""Invalid configuration must fail before creating local or external resources."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from festive_offers.online.runtime import build_runtime, configured_tokens
from festive_offers.online.runtime_settings import RuntimeSettings

ROOT = Path(__file__).resolve().parents[1]


class RuntimeSettingsTests(unittest.TestCase):
    def test_invalid_selections_fail_before_creating_any_database(self) -> None:
        cases = [
            {"FESTIVE_STORE": "unknown"},
            {"FESTIVE_PROVIDER": "unknown"},
            {"FESTIVE_SYNTHETIC_NOW": "2026-11-05T12:00:00"},
            {"FESTIVE_STORE": "postgres", "FESTIVE_POSTGRES_SCHEMA": "public"},
            {"FESTIVE_STORE": "postgres", "FESTIVE_POSTGRES_INITIALIZE": "sometimes"},
            {"FESTIVE_STORE": "lakebase"},
            {"FESTIVE_PROVIDER": "http", "FESTIVE_SYNTHETIC_NOW": "2026-11-05T12:00:00Z"},
        ]
        for updates in cases:
            with self.subTest(updates=updates), tempfile.TemporaryDirectory() as temporary:
                destination = Path(temporary) / "not_created"
                env = {
                    "FESTIVE_SQLITE_PATH": str(destination / "application.sqlite"),
                    "FESTIVE_PROVIDER_DB": str(destination / "provider.sqlite"),
                    **updates,
                }
                with self.assertRaises(ValueError):
                    build_runtime(env)
                self.assertFalse(destination.exists())

    def test_http_settings_reject_bad_origins_timeout_and_event_key_without_requests(self) -> None:
        valid = {
            "FESTIVE_PROVIDER": "http",
            "FESTIVE_PROVIDER_URL": "https://provider.example",
            "FESTIVE_PROVIDER_TOKEN": "public-test-transport-token",
            "FESTIVE_EVENT_KEY": "x" * 32,
        }
        for updates in (
            {"FESTIVE_PROVIDER_TIMEOUT": "nan"},
            {"FESTIVE_PROVIDER_TIMEOUT": "0"},
            {"FESTIVE_PROVIDER_URL": "http://provider.example"},
            {"FESTIVE_EVENT_KEY": "short"},
        ):
            with self.subTest(updates=updates), self.assertRaises(ValueError):
                RuntimeSettings.from_environment({**valid, **updates})

    def test_settings_representations_do_not_reveal_configured_secrets(self) -> None:
        token = "sample-test-token-keep-out-of-repr"
        event_key = "sample-event-key-keep-out-of-repr-000"
        dsn = "dbname=example password=sample-test-password"
        settings = RuntimeSettings.from_environment(
            {
                "FESTIVE_PROVIDER": "http",
                "FESTIVE_PROVIDER_URL": "https://provider.example",
                "FESTIVE_PROVIDER_TOKEN": token,
                "FESTIVE_EVENT_KEY": event_key,
                "FESTIVE_STORE": "postgres",
                "FESTIVE_POSTGRES_DSN": dsn,
            }
        )
        for secret in (token, event_key, dsn):
            self.assertNotIn(secret, repr(settings))

    def test_configured_identities_reject_duplicates_unknown_roles_and_missing_identity(
        self,
    ) -> None:
        token = "public-reference-customer-token-0001"
        for encoded in (
            f'{{"{token}":{{"role":"customer","user_id":"one"}},"{token}":{{"role":"operator"}}}}',
            json.dumps({token: {"role": "administrator"}}),
            json.dumps({token: {"role": ["customer"]}}),
            json.dumps({token: {"role": "customer"}}),
            json.dumps({token: {"role": "customer", "user_id": 123}}),
        ):
            with self.subTest(encoded=encoded), self.assertRaises(ValueError):
                configured_tokens({"FESTIVE_AUTH_TOKENS_JSON": encoded})

    def test_invalid_cli_options_do_not_initialize_runtime_resources(self) -> None:
        cases = [
            ("worker", "--interval", "nan"),
            ("worker", "--batch-size", "0"),
            ("serve", "--port", "70000"),
        ]
        for args in cases:
            with self.subTest(args=args), tempfile.TemporaryDirectory() as temporary:
                destination = Path(temporary) / "not_created"
                env = {
                    **os.environ,
                    "PYTHONPATH": str(ROOT / "src"),
                    "FESTIVE_STORE": "sqlite",
                    "FESTIVE_PROVIDER": "fake",
                    "FESTIVE_SQLITE_PATH": str(destination / "application.sqlite"),
                    "FESTIVE_PROVIDER_DB": str(destination / "provider.sqlite"),
                }
                result = subprocess.run(
                    [sys.executable, "-m", "festive_offers.online.runtime", *args],
                    env=env,
                    capture_output=True,
                    text=True,
                    timeout=10,
                )
                self.assertEqual(result.returncode, 2)
                self.assertFalse(destination.exists())
