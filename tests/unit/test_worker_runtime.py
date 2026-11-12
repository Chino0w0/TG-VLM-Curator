from __future__ import annotations

import asyncio
import unittest
from datetime import UTC, datetime
from uuid import uuid4

from apps.worker.runtime import WorkerRuntime
from tgcurator.application.ports.processing import ClaimedRangeExecution
from tgcurator.infrastructure.observability import OperationsMonitor
from tgcurator.shared import DomainValidationError

NOW = datetime(2026, 9, 4, 12, 0, tzinfo=UTC)


class FakeDatabase:
    def __init__(self) -> None:
        self.disposed = False

    async def dispose(self) -> None:
        self.disposed = True


class FakeRangeExecutionWorker:
    def __init__(self, claim: ClaimedRangeExecution | None) -> None:
        self.claim_result = claim
        self.requests: list[tuple[str, datetime]] = []

    async def claim(self, *, execution_id: str, now: datetime) -> ClaimedRangeExecution | None:
        self.requests.append((execution_id, now))
        return self.claim_result


class FakeRangeExecutionHistoryIngestion:
    def __init__(self, *, result: bool = True) -> None:
        self._result = result
        self.requests: list[tuple[ClaimedRangeExecution, datetime]] = []

    async def process(self, *, claim: ClaimedRangeExecution, now: datetime) -> bool:
        self.requests.append((claim, now))
        return self._result


class FakeImageArchiveWorker:
    def __init__(self, *, result: bool = True) -> None:
        self._result = result
        self.requests: list[tuple[str, datetime]] = []

    async def process(self, *, image_asset_id: str, now: datetime) -> bool:
        self.requests.append((image_asset_id, now))
        return self._result


class FakeVideoArchiveWorker:
    def __init__(self, *, result: bool = True) -> None:
        self._result = result
        self.requests: list[tuple[str, datetime]] = []

    async def process(self, *, video_asset_id: str, now: datetime) -> bool:
        self.requests.append((video_asset_id, now))
        return self._result


class FakeAnalysisStageRunWorker:
    def __init__(self, *, result: bool = True) -> None:
        self._result = result
        self.requests: list[tuple[str, datetime]] = []

    async def process(self, *, stage_run_id: str, now: datetime) -> bool:
        self.requests.append((stage_run_id, now))
        return self._result


class FakePublicationWorker:
    def __init__(self, *, result: bool = True) -> None:
        self._result = result
        self.requests: list[tuple[str, datetime]] = []

    async def process(self, *, intent_id: str, now: datetime) -> bool:
        self.requests.append((intent_id, now))
        return self._result


class FakeCleanupWorker:
    def __init__(self, *, result: bool = True) -> None:
        self._result = result
        self.requests: list[tuple[str, datetime]] = []

    async def process(self, *, job_id: str, now: datetime) -> bool:
        self.requests.append((job_id, now))
        return self._result


