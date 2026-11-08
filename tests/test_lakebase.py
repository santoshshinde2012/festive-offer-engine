from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import Mock

from festive_offers.online.lakebase import make_connection_factory


class LakebaseCredentialTests(unittest.TestCase):
    def setUp(self) -> None:
        self.environment = {
            "ENDPOINT_NAME": "projects/demo/branches/main/endpoints/primary",
            "PGHOST": "example.database.databricks.com",
            "PGDATABASE": "databricks_postgres",
            "PGUSER": "demo-service-principal",
        }
        self.client = SimpleNamespace(postgres=Mock())
        self.connector = Mock()

    def factory(self):
        return make_connection_factory(
            environ=self.environment,
            workspace_client=self.client,
            connect=self.connector,
        )

    def test_each_new_connection_mints_a_fresh_credential(self) -> None:
        self.client.postgres.generate_database_credential.side_effect = [
            SimpleNamespace(token="first-test-token"),
            SimpleNamespace(token="second-test-token"),
        ]
        connections = [object(), object()]
        self.connector.side_effect = connections
        factory = self.factory()
        self.client.postgres.generate_database_credential.assert_not_called()
        self.assertIs(factory(), connections[0])
        self.assertIs(factory(), connections[1])
        self.assertEqual(
            [call.kwargs["password"] for call in self.connector.call_args_list],
            ["first-test-token", "second-test-token"],
        )
        self.assertEqual(self.client.postgres.generate_database_credential.call_count, 2)
        for call in self.client.postgres.generate_database_credential.call_args_list:
            self.assertEqual(call.kwargs, {"endpoint": self.environment["ENDPOINT_NAME"]})
        kwargs = self.connector.call_args.kwargs
        self.assertEqual(kwargs["sslmode"], "require")
        self.assertEqual(kwargs["connect_timeout"], 10)

    def test_credential_failure_never_opens_database_or_falls_back_to_password(self) -> None:
        self.environment["PGPASSWORD"] = "ignored-test-password"
        self.client.postgres.generate_database_credential.side_effect = RuntimeError("unavailable")
        with self.assertRaisesRegex(RuntimeError, "unavailable"):
            self.factory()()
        self.connector.assert_not_called()

    def test_missing_token_never_opens_database(self) -> None:
        self.client.postgres.generate_database_credential.return_value = SimpleNamespace(token=None)
        with self.assertRaisesRegex(RuntimeError, "did not return"):
            self.factory()()
        self.connector.assert_not_called()

    def test_insecure_sslmode_is_rejected_before_any_network_call(self) -> None:
        self.environment["PGSSLMODE"] = "disable"
        with self.assertRaisesRegex(ValueError, "require TLS"):
            self.factory()
        self.client.postgres.generate_database_credential.assert_not_called()

    def test_incomplete_or_provisioned_endpoint_is_rejected(self) -> None:
        for endpoint in ("", "provisioned-instance-name", "projects/demo/branches/main"):
            with self.subTest(endpoint=endpoint):
                self.environment["ENDPOINT_NAME"] = endpoint
                with self.assertRaises(ValueError):
                    self.factory()

    def test_connect_options_remain_separate_libpq_arguments(self) -> None:
        self.environment.update(
            {
                "PGDATABASE": "example db",
                "PGSSLMODE": "verify-full",
                "PGSSLROOTCERT": "/configured/root.crt",
                "PGPORT": "6543",
                "PGCONNECT_TIMEOUT": "20",
            }
        )
        self.client.postgres.generate_database_credential.return_value = SimpleNamespace(
            token="test-token"
        )
        self.factory()()
        kwargs = self.connector.call_args.kwargs
        self.assertEqual(kwargs["dbname"], "example db")
        self.assertEqual(kwargs["sslrootcert"], "/configured/root.crt")
        self.assertEqual(kwargs["port"], 6543)
        self.assertEqual(kwargs["connect_timeout"], 20)
        self.assertEqual(kwargs["sslmode"], "verify-full")

    def test_invalid_port_or_timeout_is_rejected(self) -> None:
        for name, value in (("PGPORT", "abc"), ("PGPORT", "65536"), ("PGCONNECT_TIMEOUT", "0")):
            with self.subTest(name=name, value=value):
                environment = dict(self.environment, **{name: value})
                with self.assertRaises(ValueError):
                    make_connection_factory(environ=environment)


if __name__ == "__main__":
    unittest.main()
