from contextlib import contextmanager
from pathlib import Path
from tempfile import TemporaryDirectory
from uuid import uuid4

import pytest
from unittest.mock import patch

from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import SimpleTestCase, TestCase

import psycopg
from parameterized import parameterized

from posthog.management.commands.apply_persons_migrations import (
    TRACKING_TABLE,
    _concurrent_index_target,
    _holds_multiple_statements,
    _runs_outside_transaction,
)
from posthog.persons_db import persons_db_connection


def _persons_fetchall(query: str, params: list | None = None) -> list[tuple]:
    with (
        persons_db_connection(writer=True, autocommit=True) as conn,
        conn.cursor() as cursor,
    ):
        cursor.execute(query, params)
        return cursor.fetchall()


def _persons_execute(statements: list[str]) -> None:
    with (
        persons_db_connection(writer=True, autocommit=True) as conn,
        conn.cursor() as cursor,
    ):
        for statement in statements:
            cursor.execute(statement)


class TestNoTransactionMarker(SimpleTestCase):
    @parameterized.expand(
        [
            (
                "first_line",
                "-- no-transaction\nCREATE INDEX CONCURRENTLY a ON t (c);",
                True,
            ),
            (
                "first_line_with_suffix",
                "-- no-transaction (concurrent)\nCREATE INDEX CONCURRENTLY a ON t (c);",
                True,
            ),
            (
                "after_other_comments",
                "-- why\n\n-- no-transaction\nCREATE INDEX CONCURRENTLY a ON t (c);",
                False,
            ),
            (
                "after_a_blank_line",
                "\n-- no-transaction\nCREATE INDEX CONCURRENTLY a ON t (c);",
                False,
            ),
            ("absent", "-- why\nCREATE INDEX a ON t (c);", False),
            (
                "only_after_a_statement",
                "CREATE INDEX a ON t (c);\n-- no-transaction\n",
                False,
            ),
        ]
    )
    def test_marker_detection(self, _name: str, sql_content: str, expected: bool) -> None:
        assert _runs_outside_transaction(sql_content) is expected


class TestMultipleStatementDetection(SimpleTestCase):
    @parameterized.expand(
        [
            ("single_statement", "CREATE INDEX CONCURRENTLY i ON t (c);", False),
            (
                "semicolon_in_a_string_literal",
                "CREATE INDEX CONCURRENTLY i ON t (c) WHERE v = ';';",
                False,
            ),
            (
                "trailing_comment",
                "CREATE INDEX CONCURRENTLY i ON t (c);\n-- recovery note\n",
                False,
            ),
            (
                "comment_marker_in_a_string_literal",
                "CREATE INDEX CONCURRENTLY i ON t (c) WHERE v = '--x'; CREATE INDEX j ON t (d);",
                True,
            ),
            (
                "two_statements",
                "CREATE TABLE t (id INT);\nCREATE INDEX CONCURRENTLY i ON t (id);",
                True,
            ),
        ]
    )
    def test_statement_counting(self, _name: str, sql_content: str, expected: bool) -> None:
        assert _holds_multiple_statements(sql_content) is expected


class TestConcurrentIndexTarget(SimpleTestCase):
    @parameterized.expand(
        [
            ("create", "CREATE INDEX CONCURRENTLY IF NOT EXISTS i ON t (c);", "i"),
            (
                "create_below_a_comment_block",
                "-- no-transaction\n-- why\nCREATE INDEX CONCURRENTLY i ON t (c);",
                "i",
            ),
            (
                "create_unique_over_two_lines",
                "CREATE UNIQUE INDEX CONCURRENTLY Mixed\n    ON t (c);",
                "mixed",
            ),
            (
                "create_with_a_quoted_name",
                'CREATE INDEX CONCURRENTLY "Mixed" ON t (c);',
                "Mixed",
            ),
            (
                "create_with_a_doubled_quote_in_the_quoted_name",
                'CREATE UNIQUE INDEX CONCURRENTLY "Mixed""unique" ON t (c);',
                'Mixed"unique',
            ),
            ("create_without_a_name", "CREATE INDEX CONCURRENTLY ON t (c);", None),
            ("drop", "DROP INDEX CONCURRENTLY IF EXISTS i;", None),
        ]
    )
    def test_target_parsing(self, _name: str, sql_content: str, expected: str | None) -> None:
        assert _concurrent_index_target(sql_content) == expected


