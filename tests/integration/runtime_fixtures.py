"""Only external Telegram/HTTP peers are fake; runtime, SQL, broker and tasks are real."""

from __future__ import annotations

import base64
import io
import json
import os
import threading
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from unittest import SkipTest
from uuid import UUID, uuid4

from PIL import Image
from pydantic import SecretStr
from sqlalchemy.engine import make_url
from telethon import types

from apps.runtime_dependencies import configured_secret_vault
from tgcurator.application import Settings
from tgcurator.domain.analysis.common import canonical_hash
from tgcurator.infrastructure.database import models as m
from tgcurator.infrastructure.database.admin_api_repository import SqlAlchemyAdminBackend

MODES = ("forward_only", "native_forward_with_supplement", "copy_with_caption", "metadata_only")


def redis_test_url():
    value = os.environ.get("TGCURATOR_TEST_REDIS_URL")
    if not value:
        config = os.environ.get("TGCURATOR_TEST_SERVICES_FILE")
        if not config:
            raise SkipTest("set TGCURATOR_TEST_REDIS_URL (DB15) for real Redis tests")
        data = json.loads(Path(config).read_text(encoding="utf-8"))
        value = f"redis://{data['host']}:{data['redis_port']}/15"
    url = make_url(value)
    if url.drivername not in {"redis", "rediss"} or url.database != "15":
        raise RuntimeError("runtime tests require an explicit Redis DB15 URL")
    return value


def runtime_settings(database_url, broker_url, archive_root):
    return Settings(
        _env_file=None,
        environment="test",
        database_url=database_url,
        celery_broker_url=SecretStr(broker_url),
        app_master_key=SecretStr(base64.b64encode(os.urandom(32)).decode()),
        archive_root=archive_root,
    )


class ExternalTelegram:
    """Telethon-shaped external peer, never a repository/gateway replacement."""

    def __init__(self):
        self.clients = []
        self.events = []
        self.requests = []
        self.messages = {}
        self.remote_ids = {}
        self.lock = threading.Lock()
        self.inference_key = uuid4().hex
        self.http_requests = []
        self.http_errors = []
        self.image = io.BytesIO()
        Image.new("RGB", (96, 64), (37, 114, 203)).save(self.image, "PNG")

    def add_message(self, message_id, *, text="runtime image", peer=-100111):
        photo = types.Photo(
            id=message_id,
            access_hash=1234,
            file_reference=b"test",
            date=datetime.now(UTC),
            sizes=[],
            dc_id=1,
        )
        msg = SimpleNamespace(
            id=message_id,
            date=datetime.now(UTC) - timedelta(minutes=10),
            raw_text=text,
            photo=photo,
            media=types.MessageMediaPhoto(photo=photo),
            noforwards=False,
            grouped_id=None,
            edit_date=None,
        )
        self.messages.setdefault(peer, {})[message_id] = msg
        return msg

    def factory(self, *, credentials, receive_updates):
        assert credentials.api_id == 12345
        client = ExternalTelegramClient(self, receive_updates)
        self.clients.append(client)
        return client


class ExternalTelegramClient:
    def __init__(self, peer, receive_updates):
        self.peer = peer
        self.receive_updates = receive_updates
        self.connected = False
        self.disconnects = 0
        self.handlers = []

    async def connect(self):
        self.connected = True

    async def is_user_authorized(self):
        return True

    def is_connected(self):
        return self.connected

    async def get_dialogs(self, limit=None):
        return []

    async def disconnect(self):
        self.connected = False
        self.disconnects += 1

    async def iter_messages(self, entity, *, min_id=0, max_id=0, reverse=False, limit=None):
        values = [
            m
            for i, m in self.peer.messages.get(entity, {}).items()
            if i > min_id and (not max_id or i < max_id)
        ]
        values.sort(key=lambda m: m.id, reverse=not reverse)
        for message in values[:limit] if limit is not None else values:
            yield message

    async def get_messages(self, entity, *, ids):
        source = self.peer.messages.get(entity, {})
        return [source.get(i) for i in ids] if isinstance(ids, list) else source.get(ids)

    async def download_media(self, message, *, file):
        self.peer.events.append(("download", message.id))
        return self.peer.image.getvalue()

    async def __call__(self, request):
        self.peer.requests.append(request)
        random_ids = request.random_id
        if not isinstance(random_ids, list):
            random_ids = [random_ids]
        updates = []
        with self.peer.lock:
            for rid in random_ids:
                remote = self.peer.remote_ids.setdefault(rid, 10000 + len(self.peer.remote_ids))
                updates.append(types.UpdateMessageID(id=remote, random_id=rid))
        return types.Updates(updates=updates, users=[], chats=[], date=datetime.now(UTC), seq=1)

    def add_event_handler(self, handler, builder):
        self.handlers.append((handler, builder))

    def remove_event_handler(self, handler, builder):
        self.handlers.remove((handler, builder))


