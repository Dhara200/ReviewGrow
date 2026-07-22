import unittest

from app.services.limiter_service import (
    LimitStatus,
    LimiterService,
    hash_key,
    normalize_key,
    MySQLLimiterBackend,
)


class RecordingBackend:
    def __init__(self):
        self.calls = []

    def check_limit(self, scope, key_hash):
        self.calls.append(("check", scope, key_hash))
        return LimitStatus(False, 0, 0)

    def record_failure(self, scope, key_hash, **options):
        self.calls.append(("record", scope, key_hash, options))
        return LimitStatus(True, options["threshold"], options["block_seconds"])

    def reset(self, scope, key_hash):
        self.calls.append(("reset", scope, key_hash))
        return True

    def cleanup(self, **options):
        self.calls.append(("cleanup", options))
        return 3


class LimiterKeyTests(unittest.TestCase):
    def test_account_hash_is_stable_after_normalization(self):
        self.assertEqual(hash_key("account", " User@Example.COM "),
                         hash_key("account", "user@example.com"))
        self.assertEqual(32, len(hash_key("account", "user@example.com")))

    def test_ip_normalization_is_stable(self):
        self.assertEqual(normalize_key("ip", "2001:0db8::1"), "2001:db8::1")
        self.assertEqual(hash_key("ip", "2001:0db8::1"),
                         hash_key("ip", "2001:db8:0:0:0:0:0:1"))

    def test_composite_scope_is_unambiguous_and_stable(self):
        first = normalize_key("ip_account", ("127.0.0.1", " User@EXAMPLE.com "))
        second = normalize_key("ip_account", ["127.0.0.1", "user@example.com"])
        self.assertEqual(first, second)
        self.assertEqual(hash_key("ip_account", ("127.0.0.1", "a@b.com")),
                         hash_key("ip_account", ("127.0.0.1", "A@B.COM")))

    def test_all_supported_scopes_and_invalid_inputs(self):
        for scope, key in (("ip", "127.0.0.1"), ("account", "a@b.com"),
                           ("ip_account", ("127.0.0.1", "a@b.com"))):
            with self.subTest(scope=scope):
                self.assertEqual(32, len(hash_key(scope, key)))
        for scope, key in (("other", "x"), ("ip", "not-an-ip"),
                           ("account", " "), ("ip_account", ("only-one",))):
            with self.subTest(scope=scope, key=key):
                with self.assertRaises((ValueError, TypeError)):
                    hash_key(scope, key)


class LimiterFacadeTests(unittest.TestCase):
    def setUp(self):
        self.backend = RecordingBackend()
        self.service = LimiterService(self.backend)

    def test_public_api_hashes_keys_and_delegates(self):
        self.assertFalse(self.service.check_limit("account", "Person@Example.com").blocked)
        status = self.service.record_failure(
            "account", "person@example.com", threshold=5,
            window_seconds=60, block_seconds=120,
        )
        self.assertTrue(status.blocked)
        self.assertTrue(self.service.reset("account", "person@example.com"))
        self.assertEqual(3, self.service.cleanup(older_than_seconds=3600, limit=50))
        hashes = [call[2] for call in self.backend.calls[:3]]
        self.assertEqual(hashes[0], hashes[1])
        self.assertEqual(hashes[1], hashes[2])
        self.assertNotIn(b"person@example.com", hashes)

    def test_positive_integer_options_are_required(self):
        for name in ("threshold", "window_seconds", "block_seconds"):
            options = dict(threshold=1, window_seconds=1, block_seconds=1)
            options[name] = 0
            with self.subTest(name=name), self.assertRaises(ValueError):
                self.service.record_failure("account", "a@b.com", **options)
        with self.assertRaises(ValueError):
            self.service.cleanup(older_than_seconds=0)


class FailingCursor:
    def __init__(self):
        self.closed = False

    def execute(self, query, params):
        raise RuntimeError("simulated cursor failure with database details")

    def close(self):
        self.closed = True


class FailingConnection:
    def __init__(self):
        self.cursor_instance = FailingCursor()
        self.rolled_back = False
        self.closed = False

    def cursor(self, dictionary=False):
        return self.cursor_instance

    def rollback(self):
        self.rolled_back = True

    def close(self):
        self.closed = True


class MySQLBackendFaultTests(unittest.TestCase):
    def test_cursor_failure_rolls_back_and_closes_without_memory_fallback(self):
        connection = FailingConnection()
        service = LimiterService(MySQLLimiterBackend(lambda: connection))
        with self.assertRaisesRegex(RuntimeError, "simulated cursor failure"):
            service.check_limit("account", "user@example.com")
        self.assertTrue(connection.rolled_back)
        self.assertTrue(connection.cursor_instance.closed)
        self.assertTrue(connection.closed)

    def test_connection_failure_propagates_without_fallback(self):
        def unavailable():
            raise RuntimeError("connection unavailable")

        service = LimiterService(MySQLLimiterBackend(unavailable))
        with self.assertRaisesRegex(RuntimeError, "connection unavailable"):
            service.record_failure(
                "account", "user@example.com", threshold=5,
                window_seconds=60, block_seconds=120,
            )


if __name__ == "__main__":
    unittest.main()
