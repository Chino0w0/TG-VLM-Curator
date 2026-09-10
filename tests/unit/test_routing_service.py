from __future__ import annotations

import unittest
from uuid import UUID

from tgcurator.application.ports.routing import (
    RoutingExecutionResult,
    RoutingRequest,
    RoutingSnapshot,
)
from tgcurator.application.routing import RoutingService
from tgcurator.domain.review import LabelValue, ReviewStatus
from tgcurator.domain.routing import PublicationAction, RoutingPolicy, RoutingRule
from tgcurator.shared import DomainValidationError

MESSAGE_ID = "11111111-1111-4111-8111-111111111111"
POLICY_ID = "22222222-2222-4222-8222-222222222222"
RULE_ID = "33333333-3333-4333-8333-333333333333"
ACTION_A_ID = "44444444-4444-4444-8444-444444444441"
ACTION_B_ID = "44444444-4444-4444-8444-444444444442"
DESTINATION_A_ID = "55555555-5555-4555-8555-555555555551"
DESTINATION_B_ID = "55555555-5555-4555-8555-555555555552"
DEFAULT_IDENTITY_ID = "66666666-6666-4666-8666-666666666661"
EXPLICIT_IDENTITY_ID = "66666666-6666-4666-8666-666666666662"
LABEL_VERSION_ID = "77777777-7777-4777-8777-777777777777"
TEMPLATE_VERSION_ID = "78777777-7777-4777-8777-777777777777"


class FakeRoutingRepository:
    def __init__(self, snapshot: RoutingSnapshot) -> None:
        self.snapshot = snapshot
        self.executions: dict[str, RoutingExecutionResult] = {}
        self.find_calls: list[str] = []
        self.load_calls: list[tuple[str, str]] = []
        self.persist_calls: list[tuple[object, object, tuple[object, ...]]] = []

    async def find_by_request_id(self, *, routing_request_id: str):
        self.find_calls.append(routing_request_id)
        return self.executions.get(routing_request_id)

    async def load_snapshot(self, *, message_id: str, routing_policy_version_id: str):
        self.load_calls.append((message_id, routing_policy_version_id))
        return self.snapshot

    async def persist_evaluation(self, *, evaluation, decision, intents):  # type: ignore[no-untyped-def]
        self.persist_calls.append((evaluation, decision, intents))
        result = RoutingExecutionResult(
            evaluation_id=evaluation.evaluation_id,
            routing_request_id=evaluation.routing_request_id,
            message_id=evaluation.message_id,
            routing_policy_version_id=evaluation.routing_policy_version_id,
            facts_snapshot=evaluation.facts_snapshot,
            facts_hash=evaluation.facts_hash,
            decision=decision,
            intents=intents,
            persisted=True,
        )
        self.executions[evaluation.routing_request_id] = result
        return result


def snapshot(*, matches: bool = True) -> RoutingSnapshot:
    condition = {
        "label": {
            "namespace": "effective",
            "scope": "global",
            "key": "keep" if matches else "missing",
        }
    }
    policy = RoutingPolicy(
        policy_version_id=POLICY_ID,
        rules=(
            RoutingRule(
                rule_id=RULE_ID,
                priority=10,
                condition=condition,
                actions=(
                    PublicationAction(
                        action_id=ACTION_A_ID,
                        destination_channel_id=DESTINATION_A_ID,
                        publication_mode="metadata_only",
                        rendering_template_version_id=TEMPLATE_VERSION_ID,
                    ),
                    PublicationAction(
                        action_id=ACTION_B_ID,
                        destination_channel_id=DESTINATION_B_ID,
                        publication_mode="forward_only",
                        publish_identity_id=EXPLICIT_IDENTITY_ID,
                    ),
                ),
            ),
        ),
    )
    model = LabelValue(
        message_id=MESSAGE_ID,
        target_scope="global",
        target_kind="message",
        target_id=MESSAGE_ID,
        label_definition_version_id=LABEL_VERSION_ID,
        label_key="keep",
        score=0.93,
        activated=True,
    )
    return RoutingSnapshot(
        message_id=MESSAGE_ID,
        message={"id": MESSAGE_ID, "blocked_from_analysis": False},
        source_channel={"id": "source-1", "username": "source"},
        duplicate_state={"status": "unique"},
        media=(),
        review_status=ReviewStatus.UNREVIEWED,
        model_assignments=(model,),
        manual_events=(),
        policy=policy,
        destination_publish_identity_ids={
            DESTINATION_A_ID: DEFAULT_IDENTITY_ID,
            DESTINATION_B_ID: DEFAULT_IDENTITY_ID,
        },
    )