@contextmanager
def inference_http_server(peer):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            try:
                assert self.path == "/v1/chat/completions"
                assert self.headers.get("Authorization") == "Bearer " + peer.inference_key
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                peer.http_requests.append(body)
                schema = body["response_format"]["json_schema"]["schema"]
                props = schema["properties"]
                batch = "results" in props
                item = props["results"]["items"] if batch else props["result"]
                keys = item["properties"]["scores"]["properties"]
                blocked = "second runtime message" in json.dumps(body["messages"])
                scores = {
                    key: (float(blocked) if key == "reject" else float(not blocked)) for key in keys
                }
                if batch:
                    targets = item["properties"]["message_id"]["enum"]
                    payload = {"results": [{"message_id": t, "scores": scores} for t in targets]}
                    peer.events.append(("prescreen", len(targets)))
                else:
                    payload = {"result": {"scores": scores}}
                    peer.events.append(("vision", len(peer.http_requests)))
                content = json.dumps(
                    {
                        "choices": [{"message": {"content": json.dumps(payload)}}],
                        "usage": {"prompt_tokens": 12, "completion_tokens": 3},
                    }
                ).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(content)))
                self.end_headers()
                self.wfile.write(content)
            except Exception as error:
                peer.http_errors.append(type(error).__name__)
                self.send_error(500)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True, name="runtime-fake-http")
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/v1"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(5)


