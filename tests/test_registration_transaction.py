import unittest
from unittest.mock import patch

import mysql.connector

from app.routes.auth import _create_registered_user


class TransactionCursor:
    def __init__(self, connection):
        self.connection = connection
        self.lastrowid = 31
        self.closed = False

    def execute(self, query, params):
        if "INSERT INTO users" in query:
            if self.connection.fail_user:
                raise self.connection.user_error or RuntimeError("user insert failed")
            self.connection.pending_user = params[1]
        elif "INSERT INTO subscriptions" in query:
            if self.connection.fail_subscription:
                raise RuntimeError("subscription insert failed")
            self.connection.pending_subscription = params[0]

    def close(self):
        self.closed = True


class TransactionConnection:
    def __init__(self, fail_user=False, fail_subscription=False, fail_commit=False, user_error=None):
        self.fail_user = fail_user
        self.fail_subscription = fail_subscription
        self.fail_commit = fail_commit
        self.user_error = user_error
        self.pending_user = None
        self.pending_subscription = None
        self.users = []
        self.subscriptions = []
        self.started = False
        self.committed = False
        self.rolled_back = False
        self.closed = False
        self.cursor_instance = TransactionCursor(self)

    def cursor(self):
        return self.cursor_instance

    def start_transaction(self):
        self.started = True

    def commit(self):
        if self.fail_commit:
            raise RuntimeError("commit failed")
        self.users.append(self.pending_user)
        self.subscriptions.append(self.pending_subscription)
        self.committed = True

    def rollback(self):
        self.pending_user = None
        self.pending_subscription = None
        self.rolled_back = True

    def close(self):
        self.closed = True


class CursorFailureConnection(TransactionConnection):
    def cursor(self):
        raise RuntimeError("cursor creation failed")


class RegistrationTransactionTests(unittest.TestCase):
    def execute(self, connection):
        with patch("app.routes.auth.get_connection", return_value=connection):
            return _create_registered_user("Owner", "owner@example.com", "hash")

    def test_success_commits_user_and_subscription_together(self):
        connection = TransactionConnection()
        self.assertEqual(31, self.execute(connection))
        self.assertTrue(connection.started)
        self.assertTrue(connection.committed)
        self.assertEqual(["owner@example.com"], connection.users)
        self.assertEqual([31], connection.subscriptions)
        self.assertTrue(connection.cursor_instance.closed)
        self.assertTrue(connection.closed)

    def test_subscription_failure_rolls_back_user(self):
        connection = TransactionConnection(fail_subscription=True)
        with self.assertRaises(RuntimeError):
            self.execute(connection)
        self.assertTrue(connection.rolled_back)
        self.assertEqual([], connection.users)
        self.assertEqual([], connection.subscriptions)

    def test_user_insert_failure_creates_neither_record(self):
        connection = TransactionConnection(fail_user=True)
        with self.assertRaises(RuntimeError):
            self.execute(connection)
        self.assertTrue(connection.rolled_back)
        self.assertEqual([], connection.users)
        self.assertEqual([], connection.subscriptions)

    def test_commit_failure_rolls_back_and_closes_resources(self):
        connection = TransactionConnection(fail_commit=True)
        with self.assertRaises(RuntimeError):
            self.execute(connection)
        self.assertTrue(connection.rolled_back)
        self.assertFalse(connection.committed)
        self.assertTrue(connection.cursor_instance.closed)
        self.assertTrue(connection.closed)

    def test_retry_after_failure_succeeds(self):
        failed = TransactionConnection(fail_subscription=True)
        with self.assertRaises(RuntimeError):
            self.execute(failed)
        retry = TransactionConnection()
        self.assertEqual(31, self.execute(retry))
        self.assertEqual(["owner@example.com"], retry.users)
        self.assertEqual([31], retry.subscriptions)

    def test_duplicate_user_failure_is_deterministic(self):
        duplicate_error = mysql.connector.IntegrityError(
            msg="Duplicate entry", errno=1062
        )
        duplicate = TransactionConnection(fail_user=True, user_error=duplicate_error)
        with self.assertRaises(mysql.connector.IntegrityError) as raised:
            self.execute(duplicate)
        self.assertEqual(1062, raised.exception.errno)
        self.assertTrue(duplicate.rolled_back)
        self.assertEqual([], duplicate.subscriptions)

    def test_cursor_creation_failure_rolls_back_and_closes_connection(self):
        connection = CursorFailureConnection()
        with self.assertRaises(RuntimeError):
            self.execute(connection)
        self.assertTrue(connection.rolled_back)
        self.assertTrue(connection.closed)

    def test_connection_failure_propagates_without_creating_records(self):
        with patch(
            "app.routes.auth.get_connection",
            side_effect=mysql.connector.InterfaceError("connection failed"),
        ):
            with self.assertRaises(mysql.connector.InterfaceError):
                _create_registered_user("Owner", "owner@example.com", "hash")


if __name__ == "__main__":
    unittest.main()
