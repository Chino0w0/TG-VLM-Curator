"""Local-only browser fixture server: real M8 routes/services/auth, InMemory persistence.
No Telegram, inference provider, PostgreSQL, Redis, or cleanup worker is invoked.
Run with repository venv: python web/tests/local_api_server.py --evidence <directory>
"""

import argparse
import importlib.util
import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID

import uvicorn
from fastapi.staticfiles import StaticFiles

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
parser = argparse.ArgumentParser()
parser.add_argument("--evidence", type=Path, required=True)
parser.add_argument("--port", type=int, default=18731)
parser.add_argument("--export-fixtures", action="store_true")
args = parser.parse_args()
args.evidence.mkdir(parents=True, exist_ok=True)
spec = importlib.util.spec_from_file_location(
    "local_admin_fixture", ROOT / "tests/unit/test_admin_api.py"
)
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)
client, backend, _provider = m._build_test_stack(with_provider=False)
app = client.app
now = datetime(2026, 11, 12, 10, 0, tzinfo=UTC)


def dump(path, data):
    path.write_text(json.dumps(data, indent=2), encoding="utf-8", newline="\n")


with client:
    headers = m._login_and_csrf(client)

    def post(path, data):
        response = client.post("/api/v1" + path, headers=headers, json=data)
        assert response.status_code in (200, 201), response.text
        return response.json()

    secret = post(
        "/secrets", {"secret_type": "telegram_session", "plaintext": "LOCAL-TEST-NOT-A-CREDENTIAL"}
    )
    inference_secret = post(
        "/secrets", {"secret_type": "api_key", "plaintext": "LOCAL-TEST-NOT-A-CREDENTIAL"}
    )
    identity = post(
        "/telegram-identities",
        {"name": "Test reader", "identity_type": "mtproto_user", "secret_id": secret["secret_id"]},
    )
    source = post(
        "/source-channels",
        {
            "telegram_peer_id": -1001234001,
            "display_name": "Test Source",
            "default_read_identity_id": identity["id"],
        },
    )
    source2 = post(
        "/source-channels",
        {
            "telegram_peer_id": -1001234003,
            "display_name": "Other Source",
            "default_read_identity_id": identity["id"],
        },
    )
    destination = post(
        "/destination-channels",
        {
            "telegram_peer_id": -1001234002,
            "name": "Test Destination",
            "default_publish_identity_id": identity["id"],
        },
    )
    empty = post("/config/label-definitions", {"name": "Recover empty entity"})
    empty_template = post("/config/rendering-templates", {"name": "Recover empty template"})
    media_label = post(
        "/config/label-definitions",
        {
            "name": "Media label",
            "initial_version": {
                "key": "media_quality",
                "display_name": "Media Quality",
                "scope": "media",
                "negative": False,
            },
        },
    )
    mv = media_label["versions"][0]
    post(f"/config/label-definitions/{media_label['id']}/versions/{mv['id']}/publish", {})
    for i in range(1, 31):
        mid, aid, vid = (str(UUID(int=n)) for n in (i, 1000 + i, 2000 + i))
        backend.messages[mid] = {
            "id": mid,
            "source_channel_id": source["id"],
            "primary_telegram_message_id": i,
            "published_at": f"2026-11-12T01:00:{i:02d}+00:00",
            "original_text": f"Test message {i:02d}",
            "media_count": 2,
            "processing_status": "processed",
            "terminal_reason": "completed",
            "duplicate_status": "unique",
            "review_status": "unreviewed",
            "blocked_from_analysis": False,
        }
        backend.image_assets[aid] = {
            "id": aid,
            "message_id": mid,
            "archive_state": "ready",
            "archive_size_bytes": 2048,
            "storage_key": f"test/{aid}.webp",
            "archive_sha256": "a" * 64,
            "perceptual_hash": "1" * 16,
            "width": 1024,
            "height": 768,
            "created_at": "2026-11-01T00:00:00+00:00",
            "archived_at": "2026-11-01T00:00:00+00:00",
        }
        backend.video_assets[vid] = {
            "id": vid,
            "message_id": mid,
            "archive_state": "ready",
            "source_cover_phash": "2" * 16,
            "duration_seconds": 10,
            "created_at": "2026-11-01T00:00:00+00:00",
        }
    metadata = {
        "source_id": source["id"],
        "other_source_id": source2["id"],
        "destination_id": destination["id"],
        "identity_id": identity["id"],
        "inference_secret_id": inference_secret["secret_id"],
        "empty_entity_id": empty["id"],
        "empty_template_id": empty_template["id"],
        "media_label_version_id": mv["id"],
        "message_ids": list(backend.messages),
    }
    detail = client.get("/api/v1/messages/" + metadata["message_ids"][0]).json()
    # Real database serializer payload, not hand-authored frontend aliases.
    from tgcurator.infrastructure.database.admin_api_repository import (
        _serialize_analysis_run,
        _serialize_evaluation,
        _serialize_intent,
        _serialize_stage_run,
    )

    def uid(n):
        return UUID(int=n)

    stage = SimpleNamespace(
        id=uid(4001),
        analysis_stage_template_version_id=uid(4002),
        node_key="diagnostics",
        target_scope="global",
        target_kind="message",
        target_id=uid(1),
        status="completed",
        attempt_count=1,
        result_origin="provider",
        reused_from_stage_run_id=None,
        last_error_code=None,
        last_error_type=None,
    )
    manifest = SimpleNamespace(
        id=uid(4003),
        input_manifest_hash="a" * 64,
        content={"message_text": "Test message 01", "targets": [{"target_id": str(uid(1))}]},
    )
    call = SimpleNamespace(
        id=uid(4004),
        provider_adapter="openai_compatible",
        model_name="fixture-model",
        status="completed",
        http_status=200,
        structured_output_schema_hash="b" * 64,
        token_usage={"input_tokens": 123, "output_tokens": 7},
        raw_response={"fixture": "seeded-external-response"},
        latency_ms=25,
        error_code=None,
        error_type=None,
    )
    run = SimpleNamespace(
        id=uid(4005),
        analysis_pipeline_version_id=uid(4006),
        run_mode="formal",
        status="completed",
        created_at=now,
        started_at=now,
        completed_at=now,
    )
    evaluation = SimpleNamespace(
        id=uid(5001),
        routing_request_id="fixture-request",
        routing_policy_version_id=uid(5002),
        facts_hash="c" * 64,
        facts_snapshot={},
        rule_outcomes=[],
        stopped_at_rule_id=None,
        created_at=now,
    )
    intent = SimpleNamespace(
        id=uid(5003),
        routing_evaluation_id=uid(5001),
        destination_channel_id=UUID(destination["id"]),
        publish_identity_id=UUID(identity["id"]),
        publication_mode="forward_only",
        protected_content_fallback="fail",
        rendered_text=None,
        status="sent",
        attempt_count=1,
        last_error_code=None,
        next_retry_at=None,
    )
    record = SimpleNamespace(
        id=uid(5004),
        part="primary",
        operation="forward",
        remote_message_ids=[9001, 9002],
        sent_at=now,
    )
    detail["analysis_runs"] = [
        _serialize_analysis_run(run, [_serialize_stage_run(stage, manifest, call)])
    ]
    detail["routing_evaluations"] = [_serialize_evaluation(evaluation)]
    detail["publication_intents"] = [_serialize_intent(intent, [], [record])]
    dump(args.evidence / "fixture.json", metadata)
    dump(args.evidence / "serializer-detail.json", detail)
    if args.export_fixtures:
        fixtures = ROOT / "web/src/__tests__/fixtures"
        fixtures.mkdir(exist_ok=True)
        dump(fixtures / "message-detail.json", detail)
        dump(
            fixtures / "config-entity.json",
            next(
                e
                for e in client.get("/api/v1/config/label-definitions").json()["items"]
                if e["id"] == media_label["id"]
            ),
        )
        import re

        from tgcurator.domain.messages.deduplication import DuplicateStatus
        from tgcurator.infrastructure.database.models import MessageRecord

        constraint = next(
            str(c.sqltext)
            for c in MessageRecord.__table__.constraints
            if hasattr(c, "sqltext") and "processing_status IN" in str(c.sqltext)
        )
        dump(
            fixtures / "message-states.json",
            {
                "processing": re.findall("'([^']+)'", constraint),
                "duplicate": [s.value for s in DuplicateStatus],
            },
        )

app.mount("/", StaticFiles(directory=ROOT / "web/dist", html=True), name="test-static")
if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=args.port, log_level="warning", access_log=False)