async def seed_configuration(db, settings, peer, base_url, *, end_mode):
    """Real vault/admin configuration publication; ORM only for source-profile shell."""
    now = datetime.now(UTC)
    vault = configured_secret_vault(settings=settings, database=db)
    tg_secret = await vault.store(
        secret_type="telegram_session",
        plaintext=json.dumps(
            {
                "api_id": 12345,
                "api_hash": uuid4().hex,
                "session_string": "external-test-only",
            }
        ).encode(),
    )
    api_secret = await vault.store(secret_type="api_key", plaintext=peer.inference_key.encode())
    identity, source = uuid4(), uuid4()
    async with db.session() as s, s.begin():
        s.add(
            m.TelegramIdentity(
                id=identity, name="runtime-test", identity_type="mtproto_user", secret_id=tg_secret
            )
        )
        await s.flush()
        s.add(
            m.SourceChannel(
                id=source,
                telegram_peer_id=-100111,
                display_name="runtime-source",
                default_read_identity_id=identity,
            )
        )
    admin = SqlAlchemyAdminBackend(db)

    async def create(family, content):
        entity = await admin.create_config_entity(
            family=family,
            payload={"name": "runtime-" + uuid4().hex, "initial_version": content},
            now=now,
        )
        return await admin.publish_config_version(
            family=family,
            entity_id=entity["id"],
            version_id=entity["versions"][0]["id"],
            supersede_published=False,
            now=now,
        )

    keep = await create(
        "label-definitions", {"key": "keep", "display_name": "Keep", "scope": "global"}
    )
    reject = await create(
        "label-definitions",
        {"key": "reject", "display_name": "Reject", "scope": "global", "negative": True},
    )
    labels = await create(
        "label-sets",
        {
            "bindings": [
                {"label_definition_version_id": keep["id"], "activation_threshold": 0.5},
                {"label_definition_version_id": reject["id"], "activation_threshold": 0.5},
            ]
        },
    )
    prompt = await create(
        "prompt-templates",
        {
            "system_prompt": "Analyze runtime fixture",
            "user_prompt_template": "Analyze {{message_text}}",
            "declared_variables": ["message_text"],
        },
    )
    profile = await create(
        "inference-profiles",
        {
            "base_url": base_url,
            "model_name": "external-test",
            "api_secret_reference": str(api_secret),
            "capabilities": ["text", "vision"],
        },
    )
    common = {
        "prompt_template_version_id": prompt["id"],
        "label_set_version_id": labels["id"],
        "inference_profile_version_id": profile["id"],
    }
    pre = await create(
        "analysis-stages",
        {
            **common,
            "name": "prescreen",
            "execution_mode": "batch_messages",
            "input_policy": {"text": True},
        },
    )
    vision = await create(
        "analysis-stages",
        {**common, "name": "vision", "input_policy": {"text": True, "media": True}},
    )
    pipeline = await create(
        "analysis-pipelines",
        {
            "nodes": [
                {
                    "node_key": "prescreen",
                    "analysis_stage_template_version_id": pre["id"],
                    "depends_on": [],
                },
                {
                    "node_key": "vision",
                    "analysis_stage_template_version_id": vision["id"],
                    "depends_on": ["prescreen"],
                    "run_if": {"label": {"namespace": "model", "scope": "global", "key": "keep"}},
                },
                {
                    "node_key": "not_selected",
                    "analysis_stage_template_version_id": vision["id"],
                    "depends_on": ["prescreen"],
                    "run_if": {
                        "not": {"label": {"namespace": "model", "scope": "global", "key": "keep"}}
                    },
                },
            ]
        },
    )
    template = await create("rendering-templates", {"template_text": "Runtime publication"})
    actions = []
    for i, mode in enumerate(MODES):
        dest = await admin.create_destination_channel(
            payload={
                "name": mode,
                "telegram_peer_id": -100200 - i,
                "default_publish_identity_id": str(identity),
            },
            now=now,
        )
        action = {"destination_channel_id": dest["id"], "publication_mode": mode}
        if mode != "forward_only":
            action["rendering_template_version_id"] = template["id"]
        actions.append(action)
    route = await create(
        "routing-policies",
        {
            "rules": [
                {
                    "priority": 1,
                    "condition": {
                        "label": {"namespace": "effective", "scope": "global", "key": "keep"}
                    },
                    "actions": actions,
                }
            ]
        },
    )
    content = {
        "analysis_pipeline_version_id": pipeline["id"],
        "routing_policy_version_id": route["id"],
        "prescreen_enabled": True,
        "duplicate_policy": "skip",
    }
    profile_id, version_id = uuid4(), uuid4()
    async with db.session() as s, s.begin():
        s.add(m.SourceChannelProfile(id=profile_id, source_channel_id=source, name="runtime"))
        await s.flush()
        s.add(
            m.SourceChannelProfileVersion(
                id=version_id,
                profile_id=profile_id,
                version_number=1,
                state="published",
                content=content,
                content_hash=canonical_hash(content),
                published_at=now,
            )
        )
        await s.flush()
        (await s.get(m.SourceChannel, source)).active_profile_version_id = version_id
    processing_range = await admin.create_processing_range(
        payload={
            "source_channel_id": str(source),
            "start_at": (now - timedelta(days=1)).isoformat(),
            "end_mode": end_mode,
            "end_at": now.isoformat() if end_mode == "fixed" else None,
            "steady_after_seconds": 1 if end_mode == "latest" else None,
            "resolved_start_message_id": 1,
            "resolved_fixed_end_message_id": 2 if end_mode == "fixed" else None,
        },
        now=now,
    )
    return SimpleNamespace(
        identity=identity,
        source=source,
        range_id=UUID(processing_range["id"]),
        pre=pre["id"],
        vision=vision["id"],
        pipeline=pipeline["id"],
    )
