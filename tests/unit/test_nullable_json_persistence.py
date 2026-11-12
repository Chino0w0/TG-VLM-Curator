from __future__ import annotations

import unittest

from sqlalchemy.dialects.postgresql.asyncpg import dialect

from tgcurator.infrastructure.database.models import (
    InferenceCallRecord,
    PipelineStageNodeRecord,
    StageRunRecord,
)


class NullableJsonPersistenceTests(unittest.TestCase):
    def test_nullable_object_columns_bind_python_none_as_sql_null(self):
        for model, field in (
            (InferenceCallRecord, "raw_response"),
            (InferenceCallRecord, "token_usage"),
            (StageRunRecord, "parsed_result"),
            (PipelineStageNodeRecord, "run_if"),
        ):
            with self.subTest(model=model.__name__, field=field):
                column = model.__table__.c[field]
                self.assertTrue(column.nullable)
                self.assertTrue(column.type.none_as_null)
                # Exercise the asyncpg driver used by production; the generic
                # PostgreSQL dialect selects a different driver across SQLAlchemy releases.
                pg = dialect()
                process = column.type.dialect_impl(pg).bind_processor(pg)
                self.assertIsNotNone(process)
                self.assertIsNone(process(None))
                self.assertEqual(process({"valid": True}), '{"valid": true}')
