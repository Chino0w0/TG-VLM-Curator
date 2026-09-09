from __future__ import annotations

import asyncio
import subprocess
import unittest
from pathlib import Path
from unittest.mock import patch

from tgcurator.infrastructure.media import (
    FFmpegFrameExtractionError,
    FFmpegTimeoutError,
    FFmpegVideoFrameExtractor,
)


class FFmpegVideoFrameExtractorTests(unittest.TestCase):
    def test_probe_and_extract_use_argument_lists_without_shell_and_cleanup_temp_files(
        self,
    ) -> None:
        calls: list[tuple[list[str], dict[str, object]]] = []
        paths: list[Path] = []

        def create_process(arguments: list[str], **kwargs: object) -> _Process:
            calls.append((arguments, kwargs))
            paths.append(Path(arguments[-1]))
            if arguments[0] == "safe-ffprobe":
                stdout = kwargs["stdout"]
                stdout.write(  # type: ignore[union-attr]
                    b'{"streams":[{"width":640,"height":360}],'
                    b'"format":{"duration":"7.5","format_name":"mp4,mov"}}'
                )
                stdout.flush()  # type: ignore[union-attr]
            else:
                Path(arguments[-1]).write_bytes(b"png-frame")
            return _Process()

        extractor = FFmpegVideoFrameExtractor(
            ffprobe_path="safe-ffprobe",
            ffmpeg_path="safe-ffmpeg",
        )
        with patch("tgcurator.infrastructure.media.ffmpeg.subprocess.Popen", create_process):
            metadata = asyncio.run(extractor.probe(content=b"video"))
            frame = asyncio.run(extractor.extract_frame(content=b"video", timestamp_seconds=2.5))

        self.assertEqual(metadata.duration_seconds, 7.5)
        self.assertEqual((metadata.width, metadata.height), (640, 360))
        self.assertEqual(metadata.content_type, "video/mp4")
        self.assertEqual(frame, b"png-frame")
        self.assertEqual(calls[1][0][calls[1][0].index("-ss") + 1], "2.500000")
        for arguments, kwargs in calls:
            self.assertIsInstance(arguments, list)
            self.assertIs(kwargs["shell"], False)
            self.assertIs(kwargs["stdin"], subprocess.DEVNULL)
        self.assertTrue(all(not path.parent.exists() for path in paths))

    def test_timeout_terminates_then_kills_and_returns_sanitized_error(self) -> None:
        process = _Process(timeout_count=2)
        extractor = FFmpegVideoFrameExtractor(timeout_seconds=0.01, terminate_grace_seconds=0.01)

        with patch("tgcurator.infrastructure.media.ffmpeg.subprocess.Popen", return_value=process):
            with self.assertRaisesRegex(FFmpegTimeoutError, "exceeded its timeout") as caught:
                asyncio.run(extractor.probe(content=b"secret-video"))

        self.assertTrue(process.terminated)
        self.assertTrue(process.killed)
        self.assertNotIn("secret-video", str(caught.exception))

    def test_rejects_missing_or_oversized_frame_output(self) -> None:
        extractor = FFmpegVideoFrameExtractor(max_frame_bytes=4)

        def no_frame(arguments: list[str], **_: object) -> _Process:
            return _Process()

        with patch("tgcurator.infrastructure.media.ffmpeg.subprocess.Popen", no_frame):
            with self.assertRaises(FFmpegFrameExtractionError):
                asyncio.run(extractor.extract_frame(content=b"video", timestamp_seconds=0))

        def oversized(arguments: list[str], **_: object) -> _Process:
            Path(arguments[-1]).write_bytes(b"12345")
            return _Process()

        with patch("tgcurator.infrastructure.media.ffmpeg.subprocess.Popen", oversized):
            with self.assertRaisesRegex(FFmpegFrameExtractionError, "safe bounds"):
                asyncio.run(extractor.extract_frame(content=b"video", timestamp_seconds=0))


class _Process:
    def __init__(self, *, timeout_count: int = 0) -> None:
        self.timeout_count = timeout_count
        self.wait_calls = 0
        self.terminated = False
        self.killed = False

    def wait(self, timeout: float) -> int:
        self.wait_calls += 1
        if self.wait_calls <= self.timeout_count:
            raise subprocess.TimeoutExpired("media", timeout)
        return 0

    def terminate(self) -> None:
        self.terminated = True

    def kill(self) -> None:
        self.killed = True


if __name__ == "__main__":
    unittest.main()