class TestApplyPersonsMigrations(TestCase):
    def setUp(self) -> None:
        super().setUp()
        self.suffix = uuid4().hex[:8]
        self.table = f"no_tx_test_{self.suffix}"
        self.index = f"no_tx_test_{self.suffix}_idx"
        self.other_table = f"no_tx_test_{self.suffix}_other"
        self.other_index = f"no_tx_test_{self.suffix}_other_idx"
        self._migrations_tmp = TemporaryDirectory()
        self.migrations_dir = Path(self._migrations_tmp.name)

    def tearDown(self) -> None:
        _persons_execute(
            [
                f"DROP INDEX CONCURRENTLY IF EXISTS {self.index}",
                f"DROP INDEX CONCURRENTLY IF EXISTS {self.other_index}",
                f"DROP TABLE IF EXISTS {self.table}",
                f"DROP TABLE IF EXISTS {self.other_table}",
                f"DELETE FROM {TRACKING_TABLE} WHERE filename LIKE '%{self.suffix}%'",
            ]
        )
        self._migrations_tmp.cleanup()
        super().tearDown()

    def _write_migration(self, version: str, name: str, body: str) -> None:
        (self.migrations_dir / f"{version}_{self.suffix}_{name}.sql").write_text(body)

    def _apply(self) -> None:
        call_command("apply_persons_migrations", "--migrations-dir", str(self.migrations_dir))

    def _leave_an_invalid_index(self, table: str, index: str) -> None:
        _persons_execute(
            [
                f"CREATE TABLE {table} (id INT)",
                f"INSERT INTO {table} (id) VALUES (1), (1)",
            ]
        )
        with pytest.raises(psycopg.Error):
            _persons_execute([f"CREATE UNIQUE INDEX CONCURRENTLY {index} ON {table} (id)"])

    def _recorded_migrations(self) -> list[str]:
        rows = _persons_fetchall(
            f"SELECT filename FROM {TRACKING_TABLE} WHERE filename LIKE %s ORDER BY filename",
            [f"%{self.suffix}%"],
        )
        return [row[0] for row in rows]

    def test_applies_a_concurrent_index_build_marked_no_transaction(self) -> None:
        self._write_migration("20990101000001", "create_table", f"CREATE TABLE {self.table} (id INT);")
        self._write_migration(
            "20990101000002",
            "add_index",
            f"-- no-transaction\nCREATE INDEX CONCURRENTLY IF NOT EXISTS {self.index} ON {self.table} (id);",
        )

        self._apply()

        assert _persons_fetchall(
            "SELECT indisvalid FROM pg_index WHERE indexrelid = %s::regclass",
            [self.index],
        ) == [(True,)]
        assert len(self._recorded_migrations()) == 2

    def test_rejects_a_no_transaction_file_holding_more_than_one_statement(
        self,
    ) -> None:
        self._write_migration(
            "20990101000001",
            "two_statements",
            f"-- no-transaction\nCREATE TABLE {self.table} (id INT);\n"
            f"CREATE INDEX CONCURRENTLY {self.index} ON {self.table} (id);",
        )

        with pytest.raises(CommandError, match="more than one statement"):
            self._apply()

        assert self._recorded_migrations() == []

    def test_applies_a_marked_migration_while_an_unrelated_index_is_invalid(
        self,
    ) -> None:
        self._leave_an_invalid_index(self.other_table, self.other_index)
        self._write_migration("20990101000001", "create_table", f"CREATE TABLE {self.table} (id INT);")
        self._write_migration(
            "20990101000002",
            "add_index",
            f"-- no-transaction\nCREATE INDEX CONCURRENTLY IF NOT EXISTS {self.index} ON {self.table} (id);",
        )

        self._apply()

        assert _persons_fetchall(
            "SELECT indisvalid FROM pg_index WHERE indexrelid = %s::regclass",
            [self.index],
        ) == [(True,)]
        assert len(self._recorded_migrations()) == 2

    def test_applies_a_concurrent_drop_of_an_invalid_index(self) -> None:
        self._leave_an_invalid_index(self.table, self.index)
        self._write_migration(
            "20990101000001",
            "drop_index",
            f"-- no-transaction\nDROP INDEX CONCURRENTLY IF EXISTS {self.index};",
        )

        self._apply()

        assert _persons_fetchall("SELECT to_regclass(%s) IS NULL", [self.index]) == [(True,)]
        assert self._recorded_migrations() == [f"20990101000001_{self.suffix}_drop_index.sql"]

    def test_refuses_to_continue_when_a_failed_build_left_an_invalid_index(
        self,
    ) -> None:
        self._write_migration(
            "20990101000001",
            "create_table",
            f"CREATE TABLE {self.table} (id INT); INSERT INTO {self.table} (id) VALUES (1), (1);",
        )
        self._write_migration(
            "20990101000002",
            "add_unique_index",
            f"-- no-transaction\nCREATE UNIQUE INDEX CONCURRENTLY IF NOT EXISTS {self.index} ON {self.table} (id);",
        )

        with pytest.raises(psycopg.Error):
            self._apply()

        with pytest.raises(CommandError, match=f"DROP INDEX CONCURRENTLY public.{self.index}") as error:
            self._apply()

        assert "INVALID" in str(error.value)
        assert len(self._recorded_migrations()) == 1