class WorkerRuntimeTests(unittest.TestCase):
    def test_claims_one_normalized_execution_uuid_without_configured_history_processing(
        self,
    ) -> None:
        database = FakeDatabase()
        worker = FakeRangeExecutionWorker(claim=_claim())
        runtime = WorkerRuntime(database=database, range_execution_worker=worker)
        execution_id = str(uuid4()).upper()

        claimed = asyncio.run(runtime.handle_range_execution(execution_id=execution_id, now=NOW))
        asyncio.run(runtime.close())

        self.assertTrue(claimed)
        self.assertEqual(worker.requests, [(execution_id.lower(), NOW)])
        self.assertTrue(database.disposed)

    def test_processes_the_claim_with_an_injected_history_orchestrator(self) -> None:
        claim = _claim()
        worker = FakeRangeExecutionWorker(claim=claim)
        ingestion = FakeRangeExecutionHistoryIngestion(result=True)
        runtime = WorkerRuntime(
            database=FakeDatabase(),
            range_execution_worker=worker,
            range_execution_history_ingestion=ingestion,
        )

        processed = asyncio.run(
            runtime.handle_range_execution(execution_id=claim.execution_id, now=NOW)
        )

        self.assertTrue(processed)
        self.assertEqual(worker.requests, [(claim.execution_id, NOW)])
        self.assertEqual(ingestion.requests, [(claim, NOW)])

    def test_does_not_process_when_the_execution_cannot_be_claimed(self) -> None:
        worker = FakeRangeExecutionWorker(claim=None)
        ingestion = FakeRangeExecutionHistoryIngestion()
        runtime = WorkerRuntime(
            database=FakeDatabase(),
            range_execution_worker=worker,
            range_execution_history_ingestion=ingestion,
        )
        execution_id = str(uuid4())

        processed = asyncio.run(runtime.handle_range_execution(execution_id=execution_id, now=NOW))

        self.assertFalse(processed)
        self.assertEqual(worker.requests, [(execution_id, NOW)])
        self.assertEqual(ingestion.requests, [])

    def test_rejects_non_uuid_payload_before_worker_claim(self) -> None:
        worker = FakeRangeExecutionWorker(claim=None)
        runtime = WorkerRuntime(database=FakeDatabase(), range_execution_worker=worker)

        with self.assertRaises(DomainValidationError):
            asyncio.run(runtime.handle_range_execution(execution_id="not-a-uuid", now=NOW))

        self.assertEqual(worker.requests, [])

    def test_processes_normalized_image_uuid_with_an_injected_archive_worker(self) -> None:
        image_worker = FakeImageArchiveWorker(result=True)
        runtime = WorkerRuntime(
            database=FakeDatabase(),
            range_execution_worker=FakeRangeExecutionWorker(claim=None),
            image_archive_worker=image_worker,  # type: ignore[arg-type]
        )
        image_asset_id = str(uuid4()).upper()

        processed = asyncio.run(
            runtime.handle_image_archive(image_asset_id=image_asset_id, now=NOW)
        )

        self.assertTrue(processed)
        self.assertEqual(image_worker.requests, [(image_asset_id.lower(), NOW)])

    def test_unconfigured_image_archive_runtime_never_fakes_completion(self) -> None:
        runtime = WorkerRuntime(
            database=FakeDatabase(), range_execution_worker=FakeRangeExecutionWorker(claim=None)
        )

        processed = asyncio.run(runtime.handle_image_archive(image_asset_id=str(uuid4()), now=NOW))

        self.assertFalse(processed)

    def test_rejects_non_uuid_image_archive_payload_before_worker_call(self) -> None:
        image_worker = FakeImageArchiveWorker()
        runtime = WorkerRuntime(
            database=FakeDatabase(),
            range_execution_worker=FakeRangeExecutionWorker(claim=None),
            image_archive_worker=image_worker,  # type: ignore[arg-type]
        )

        with self.assertRaises(DomainValidationError):
            asyncio.run(runtime.handle_image_archive(image_asset_id="not-a-uuid", now=NOW))

        self.assertEqual(image_worker.requests, [])

    def test_processes_normalized_video_uuid_with_an_injected_archive_worker(self) -> None:
        video_worker = FakeVideoArchiveWorker(result=True)
        runtime = WorkerRuntime(
            database=FakeDatabase(),
            range_execution_worker=FakeRangeExecutionWorker(claim=None),
            video_archive_worker=video_worker,  # type: ignore[arg-type]
        )
        video_asset_id = str(uuid4()).upper()

        processed = asyncio.run(
            runtime.handle_video_archive(video_asset_id=video_asset_id, now=NOW)
        )

        self.assertTrue(processed)
        self.assertEqual(video_worker.requests, [(video_asset_id.lower(), NOW)])

    def test_unconfigured_video_archive_runtime_never_fakes_completion(self) -> None:
        runtime = WorkerRuntime(
            database=FakeDatabase(), range_execution_worker=FakeRangeExecutionWorker(claim=None)
        )

        processed = asyncio.run(runtime.handle_video_archive(video_asset_id=str(uuid4()), now=NOW))

        self.assertFalse(processed)

    def test_rejects_non_uuid_video_archive_payload_before_worker_call(self) -> None:
        video_worker = FakeVideoArchiveWorker()
        runtime = WorkerRuntime(
            database=FakeDatabase(),
            range_execution_worker=FakeRangeExecutionWorker(claim=None),
            video_archive_worker=video_worker,  # type: ignore[arg-type]
        )

        with self.assertRaises(DomainValidationError):
            asyncio.run(runtime.handle_video_archive(video_asset_id="not-a-uuid", now=NOW))

        self.assertEqual(video_worker.requests, [])

    def test_processes_normalized_analysis_uuid_with_an_injected_worker(self) -> None:
        analysis_worker = FakeAnalysisStageRunWorker(result=True)
        runtime = WorkerRuntime(
            database=FakeDatabase(),
            range_execution_worker=FakeRangeExecutionWorker(claim=None),
            analysis_stage_run_worker=analysis_worker,  # type: ignore[arg-type]
        )
        stage_run_id = str(uuid4()).upper()

        processed = asyncio.run(
            runtime.handle_analysis_stage_run(stage_run_id=stage_run_id, now=NOW)
        )

        self.assertTrue(processed)
        self.assertEqual(analysis_worker.requests, [(stage_run_id.lower(), NOW)])

    def test_unconfigured_analysis_runtime_never_fakes_completion(self) -> None:
        runtime = WorkerRuntime(
            database=FakeDatabase(), range_execution_worker=FakeRangeExecutionWorker(claim=None)
        )

        processed = asyncio.run(
            runtime.handle_analysis_stage_run(stage_run_id=str(uuid4()), now=NOW)
        )

        self.assertFalse(processed)

    def test_rejects_non_uuid_analysis_payload_before_worker_call(self) -> None:
        analysis_worker = FakeAnalysisStageRunWorker()
        runtime = WorkerRuntime(
            database=FakeDatabase(),
            range_execution_worker=FakeRangeExecutionWorker(claim=None),
            analysis_stage_run_worker=analysis_worker,  # type: ignore[arg-type]
        )

        with self.assertRaises(DomainValidationError):
            asyncio.run(runtime.handle_analysis_stage_run(stage_run_id="not-a-uuid", now=NOW))

        self.assertEqual(analysis_worker.requests, [])

    def test_processes_normalized_publication_uuid_with_an_injected_worker(self) -> None:
        publication_worker = FakePublicationWorker(result=True)
        runtime = WorkerRuntime(
            database=FakeDatabase(),
            range_execution_worker=FakeRangeExecutionWorker(claim=None),
            publication_worker=publication_worker,  # type: ignore[arg-type]
        )
        intent_id = str(uuid4()).upper()

        processed = asyncio.run(runtime.handle_publication(intent_id=intent_id, now=NOW))

        self.assertTrue(processed)
        self.assertEqual(publication_worker.requests, [(intent_id.lower(), NOW)])

    def test_unconfigured_publication_runtime_never_fakes_completion(self) -> None:
        runtime = WorkerRuntime(
            database=FakeDatabase(), range_execution_worker=FakeRangeExecutionWorker(claim=None)
        )

        processed = asyncio.run(runtime.handle_publication(intent_id=str(uuid4()), now=NOW))

        self.assertFalse(processed)

    def test_rejects_non_uuid_publication_payload_before_worker_call(self) -> None:
        publication_worker = FakePublicationWorker()
        runtime = WorkerRuntime(
            database=FakeDatabase(),
            range_execution_worker=FakeRangeExecutionWorker(claim=None),
            publication_worker=publication_worker,  # type: ignore[arg-type]
        )

        with self.assertRaises(DomainValidationError):
            asyncio.run(runtime.handle_publication(intent_id="not-a-uuid", now=NOW))

        self.assertEqual(publication_worker.requests, [])

    def test_processes_normalized_cleanup_uuid_with_an_injected_worker(self) -> None:
        cleanup_worker = FakeCleanupWorker(result=True)
        runtime = WorkerRuntime(
            database=FakeDatabase(),
            range_execution_worker=FakeRangeExecutionWorker(claim=None),
            cleanup_worker=cleanup_worker,  # type: ignore[arg-type]
        )
        job_id = str(uuid4()).upper()

        processed = asyncio.run(runtime.handle_archive_cleanup(job_id=job_id, now=NOW))

        self.assertTrue(processed)
        self.assertEqual(cleanup_worker.requests, [(job_id.lower(), NOW)])

    def test_unconfigured_cleanup_runtime_never_fakes_completion(self) -> None:
        runtime = WorkerRuntime(
            database=FakeDatabase(), range_execution_worker=FakeRangeExecutionWorker(claim=None)
        )

        processed = asyncio.run(runtime.handle_archive_cleanup(job_id=str(uuid4()), now=NOW))

        self.assertFalse(processed)

    def test_rejects_non_uuid_cleanup_payload_before_worker_call(self) -> None:
        cleanup_worker = FakeCleanupWorker()
        runtime = WorkerRuntime(
            database=FakeDatabase(),
            range_execution_worker=FakeRangeExecutionWorker(claim=None),
            cleanup_worker=cleanup_worker,  # type: ignore[arg-type]
        )

        with self.assertRaises(DomainValidationError):
            asyncio.run(runtime.handle_archive_cleanup(job_id="not-a-uuid", now=NOW))

        self.assertEqual(cleanup_worker.requests, [])

    def test_records_worker_heartbeat_on_valid_task_and_marks_db_unready_on_failure(self) -> None:
        monitor = OperationsMonitor()
        publication_worker = FakePublicationWorker(result=True)
        runtime = WorkerRuntime(
            database=FakeDatabase(),
            range_execution_worker=FakeRangeExecutionWorker(claim=None),
            publication_worker=publication_worker,  # type: ignore[arg-type]
            telemetry=monitor,
        )

        asyncio.run(runtime.handle_publication(intent_id=str(uuid4()), now=NOW))
        healthy = monitor.worker_health(now=NOW)
        self.assertTrue(healthy.alive)
        self.assertTrue(healthy.ready)

        class FailingCleanupWorker:
            async def process(self, *, job_id: str, now: datetime) -> bool:
                raise RuntimeError("db lost")

        failing_runtime = WorkerRuntime(
            database=FakeDatabase(),
            range_execution_worker=FakeRangeExecutionWorker(claim=None),
            cleanup_worker=FailingCleanupWorker(),  # type: ignore[arg-type]
            telemetry=monitor,
        )
        later = datetime(2026, 9, 4, 12, 1, tzinfo=UTC)
        with self.assertRaisesRegex(RuntimeError, "db lost"):
            asyncio.run(failing_runtime.handle_archive_cleanup(job_id=str(uuid4()), now=later))
        degraded = monitor.worker_health(now=later)
        self.assertTrue(degraded.alive)
        self.assertFalse(degraded.ready)
        self.assertIn("database_unavailable", degraded.blockers)


def _claim() -> ClaimedRangeExecution:
    return ClaimedRangeExecution(
        execution_id=str(uuid4()),
        processing_range_id=str(uuid4()),
        source_channel_id=str(uuid4()),
        source_profile_version_id=str(uuid4()),
        from_message_id_exclusive=10,
        to_message_id_inclusive=20,
        watermark_message_id=10,
        attempt_count=1,
        max_attempts=5,
        lease_token=str(uuid4()),
    )


if __name__ == "__main__":
    unittest.main()