class RoutingServiceTests(unittest.IsolatedAsyncioTestCase):
    async def test_dry_run_routes_unreviewed_message_and_writes_nothing(self) -> None:
        repository = FakeRoutingRepository(snapshot())
        identifiers = iter(
            (
                UUID("88888888-8888-4888-8888-888888888881"),
                UUID("88888888-8888-4888-8888-888888888882"),
                UUID("88888888-8888-4888-8888-888888888883"),
            )
        )
        service = RoutingService(
            repository,  # type: ignore[arg-type]
            id_factory=lambda: next(identifiers),
        )

        result = await service.evaluate(
            RoutingRequest(
                request_id="dry-run-1",
                message_id=MESSAGE_ID,
                routing_policy_version_id=POLICY_ID,
                dry_run=True,
            )
        )

        self.assertTrue(result.dry_run)
        self.assertFalse(result.persisted)
        self.assertEqual(repository.find_calls, [])
        self.assertEqual(repository.persist_calls, [])
        self.assertEqual(len(result.intents), 2)
        self.assertEqual(result.intents[0].publish_identity_id, DEFAULT_IDENTITY_ID)
        self.assertEqual(
            result.intents[0].rendering_template_version_id,
            TEMPLATE_VERSION_ID,
        )
        self.assertEqual(result.intents[1].publish_identity_id, EXPLICIT_IDENTITY_ID)
        self.assertEqual(result.facts_snapshot["review_status"], "unreviewed")
        self.assertEqual(result.facts_snapshot["message"]["review_status"], "unreviewed")

    async def test_duplicate_request_reuses_history_and_explicit_reroute_is_distinct(self) -> None:
        repository = FakeRoutingRepository(snapshot())
        identifiers = iter(
            UUID(value)
            for value in (
                "99999999-9999-4999-8999-999999999991",
                "99999999-9999-4999-8999-999999999992",
                "99999999-9999-4999-8999-999999999993",
                "99999999-9999-4999-8999-999999999994",
                "99999999-9999-4999-8999-999999999995",
                "99999999-9999-4999-8999-999999999996",
            )
        )
        service = RoutingService(
            repository,  # type: ignore[arg-type]
            id_factory=lambda: next(identifiers),
        )

        first = await service.evaluate(RoutingRequest("route-1", MESSAGE_ID, POLICY_ID))
        duplicate = await service.evaluate(RoutingRequest("route-1", MESSAGE_ID, POLICY_ID))
        reroute = await service.evaluate(RoutingRequest("route-2", MESSAGE_ID, POLICY_ID))

        self.assertTrue(first.persisted)
        self.assertTrue(duplicate.reused)
        self.assertEqual(duplicate.evaluation_id, first.evaluation_id)
        self.assertEqual(len(repository.persist_calls), 2)
        self.assertNotEqual(reroute.evaluation_id, first.evaluation_id)
        self.assertEqual(set(repository.executions), {"route-1", "route-2"})
        self.assertTrue(
            set(intent.business_idempotency_key for intent in first.intents).isdisjoint(
                intent.business_idempotency_key for intent in reroute.intents
            )
        )

    async def test_duplicate_request_rejects_a_different_message_or_policy(self) -> None:
        repository = FakeRoutingRepository(snapshot())
        service = RoutingService(repository)  # type: ignore[arg-type]
        await service.evaluate(RoutingRequest("route-1", MESSAGE_ID, POLICY_ID))

        conflicting_requests = (
            RoutingRequest(
                "route-1",
                "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
                POLICY_ID,
            ),
            RoutingRequest(
                "route-1",
                MESSAGE_ID,
                "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb",
            ),
        )
        for request in conflicting_requests:
            with self.subTest(request=request):
                with self.assertRaisesRegex(DomainValidationError, "already bound"):
                    await service.evaluate(request)

        self.assertEqual(repository.load_calls, [(MESSAGE_ID, POLICY_ID)])
        self.assertEqual(len(repository.persist_calls), 1)

    async def test_formal_evaluation_persists_zero_intents_when_no_rule_matches(self) -> None:
        repository = FakeRoutingRepository(snapshot(matches=False))
        service = RoutingService(
            repository,  # type: ignore[arg-type]
            id_factory=lambda: UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"),
        )

        result = await service.evaluate(RoutingRequest("route-none", MESSAGE_ID, POLICY_ID))

        self.assertTrue(result.persisted)
        self.assertEqual(result.intents, ())
        self.assertEqual(len(repository.persist_calls), 1)
        self.assertEqual(repository.persist_calls[0][2], ())


if __name__ == "__main__":
    unittest.main()
