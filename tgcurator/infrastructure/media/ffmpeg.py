from __future__ import annotations

import asyncio
import json
import subprocess
import tempfile
import threading
from dataclasses import dataclass, field
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


@dataclass(slots=True, weakref_slot=True, eq=False)
class FFmpegVideoFrameExtractor:
    ffprobe_path: str = "ffprobe"
    ffmpeg_path: str = "ffmpeg"
    timeout_seconds: float = 30.0
    terminate_grace_seconds: float = 2.0
    max_probe_output_bytes: int = 256 * 1024
    max_stderr_bytes: int = 64 * 1024
    max_frame_bytes: int = 32 * 1024 * 1024
    temp_dir: Path | None = None
    _process_lock: threading.Lock = field(default_factory=threading.Lock, init=False, repr=False)
    _active_processes: set[subprocess.Popen[bytes]] = field(
        default_factory=set, init=False, repr=False
    )

    def terminate_active_processes(self) -> int:
        """Terminate any in-flight ffprobe/ffmpeg subprocesses during daemon shutdown."""

        with self._process_lock:
            active = list(self._active_processes)
        terminated = 0
        for process in active:
            if process.poll() is not None:
                continue
            terminated += 1
            self._terminate_process(process)
        return terminated

    def _terminate_process(self, process: subprocess.Popen[bytes]) -> None:
        try:
            process.terminate()
        except OSError:
            return
        try:
            process.wait(timeout=self.terminate_grace_seconds)
        except subprocess.TimeoutExpired:
            try:
                process.kill()
                process.wait(timeout=self.terminate_grace_seconds)
            except OSError:
                pass

    def __post_init__(self) -> None:
        if self.temp_dir is not None:
            self.temp_dir = Path(self.temp_dir).expanduser().resolve()
            self.temp_dir.mkdir(parents=True, exist_ok=True)
        for field_name, value in (
            ("ffprobe_path", self.ffprobe_path),
            ("ffmpeg_path", self.ffmpeg_path),
        ):
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{field_name} must not be blank")
        for field_name, value in (
            ("timeout_seconds", self.timeout_seconds),
            ("terminate_grace_seconds", self.terminate_grace_seconds),
        ):
            if (
                not isinstance(value, (int, float))
                or isinstance(value, bool)
                or not isfinite(value)
                or value <= 0
            ):
                raise ValueError(f"{field_name} must be a finite positive number")
        for field_name, value in (
            ("max_probe_output_bytes", self.max_probe_output_bytes),
            ("max_stderr_bytes", self.max_stderr_bytes),
            ("max_frame_bytes", self.max_frame_bytes),
        ):
            if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
                raise ValueError(f"{field_name} must be a positive integer")

    def _terminate_process_set(self, processes: set[subprocess.Popen[bytes]]) -> None:
        with self._process_lock:
            snapshot = list(processes)
        for process in snapshot:
            if process.poll() is not None:
                continue
            self._terminate_process(process)

    async def probe(self, *, content: bytes) -> VideoProbeMetadata:
        if not isinstance(content, bytes) or not content:
            raise FFmpegProbeError("video content must not be empty")
        call_processes: set[subprocess.Popen[bytes]] = set()
        try:
            return await asyncio.to_thread(self._probe_sync, content, call_processes)
        except asyncio.CancelledError:
            await asyncio.to_thread(self._terminate_process_set, call_processes)
            raise

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
        call_processes: set[subprocess.Popen[bytes]] = set()
        try:
            return await asyncio.to_thread(
                self._extract_sync, content, float(timestamp_seconds), call_processes
            )
        except asyncio.CancelledError:
            await asyncio.to_thread(self._terminate_process_set, call_processes)
            raise

    async def extract_frames(
        self,
        *,
        content: bytes,
        timestamps: tuple[float, ...],
        duration_seconds: float | None = None,
    ) -> tuple[bytes, ...]:
        if not isinstance(content, bytes) or not content:
            raise FFmpegFrameExtractionError("video content must not be empty")
        normalized_timestamps: list[float] = []
        for ts in timestamps:
            if (
                not isinstance(ts, (int, float))
                or isinstance(ts, bool)
                or not isfinite(ts)
                or ts < 0
            ):
                raise FFmpegFrameExtractionError(
                    "timestamp_seconds must be finite and non-negative"
                )
            normalized_timestamps.append(float(ts))
        if not normalized_timestamps:
            return ()
        call_processes: set[subprocess.Popen[bytes]] = set()
        try:
            return await asyncio.to_thread(
                self._extract_frames_sync,
                content,
                tuple(normalized_timestamps),
                duration_seconds,
                call_processes,
            )
        except asyncio.CancelledError:
            await asyncio.to_thread(self._terminate_process_set, call_processes)
            raise

    def _probe_sync(
        self,
        content: bytes,
        call_processes: set[subprocess.Popen[bytes]] | None = None,
    ) -> VideoProbeMetadata:
        with tempfile.TemporaryDirectory(
            prefix="tgcurator-ffprobe-", dir=self.temp_dir
        ) as directory:
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
                    "stream=width,height,duration:format=duration,format_name",
                    "-of",
                    "json",
                    str(source),
                ],
                stdout_limit=self.max_probe_output_bytes,
                error_type=FFmpegProbeError,
                call_processes=call_processes,
            )
            try:
                payload: dict[str, Any] = json.loads(completed.decode("utf-8"))
                format_payload = payload.get("format") or {}
                streams = payload.get("streams") or []
                stream = streams[0] if streams else {}
                raw_duration = format_payload.get("duration")
                if raw_duration in (None, "", "N/A"):
                    raw_duration = stream.get("duration")
                duration = float(raw_duration)  # type: ignore[arg-type]
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

    def _extract_sync(
        self,
        content: bytes,
        timestamp_seconds: float,
        call_processes: set[subprocess.Popen[bytes]] | None = None,
    ) -> bytes:
        with tempfile.TemporaryDirectory(
            prefix="tgcurator-ffmpeg-", dir=self.temp_dir
        ) as directory:
            source = Path(directory) / "source.video"
            frame = Path(directory) / "frame.png"
            source.write_bytes(content)
            return self._extract_frame_from_source(
                source=source,
                frame=frame,
                timestamp_seconds=timestamp_seconds,
                duration_seconds=None,
                call_processes=call_processes,
            )

    def _extract_frames_sync(
        self,
        content: bytes,
        timestamps: tuple[float, ...],
        duration_seconds: float | None,
        call_processes: set[subprocess.Popen[bytes]] | None = None,
    ) -> tuple[bytes, ...]:
        with tempfile.TemporaryDirectory(
            prefix="tgcurator-ffmpeg-", dir=self.temp_dir
        ) as directory:
            source = Path(directory) / "source.video"
            source.write_bytes(content)
            outputs: list[bytes] = []
            for index, timestamp_seconds in enumerate(timestamps):
                frame = Path(directory) / f"frame_{index}.png"
                outputs.append(
                    self._extract_frame_from_source(
                        source=source,
                        frame=frame,
                        timestamp_seconds=timestamp_seconds,
                        duration_seconds=duration_seconds,
                        call_processes=call_processes,
                    )
                )
                try:
                    frame.unlink()
                except FileNotFoundError:
                    pass
            return tuple(outputs)

    def _extract_frame_from_source(
        self,
        *,
        source: Path,
        frame: Path,
        timestamp_seconds: float,
        duration_seconds: float | None,
        call_processes: set[subprocess.Popen[bytes]] | None,
    ) -> bytes:
        seek_seconds = timestamp_seconds
        if (
            duration_seconds is not None
            and duration_seconds > 0
            and timestamp_seconds >= duration_seconds
        ):
            seek_seconds = max(0.0, duration_seconds - min(0.1, duration_seconds * 0.05))
        self._run(
            [
                self.ffmpeg_path,
                "-v",
                "error",
                "-ss",
                f"{seek_seconds:.6f}",
                "-i",
                str(source),
                "-frames:v",
                "1",
                "-y",
                str(frame),
            ],
            stdout_limit=self.max_probe_output_bytes,
            error_type=FFmpegFrameExtractionError,
            call_processes=call_processes,
        )
        if timestamp_seconds > 0 and (not frame.exists() or frame.stat().st_size == 0):
            self._run(
                [
                    self.ffmpeg_path,
                    "-v",
                    "error",
                    "-sseof",
                    "-0.1",
                    "-i",
                    str(source),
                    "-frames:v",
                    "1",
                    "-y",
                    str(frame),
                ],
                stdout_limit=self.max_probe_output_bytes,
                error_type=FFmpegFrameExtractionError,
                call_processes=call_processes,
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
        call_processes: set[subprocess.Popen[bytes]] | None = None,
    ) -> bytes:
        with (
            tempfile.TemporaryFile(dir=self.temp_dir) as stdout,
            tempfile.TemporaryFile(dir=self.temp_dir) as stderr,
        ):
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
            with self._process_lock:
                self._active_processes.add(process)
                if call_processes is not None:
                    call_processes.add(process)
            try:
                return_code = process.wait(timeout=self.timeout_seconds)
            except subprocess.TimeoutExpired as error:
                self._terminate_process(process)
                raise FFmpegTimeoutError("media executable exceeded its timeout") from error
            except BaseException:
                self._terminate_process(process)
                raise
            finally:
                with self._process_lock:
                    self._active_processes.discard(process)
                    if call_processes is not None:
                        call_processes.discard(process)
            stderr.seek(0)
            stderr.read(self.max_stderr_bytes + 1)
            if return_code != 0:
                raise error_type("media executable returned a failure status")
            stdout.seek(0)
            result = stdout.read(stdout_limit + 1)
            if len(result) > stdout_limit:
                raise error_type("media executable output exceeded safe bounds")
            return result
