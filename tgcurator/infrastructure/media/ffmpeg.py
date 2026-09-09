from __future__ import annotations

import asyncio
import json
import subprocess
import tempfile
from dataclasses import dataclass
from math import isfinite
from pathlib import Path
from typing import Any

from tgcurator.application.ports.media import VideoProbeMetadata


class FFmpegMediaError(RuntimeError):
    """Sanitized base error for bounded ffprobe/FFmpeg execution."""


class FFmpegTimeoutError(FFmpegMediaError):
    pass


class FFmpegProbeError(FFmpegMediaError):
    pass


class FFmpegFrameExtractionError(FFmpegMediaError):
    pass


@dataclass(slots=True)
class FFmpegVideoFrameExtractor:
    ffprobe_path: str = "ffprobe"
    ffmpeg_path: str = "ffmpeg"
    timeout_seconds: float = 30.0
    terminate_grace_seconds: float = 2.0
    max_probe_output_bytes: int = 256 * 1024
    max_stderr_bytes: int = 64 * 1024
    max_frame_bytes: int = 32 * 1024 * 1024

    def __post_init__(self) -> None:
        for field, value in (
            ("ffprobe_path", self.ffprobe_path),
            ("ffmpeg_path", self.ffmpeg_path),
        ):
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{field} must not be blank")
        for field, value in (
            ("timeout_seconds", self.timeout_seconds),
            ("terminate_grace_seconds", self.terminate_grace_seconds),
        ):
            if (
                not isinstance(value, (int, float))
                or isinstance(value, bool)
                or not isfinite(value)
                or value <= 0
            ):
                raise ValueError(f"{field} must be a finite positive number")
        for field, value in (
            ("max_probe_output_bytes", self.max_probe_output_bytes),
            ("max_stderr_bytes", self.max_stderr_bytes),
            ("max_frame_bytes", self.max_frame_bytes),
        ):
            if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
                raise ValueError(f"{field} must be a positive integer")

    async def probe(self, *, content: bytes) -> VideoProbeMetadata:
        if not isinstance(content, bytes) or not content:
            raise FFmpegProbeError("video content must not be empty")
        return await asyncio.to_thread(self._probe_sync, content)

    async def probe_duration(self, *, content: bytes) -> float:
        return (await self.probe(content=content)).duration_seconds

    async def extract_frame(self, *, content: bytes, timestamp_seconds: float) -> bytes:
        if not isinstance(content, bytes) or not content:
            raise FFmpegFrameExtractionError("video content must not be empty")
        if (
            not isinstance(timestamp_seconds, (int, float))
            or isinstance(timestamp_seconds, bool)
            or not isfinite(timestamp_seconds)
            or timestamp_seconds < 0
        ):
            raise FFmpegFrameExtractionError("timestamp_seconds must be finite and non-negative")
        return await asyncio.to_thread(self._extract_sync, content, float(timestamp_seconds))

    def _probe_sync(self, content: bytes) -> VideoProbeMetadata:
        with tempfile.TemporaryDirectory(prefix="tgcurator-ffprobe-") as directory:
            source = Path(directory) / "source.video"
            source.write_bytes(content)
            completed = self._run(
                [
                    self.ffprobe_path,
                    "-v",
                    "error",
                    "-select_streams",
                    "v:0",
                    "-show_entries",
                    "stream=width,height:format=duration,format_name",
                    "-of",
                    "json",
                    str(source),
                ],
                stdout_limit=self.max_probe_output_bytes,
                error_type=FFmpegProbeError,
            )
            try:
                payload: dict[str, Any] = json.loads(completed.decode("utf-8"))
                format_payload = payload.get("format") or {}
                streams = payload.get("streams") or []
                stream = streams[0] if streams else {}
                duration = float(format_payload["duration"])
                width = int(stream["width"]) if stream.get("width") is not None else None
                height = int(stream["height"]) if stream.get("height") is not None else None
                format_name = str(format_payload.get("format_name") or "").split(",", 1)[0]
            except (KeyError, TypeError, ValueError, json.JSONDecodeError, IndexError) as error:
                raise FFmpegProbeError("ffprobe returned invalid video metadata") from error
            content_type = f"video/{format_name}" if format_name else None
            return VideoProbeMetadata(
                duration_seconds=duration,
                width=width,
                height=height,
                content_type=content_type,
            )

    def _extract_sync(self, content: bytes, timestamp_seconds: float) -> bytes:
        with tempfile.TemporaryDirectory(prefix="tgcurator-ffmpeg-") as directory:
            source = Path(directory) / "source.video"
            frame = Path(directory) / "frame.png"
            source.write_bytes(content)
            self._run(
                [
                    self.ffmpeg_path,
                    "-v",
                    "error",
                    "-ss",
                    f"{timestamp_seconds:.6f}",
                    "-i",
                    str(source),
                    "-frames:v",
                    "1",
                    "-y",
                    str(frame),
                ],
                stdout_limit=self.max_probe_output_bytes,
                error_type=FFmpegFrameExtractionError,
            )
            try:
                size = frame.stat().st_size
            except FileNotFoundError as error:
                raise FFmpegFrameExtractionError("FFmpeg did not produce a frame") from error
            if size <= 0 or size > self.max_frame_bytes:
                raise FFmpegFrameExtractionError("FFmpeg frame output exceeded safe bounds")
            result = frame.read_bytes()
            if len(result) != size:
                raise FFmpegFrameExtractionError("FFmpeg frame output could not be read completely")
            return result

    def _run(
        self,
        arguments: list[str],
        *,
        stdout_limit: int,
        error_type: type[FFmpegMediaError],
    ) -> bytes:
        with tempfile.TemporaryFile() as stdout, tempfile.TemporaryFile() as stderr:
            try:
                process = subprocess.Popen(
                    arguments,
                    stdin=subprocess.DEVNULL,
                    stdout=stdout,
                    stderr=stderr,
                    shell=False,
                )
            except OSError as error:
                raise error_type("media executable could not be started") from error
            try:
                return_code = process.wait(timeout=self.timeout_seconds)
            except subprocess.TimeoutExpired as error:
                process.terminate()
                try:
                    process.wait(timeout=self.terminate_grace_seconds)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=self.terminate_grace_seconds)
                raise FFmpegTimeoutError("media executable exceeded its timeout") from error
            stderr.seek(0)
            stderr.read(self.max_stderr_bytes + 1)
            if return_code != 0:
                raise error_type("media executable returned a failure status")
            stdout.seek(0)
            result = stdout.read(stdout_limit + 1)
            if len(result) > stdout_limit:
                raise error_type("media executable output exceeded safe bounds")
            return result