PERSON_UPSERT_MIGRATION = (
    Path(__file__).resolve().parents[4]
    / "rust/persons_migrations/20261025000001_ensure_person_team_uuid_upsert_index.sql"
)


class TestPersonUpsertArbiterMigration(TestCase):
    """Run the genuine migration through the canonical command in an isolated persons schema."""

    def setUp(self) -> None:
        super().setUp()
        self.schema = f"person_arbiter_test_{uuid4().hex[:12]}"
        self._migrations_tmp = TemporaryDirectory()
        self.migrations_dir = Path(self._migrations_tmp.name)
        self.public_history = self._public_tracking_history()
        self.filename = PERSON_UPSERT_MIGRATION.name
        (self.migrations_dir / self.filename).write_text(PERSON_UPSERT_MIGRATION.read_text())
        _persons_execute([f"CREATE SCHEMA {self.schema}"])
        # The runner's sqlx bridge probes all schemas before reading an unqualified table.
        # Supply an empty private ledger, never synthetic applied-version records.
        self._execute("CREATE TABLE _sqlx_migrations (version BIGINT PRIMARY KEY, success BOOLEAN NOT NULL)")

    def tearDown(self) -> None:
        try:
            assert self._public_tracking_history() == self.public_history
        finally:
            _persons_execute([f"DROP SCHEMA {self.schema} CASCADE"])
            self._migrations_tmp.cleanup()
            super().tearDown()

    def _public_tracking_history(self) -> list[tuple] | None:
        if not _persons_fetchall("SELECT to_regclass(%s) IS NOT NULL", [f"public.{TRACKING_TABLE}"])[0][0]:
            return None
        return _persons_fetchall(f"SELECT filename, applied_at FROM public.{TRACKING_TABLE} ORDER BY filename")

    @contextmanager
    def _connection(self, **kwargs):
        with persons_db_connection(**kwargs) as conn:
            conn.execute(f"SET search_path TO {self.schema}")
            yield conn

    def _execute(self, *statements: str) -> None:
        with self._connection(writer=True, autocommit=True) as conn:
            for statement in statements:
                conn.execute(statement)

    def _query(self, statement: str, params=None) -> list[tuple]:
        with (
            self._connection(writer=True, autocommit=True) as conn,
            conn.cursor() as cursor,
        ):
            cursor.execute(statement, params)
            return cursor.fetchall()

    def _create_person_table(self, *, partitioned: bool = False) -> None:
        suffix = " PARTITION BY HASH (team_id)" if partitioned else ""
        self._execute(
            "CREATE TABLE posthog_person (team_id INTEGER NOT NULL, uuid UUID NOT NULL, "
            f"version BIGINT NOT NULL DEFAULT 0){suffix}"
        )
        if partitioned:
            self._execute(
                "CREATE TABLE posthog_person_p0 PARTITION OF posthog_person FOR VALUES WITH (MODULUS 2, REMAINDER 0)",
                "CREATE TABLE posthog_person_p1 PARTITION OF posthog_person FOR VALUES WITH (MODULUS 2, REMAINDER 1)",
            )

    def _apply(self, *, hobby: bool = True) -> None:
        with patch(
            "posthog.management.commands.apply_persons_migrations.persons_db_connection",
            self._connection,
        ):
            call_command(
                "apply_persons_migrations",
                migrations_dir=str(self.migrations_dir),
                hobby=hobby,
            )
        assert self._query(
            "SELECT table_schema FROM information_schema.tables "
            "WHERE table_name = %s AND table_schema = current_schema()",
            [TRACKING_TABLE],
        ) == [(self.schema,)]

    def _indexes(self) -> list[tuple]:
        return self._query(
            "SELECT indexrelid, pg_get_indexdef(indexrelid) FROM pg_index "
            "WHERE indrelid = 'posthog_person'::regclass ORDER BY indexrelid"
        )

    def _assert_consumer_upsert(self) -> None:
        person_uuid = uuid4()
        statement = (
            "INSERT INTO posthog_person (team_id, uuid, version) VALUES (%s, %s, 1) "
            "ON CONFLICT (team_id, uuid) DO UPDATE SET version = posthog_person.version + 1 RETURNING version"
        )
        assert self._query(statement, [1, person_uuid]) == [(1,)]
        assert self._query(statement, [1, person_uuid]) == [(2,)]
        # UUID uniqueness is scoped to the team, not global.
        assert self._query(statement, [2, person_uuid]) == [(1,)]

    def test_hobby_creates_usable_arbiter_and_repeated_migration_is_idempotent(
        self,
    ) -> None:
        self._create_person_table()
        self._execute("CREATE INDEX old_uuid_only_index ON posthog_person (uuid)")

        self._apply()
        self._assert_consumer_upsert()
        indexes = self._indexes()
        assert len(indexes) == 2

        self._apply()
        # Also prove body-level idempotency if SQL applied successfully before tracking.
        self._execute(PERSON_UPSERT_MIGRATION.read_text())
        assert self._indexes() == indexes
        assert self._query(f"SELECT filename FROM {TRACKING_TABLE}") == [(self.filename,)]

    @parameterized.expand(
        [
            ("renamed", False, "(team_id, uuid)"),
            ("reversed", False, "(uuid, team_id)"),
            ("included", False, "(uuid, team_id) INCLUDE (version)"),
            ("partitioned", True, "(team_id, uuid)"),
            ("partitioned_reversed", True, "(uuid, team_id)"),
        ]
    )
    def test_equivalent_arbiter_is_a_noop(self, _name: str, partitioned: bool, definition: str) -> None:
        self._create_person_table(partitioned=partitioned)
        self._execute(f"CREATE UNIQUE INDEX arbitrary_existing_name ON posthog_person {definition}")
        indexes = self._indexes()

        self._apply(hobby=not partitioned)

        assert self._indexes() == indexes
        self._assert_consumer_upsert()

    @parameterized.expand(
        [
            (
                "nonunique",
                "CREATE INDEX inadequate_index ON posthog_person (team_id, uuid)",
            ),
            (
                "extra_key",
                "CREATE UNIQUE INDEX inadequate_index ON posthog_person (team_id, uuid, version)",
            ),
            (
                "partial",
                "CREATE UNIQUE INDEX inadequate_index ON posthog_person (team_id, uuid) WHERE version > 0",
            ),
            (
                "expression",
                "CREATE UNIQUE INDEX inadequate_index ON posthog_person (team_id, (uuid::text))",
            ),
        ]
    )
    def test_inadequate_index_does_not_suppress_arbiter_creation(self, _name: str, definition: str) -> None:
        self._create_person_table()
        self._execute(definition)

        self._apply()

        assert len(self._indexes()) == 2
        self._assert_consumer_upsert()

    def test_duplicates_abort_without_data_repair_or_tracking(self) -> None:
        self._create_person_table()
        self._execute(
            "INSERT INTO posthog_person (team_id, uuid) VALUES "
            "(1, '00000000-0000-0000-0000-000000000001'), (1, '00000000-0000-0000-0000-000000000001')"
        )

        with pytest.raises(psycopg.errors.UniqueViolation):
            self._apply()

        assert self._query("SELECT count(*) FROM posthog_person") == [(2,)]
        assert self._indexes() == []
        assert self._query(f"SELECT filename FROM {TRACKING_TABLE}") == []

    @parameterized.expand([(False,), (True,)])
    def test_deferrable_arbiter_is_rejected_without_schema_repair(self, immediate_also_exists: bool) -> None:
        self._create_person_table()
        self._execute(
            "ALTER TABLE posthog_person ADD CONSTRAINT deferred_person_key "
            "UNIQUE (team_id, uuid) DEFERRABLE INITIALLY IMMEDIATE"
        )
        if immediate_also_exists:
            self._execute("CREATE UNIQUE INDEX immediate_person_key ON posthog_person (uuid, team_id)")
        indexes = self._indexes()

        with pytest.raises(psycopg.Error, match="matching deferrable unique constraint"):
            self._apply()

        assert self._indexes() == indexes
        assert self._query(f"SELECT filename FROM {TRACKING_TABLE}") == []

    def test_conflicting_name_is_not_silently_accepted(self) -> None:
        self._create_person_table()
        self._execute("CREATE INDEX posthog_person_team_uuid_upsert_idx ON posthog_person (uuid)")
        indexes = self._indexes()

        with pytest.raises(psycopg.Error, match="already exists without an equivalent"):
            self._apply()

        assert self._indexes() == indexes
        assert self._query(f"SELECT filename FROM {TRACKING_TABLE}") == []

    def test_invalid_named_index_is_preserved_and_rejected(self) -> None:
        self._create_person_table()
        self._execute(
            "INSERT INTO posthog_person (team_id, uuid) VALUES "
            "(1, '00000000-0000-0000-0000-000000000001'), (1, '00000000-0000-0000-0000-000000000001')"
        )
        with pytest.raises(psycopg.errors.UniqueViolation):
            self._execute(
                "CREATE UNIQUE INDEX CONCURRENTLY posthog_person_team_uuid_upsert_idx ON posthog_person (team_id, uuid)"
            )
        indexes = self._indexes()

        with pytest.raises(psycopg.Error, match="already exists without an equivalent"):
            self._apply()

        assert self._indexes() == indexes
        assert self._query(
            "SELECT indisvalid FROM pg_index WHERE indexrelid = 'posthog_person_team_uuid_upsert_idx'::regclass"
        ) == [(False,)]
        assert self._query("SELECT count(*) FROM posthog_person") == [(2,)]
        assert self._query(f"SELECT filename FROM {TRACKING_TABLE}") == []
