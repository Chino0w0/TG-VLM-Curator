from __future__ import annotations

import base64
import hashlib
import json
import tempfile
import unittest
from dataclasses import replace
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID

import httpx
from PIL import Image

from tests.unit.test_analysis_orchestrator import _stage
from tgcurator.application.analysis import AnalysisOrchestrator, AnalysisTargetInput
from tgcurator.application.analysis.input_composer import AnalysisInputError
from tgcurator.application.analysis.visual_inputs import VisualInputLimits
from tgcurator.application.ports.cleanup import MappingArchiveStorageResolver
from tgcurator.domain.analysis import (
    ExecutionMode,
    LabelScope,
    StageInputPolicy,
    VisualCompositionPolicy,
)
from tgcurator.infrastructure.archive.local_storage import LocalArchiveStorage
from tgcurator.infrastructure.inference.openai_compatible import OpenAICompatibleInferenceProvider


class Secrets:
    async def resolve(self, *, secret_reference):
        return "test-secret-never-persist"


class VisualInputResolutionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.storage = LocalArchiveStorage(root=Path(self.temp.name))
        self.requests = []
        self.echo_error = False
        self.provider = OpenAICompatibleInferenceProvider(
            secret_resolver=Secrets(),
            transport=httpx.MockTransport(self.capture),
        )
        self.orchestrator = AnalysisOrchestrator(
            provider=self.provider,
            storage_resolver=MappingArchiveStorageResolver({"local": self.storage}),
        )

    async def asyncTearDown(self):
        self.temp.cleanup()

    def capture(self, request):
        body = json.loads(request.content)
        self.requests.append(body)
        if self.echo_error:
            return httpx.Response(
                400,
                json={
                    "error": {
                        "authorization": "Bearer test-secret-never-persist",
                        "message": "test-secret-never-persist " + json.dumps(body),
                    },
                },
            )
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "content": json.dumps(
                                {"result": {"scores": {"keep": 0.95, "block": 0.1}}}
                            ),
                        }
                    }
                ]
            },
        )

    def stage(self, *, policy=VisualCompositionPolicy.RAW, max_images=8):
        stage = _stage(scope=LabelScope.MEDIA)
        return replace(
            stage,
            visual_composition_policy=policy,
            inference_profile=replace(stage.inference_profile, max_images=max_images),
        )

    async def image(self, index=0):
        buffer = BytesIO()
        Image.new("RGB", (40 + index, 24), (index * 31 % 255, 100, 200)).save(buffer, "PNG")
        content = buffer.getvalue()
        key = f"images/{index}.png"
        await self.storage.put(key=key, content=content, content_type="image/png")
        return {
            "kind": "image",
            "asset_id": "asset-a",
            "storage_backend": "local",
            "storage_key": key,
            "archive_sha256": hashlib.sha256(content).hexdigest(),
        }, content

    def target(self, media):
        return AnalysisTargetInput(
            target_id="asset-a", message_id="message-a", asset_id="asset-a", media=tuple(media)
        )

    async def video(self, count):
        frames = []
        for index in range(count):
            descriptor, _ = await self.image(index)
            frames.append(
                {
                    **descriptor,
                    "frame_role": "analysis",
                    "frame_index": index,
                    "timestamp_seconds": index * 1.5,
                }
            )
        return {"kind": "video", "asset_id": "asset-a", "frames": frames}

    async def prepare(self, media, **kwargs):
        return await self.orchestrator.prepare_async(
            stage=self.stage(**kwargs),
            targets=(self.target(media),),
        )

    def assert_capture_matches_manifest(self, prepared):
        parts = self.requests[-1]["messages"][-1]["content"]
        self.assertIsInstance(parts, list)
        sent = [part["image_url"]["url"] for part in parts if part["type"] == "image_url"]
        manifest = prepared.request.input_manifest
        visual = manifest.content["inputs"]["visual_inputs"]
        self.assertEqual(len(sent), len(visual))
        self.assertLessEqual(len(sent), prepared.request.profile.max_images)
        for index, (url, item) in enumerate(zip(sent, visual, strict=True)):
            content = base64.b64decode(url.partition(",")[2], validate=True)
            self.assertEqual(item["index"], index)
            self.assertEqual(hashlib.sha256(content).hexdigest(), item["sha256"])
            self.assertEqual(len(content), item["size_bytes"])
            with Image.open(BytesIO(content)) as image:
                self.assertEqual(list(image.size), [item["width"], item["height"]])
            for source in item["sources"]:
                left, top, right, bottom = source["region"]
                self.assertTrue(0 <= left < right <= item["width"])
                self.assertTrue(0 <= top < bottom <= item["height"])
        serialized = json.dumps(manifest.content)
        self.assertNotIn("data:image", serialized)
        self.assertNotIn("test-secret-never-persist", serialized)
        self.assertNotIn("storage_key", serialized)
        return sent, visual

    async def test_real_archived_image_becomes_image_part_and_valid_label(self):
        descriptor, content = await self.image()
        prepared = await self.prepare([descriptor])
        invocation = await self.orchestrator.invoke(prepared)
        self.assertTrue(invocation.succeeded)
        urls, visual = self.assert_capture_matches_manifest(prepared)
        self.assertEqual(base64.b64decode(urls[0].split(",")[1]), content)
        self.assertEqual(visual[0]["sources"][0]["asset_id"], "asset-a")
        result = self.orchestrator.validate(
            stage=self.stage(), targets=(self.target([descriptor]),), response=invocation.response
        )
        self.assertTrue(result.succeeded)
        self.assertEqual(result.validation.valid_results[0].target_id, "asset-a")
        self.assertEqual(prepared.request.input_manifest.manifest_version, 2)

    async def test_all_video_frames_and_input_order_are_transmitted(self):
        video = await self.video(3)
        video["frames"] = list(reversed(video["frames"]))
        prepared = await self.prepare([video])
        await self.orchestrator.invoke(prepared)
        _, visual = self.assert_capture_matches_manifest(prepared)
        self.assertEqual([i["sources"][0]["frame_index"] for i in visual], [2, 1, 0])
        self.assertEqual([i["sources"][0]["timestamp_seconds"] for i in visual], [3.0, 1.5, 0.0])

    async def test_contact_sheet_has_actual_regions_and_all_frame_hashes(self):
        video = await self.video(3)
        prepared = await self.prepare(
            [video], policy=VisualCompositionPolicy.CONTACT_SHEET, max_images=1
        )
        await self.orchestrator.invoke(prepared)
        _, visual = self.assert_capture_matches_manifest(prepared)
        self.assertEqual(len(visual), 1)
        self.assertEqual(visual[0]["composition"], "contact_sheet")
        self.assertEqual([s["frame_index"] for s in visual[0]["sources"]], [0, 1, 2])
        self.assertEqual(
            [s["sha256"] for s in visual[0]["sources"]],
            [frame["archive_sha256"] for frame in video["frames"]],
        )

    async def test_contact_sheet_splits_without_exceeding_image_budget(self):
        prepared = await self.prepare(
            [await self.video(7)], policy=VisualCompositionPolicy.CONTACT_SHEET, max_images=2
        )
        await self.orchestrator.invoke(prepared)
        _, visual = self.assert_capture_matches_manifest(prepared)
        self.assertEqual([len(part["sources"]) for part in visual], [4, 3])

    async def test_adaptive_raw_within_budget_and_sheet_when_over(self):
        video = await self.video(2)
        for budget, expected in ((2, "raw"), (1, "contact_sheet")):
            prepared = await self.prepare(
                [video], policy=VisualCompositionPolicy.ADAPTIVE, max_images=budget
            )
            await self.orchestrator.invoke(prepared)
            _, visual = self.assert_capture_matches_manifest(prepared)
            self.assertEqual(visual[0]["composition"], expected)

    async def test_raw_over_budget_is_recoverable_and_never_calls_provider(self):
        with self.assertRaises(AnalysisInputError) as caught:
            await self.prepare([await self.video(2)], max_images=1)
        self.assertEqual(caught.exception.error_type, "VisualImageBudgetExceeded")
        self.assertTrue(caught.exception.retryable)
        self.assertEqual(self.requests, [])

    async def test_adaptive_over_sheet_capacity_fails_without_truncation(self):
        with self.assertRaises(AnalysisInputError) as caught:
            await self.prepare(
                [await self.video(5)], policy=VisualCompositionPolicy.ADAPTIVE, max_images=1
            )
        self.assertEqual(caught.exception.error_type, "VisualImageBudgetExceeded")
        self.assertEqual(self.requests, [])

    async def test_missing_corrupt_hash_mismatch_and_empty_video_fail_closed(self):
        image, _ = await self.image()
        await self.storage.put(key="bad.png", content=b"not an image", content_type="image/png")
        cases = [
            ([{**image, "storage_key": "missing.png"}], "VisualArchiveReadFailed"),
            (
                [{**image, "storage_key": "bad.png", "archive_sha256": None}],
                "VisualImageDecodeFailed",
            ),
            ([{**image, "archive_sha256": "0" * 64}], "VisualArchiveHashMismatch"),
            ([{"kind": "video", "asset_id": "asset-a", "frames": []}], "VideoFramesUnavailable"),
            ([{"kind": "image", "asset_id": "asset-a"}], "VisualArchiveReferenceMissing"),
            ([], "RequiredMediaUnavailable"),
        ]
        for media, expected in cases:
            with self.subTest(expected=expected):
                result = await self.orchestrator.execute(
                    stage=self.stage(), targets=(self.target(media),)
                )
                self.assertFalse(result.succeeded)
                self.assertIsNone(result.validation)
                self.assertEqual(result.failure.error_type, expected)
                self.assertTrue(result.failure.retryable)
        self.assertEqual(self.requests, [])

    async def test_one_bad_frame_fails_whole_request(self):
        video = await self.video(2)
        video["frames"][1]["storage_key"] = "missing.png"
        result = await self.orchestrator.execute(
            stage=self.stage(), targets=(self.target([video]),)
        )
        self.assertFalse(result.succeeded)
        self.assertEqual(self.requests, [])

    async def test_missing_resolver_is_not_text_fallback(self):
        image, _ = await self.image()
        result = await AnalysisOrchestrator(provider=self.provider).execute(
            stage=self.stage(), targets=(self.target([image]),)
        )
        self.assertEqual(result.failure.error_type, "VisualArchiveResolverUnavailable")
        self.assertEqual(self.requests, [])

    async def test_sync_prepare_requires_explicit_resolved_images(self):
        image, _ = await self.image()
        with self.assertRaises(AnalysisInputError):
            self.orchestrator.prepare(stage=self.stage(), targets=(self.target([image]),))
        prepared = await self.prepare([image])
        again = self.orchestrator.prepare(
            stage=self.stage(),
            targets=(self.target([image]),),
            visual_inputs=prepared.request.visual_inputs,
        )
        self.assertEqual(prepared.request.input_manifest, again.request.input_manifest)

    async def test_direct_adapter_rejects_missing_tampered_or_over_budget_images(self):
        prepared = await self.prepare([await self.video(2)])
        requests = [
            replace(prepared.request, visual_inputs=()),
            replace(prepared.request, profile=replace(prepared.request.profile, max_images=1)),
            replace(
                prepared.request,
                visual_inputs=(
                    replace(prepared.request.visual_inputs[0], content=b"tampered"),
                    prepared.request.visual_inputs[1],
                ),
            ),
        ]
        for request in requests:
            invocation = await self.orchestrator.invoke(replace(prepared, request=request))
            self.assertFalse(invocation.succeeded)
        self.assertEqual(self.requests, [])

    async def test_data_url_is_decoded_and_not_audited_including_nested_frames(self):
        _, content = await self.image()
        url = "data:image/png;base64," + base64.b64encode(content).decode("ascii")
        video = {
            "kind": "video",
            "asset_id": "asset-a",
            "frames": [
                {
                    "frame_role": "analysis",
                    "frame_index": 0,
                    "timestamp_seconds": 0.0,
                    "data_url": url,
                    "api_key": "test-secret-never-persist",
                }
            ],
            "provider_url": "https://example.invalid/?token=test-secret-never-persist",
        }
        prepared = await self.prepare([video])
        await self.orchestrator.invoke(prepared)
        self.assert_capture_matches_manifest(prepared)
        self.assertNotIn(
            base64.b64encode(content).decode("ascii"),
            json.dumps(prepared.request.input_manifest.content),
        )

    async def test_external_url_without_archive_is_explicit_failure(self):
        with self.assertRaises(AnalysisInputError):
            await self.prepare(
                [{"asset_id": "asset-a", "provider_url": "https://example.invalid/a"}]
            )
        self.assertEqual(self.requests, [])

    async def test_input_byte_pixel_and_source_limits_are_enforced(self):
        image, content = await self.image()
        for limits in (
            VisualInputLimits(max_source_bytes=len(content) - 1),
            VisualInputLimits(max_pixels=10),
            VisualInputLimits(max_total_bytes=len(content) - 1),
        ):
            orchestrator = AnalysisOrchestrator(
                provider=self.provider,
                storage_resolver=MappingArchiveStorageResolver({"local": self.storage}),
                visual_input_limits=limits,
            )
            result = await orchestrator.execute(stage=self.stage(), targets=(self.target([image]),))
            self.assertFalse(result.succeeded)
            self.assertTrue(result.failure.retryable)
        orchestrator = AnalysisOrchestrator(
            provider=self.provider,
            storage_resolver=MappingArchiveStorageResolver({"local": self.storage}),
            visual_input_limits=VisualInputLimits(max_source_images=1),
        )
        result = await orchestrator.execute(
            stage=self.stage(), targets=(self.target([await self.video(2)]),)
        )
        self.assertEqual(result.failure.error_type, "VisualSourceBudgetExceeded")
        self.assertEqual(self.requests, [])

    async def test_cache_changes_with_visual_policy_order_and_actual_bytes(self):
        video = await self.video(2)
        raw = await self.prepare([video])
        repeated = await self.prepare([video])
        sheet = await self.prepare([video], policy=VisualCompositionPolicy.CONTACT_SHEET)
        reversed_order = await self.prepare([{**video, "frames": list(reversed(video["frames"]))}])
        self.assertEqual(raw.cache_key, repeated.cache_key)
        self.assertNotEqual(raw.cache_key, sheet.cache_key)
        self.assertNotEqual(raw.cache_key, reversed_order.cache_key)

    async def test_partial_resolved_sources_cannot_bypass_required_frames(self):
        video = await self.video(2)
        prepared = await self.prepare([video])
        with self.assertRaises(AnalysisInputError) as caught:
            self.orchestrator.prepare(
                stage=self.stage(),
                targets=(self.target([video]),),
                visual_inputs=prepared.request.visual_inputs[:1],
            )
        self.assertEqual(caught.exception.error_type, "VisualSourceMappingIncomplete")

    async def test_batch_assets_have_unambiguous_cross_target_sheet_mapping(self):
        image_a, _ = await self.image(0)
        image_b, _ = await self.image(1)
        image_b["asset_id"] = "asset-b"
        targets = (
            self.target([image_a]),
            AnalysisTargetInput(
                target_id="asset-b", message_id="message-b", asset_id="asset-b", media=(image_b,)
            ),
        )
        stage = replace(
            self.stage(policy=VisualCompositionPolicy.ADAPTIVE, max_images=1),
            execution_mode=ExecutionMode.BATCH_ASSETS,
            max_batch_size=2,
        )
        prepared = await self.orchestrator.prepare_async(stage=stage, targets=targets)
        await self.orchestrator.invoke(prepared)
        _, visual = self.assert_capture_matches_manifest(prepared)
        self.assertEqual([s["target_id"] for s in visual[0]["sources"]], ["asset-a", "asset-b"])

    async def test_provider_echoed_images_and_credentials_are_not_audited(self):
        self.echo_error = True
        image, _ = await self.image()
        prepared = await self.prepare([image])
        invocation = await self.orchestrator.invoke(prepared)
        raw = json.dumps(invocation.failure.raw_response)
        self.assertNotIn("test-secret-never-persist", raw)
        self.assertNotIn("data:image", raw)
        self.assertIn("redacted", raw)

    async def test_text_only_does_not_resolve_unused_invalid_media(self):
        stage = _stage(scope=LabelScope.GLOBAL)
        target = AnalysisTargetInput(
            target_id="message-a",
            message_id="message-a",
            text="hello",
            media=({"kind": "video", "frames": []},),
        )
        prepared = await self.orchestrator.prepare_async(stage=stage, targets=(target,))
        await self.orchestrator.invoke(prepared)
        self.assertIsInstance(self.requests[-1]["messages"][-1]["content"], str)
        self.assertEqual(prepared.request.visual_inputs, ())

    async def test_repository_snapshot_shape_resolves_without_repository_changes(self):
        from tgcurator.infrastructure.database.analysis_repository import _load_media_snapshot

        descriptor, content = await self.image()
        common = {
            **descriptor,
            "content_type": "image/png",
            "width": 40,
            "height": 24,
            "source_sha256": descriptor["archive_sha256"],
            "perceptual_hash": "0" * 16,
            "archive_size_bytes": len(content),
        }
        aid, mid = UUID(int=2), UUID(int=1)
        image = SimpleNamespace(id=aid, **common)
        frame = SimpleNamespace(
            video_asset_id=aid,
            frame_role="representative",
            frame_index=0,
            timestamp_seconds=2.0,
            **common,
        )
        video = SimpleNamespace(
            id=aid,
            source_content_type="video/mp4",
            source_size_bytes=100,
            source_sha256="b" * 64,
            duration_seconds=4.0,
            source_width=40,
            source_height=24,
        )

        class Session:
            def __init__(self, batches):
                self.batches = list(batches)

            async def scalars(self, statement):
                return self.batches.pop(0)

        for kind, batches in (
            ("image_asset", [(image,), ()]),
            ("video_asset", [(), (video,), (frame,)]),
        ):
            row = SimpleNamespace(target_kind=kind, image_asset_id=aid, video_asset_id=aid)
            media, _ = await _load_media_snapshot(
                session=Session(batches),
                stage_run=row,
                message=SimpleNamespace(id=mid, visual_fingerprint="a" * 64),
            )
            target = AnalysisTargetInput(
                target_id=str(aid), message_id=str(mid), asset_id=str(aid), media=media
            )
            prepared = await self.orchestrator.prepare_async(stage=self.stage(), targets=(target,))
            invocation = await self.orchestrator.invoke(prepared)
            self.assertTrue(invocation.succeeded)
            urls, visual = self.assert_capture_matches_manifest(prepared)
            self.assertEqual(base64.b64decode(urls[0].split(",")[1]), content)
            self.assertEqual(visual[0]["sources"][0]["asset_id"], str(aid))

    async def test_sheet_regions_contain_corresponding_real_frame_pixels(self):
        prepared = await self.prepare(
            [await self.video(3)], policy=VisualCompositionPolicy.CONTACT_SHEET, max_images=1
        )
        sheet = prepared.request.visual_inputs[0]
        with Image.open(BytesIO(sheet.content)) as actual:
            for source in sheet.sources:
                left, top, right, bottom = source["region"]
                pixel = actual.getpixel(((left + right) // 2, (top + bottom) // 2))
                expected = (source["frame_index"] * 31, 100, 200)
                self.assertTrue(all(abs(a - b) <= 4 for a, b in zip(pixel, expected, strict=True)))

    async def test_global_message_can_send_multiple_assets_and_video_frames(self):
        image, _ = await self.image(0)
        image["asset_id"] = "image-a"
        video = await self.video(2)
        video["asset_id"] = "video-b"
        stage = _stage(scope=LabelScope.GLOBAL)
        stage = replace(
            stage,
            input_policy=StageInputPolicy(text=True, media=True),
            visual_composition_policy=VisualCompositionPolicy.RAW,
        )
        target = AnalysisTargetInput(
            target_id="message-a", message_id="message-a", text="all media", media=(image, video)
        )
        prepared = await self.orchestrator.prepare_async(stage=stage, targets=(target,))
        await self.orchestrator.invoke(prepared)
        _, visual = self.assert_capture_matches_manifest(prepared)
        self.assertEqual(
            [p["sources"][0]["asset_id"] for p in visual], ["image-a", "video-b", "video-b"]
        )

    async def test_duplicate_or_invalid_frame_identity_fails_closed(self):
        video = await self.video(2)
        for frames in (
            [video["frames"][0], video["frames"][0]],
            [{**video["frames"][0], "timestamp_seconds": float("nan")}],
            [{**video["frames"][0], "frame_index": -1}],
        ):
            with self.assertRaises(AnalysisInputError) as caught:
                await self.prepare([{**video, "frames": frames}])
            self.assertEqual(caught.exception.error_type, "VideoFrameIdentityInvalid")
        self.assertEqual(self.requests, [])

    async def test_animated_image_requires_explicit_frame_extraction(self):
        buffer = BytesIO()
        first = Image.new("RGB", (20, 20), "red")
        first.save(buffer, "GIF", save_all=True, append_images=[Image.new("RGB", (20, 20), "blue")])
        await self.storage.put(
            key="animated.gif", content=buffer.getvalue(), content_type="image/gif"
        )
        result = await self.orchestrator.execute(
            stage=self.stage(),
            targets=(
                self.target(
                    [
                        {
                            "kind": "image",
                            "asset_id": "asset-a",
                            "storage_backend": "local",
                            "storage_key": "animated.gif",
                        }
                    ]
                ),
            ),
        )
        self.assertEqual(result.failure.error_type, "AnimatedVisualRequiresFrames")
        self.assertEqual(self.requests, [])
