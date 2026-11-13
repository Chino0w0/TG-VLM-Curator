from __future__ import annotations

import hashlib
import io
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def _resolve_bash() -> str | None:
    configured = os.environ.get("GIT_BASH")
    if configured and Path(configured).is_file():
        return configured
    if os.name == "nt":
        for candidate in (
            "C:/Program Files/Git/bin/bash.exe",
            "D:/Program Files/Git/bin/bash.exe",
            "C:/Program Files (x86)/Git/bin/bash.exe",
            "D:/Program Files (x86)/Git/bin/bash.exe",
        ):
            if Path(candidate).is_file():
                return candidate
        found = shutil.which("bash")
        if found and "system32" not in found.lower():
            return found
        return None
    return shutil.which("bash")


BASH = _resolve_bash()


@unittest.skipUnless(BASH and Path(BASH).exists(), "Bash required")
class OperationsScriptsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.work = Path(self.temp.name)
        self.log = self.work / "calls"
        docker = self.work / "docker"
        docker.write_text(
            """#!/usr/bin/env bash
set -eu
printf '%s\n' "$*" >> "$CALL_LOG"
if [[ -n "${FAIL_MATCH:-}" && "$*" == *"$FAIL_MATCH"* ]]; then exit 7; fi
case "$*" in
  *"ps --status running --services"*) printf '%s\n' "${RUNNING:-postgres}" ;;
  *"ps --status restarting --services"*) printf '%s\n' "${RESTARTING:-}" ;;
  *pg_dump*) printf 'DUMP' ;;
  *"tar -czf"*) printf 'ARCHIVE' ;;
  *consistency_cli*) printf '{"consistent":true}'; exit "${SCAN_EXIT:-0}" ;;
esac
""",
            encoding="utf-8",
            newline="\n",
        )
        docker.chmod(0o755)
        self.env = dict(
            os.environ,
            PATH=str(self.work) + os.pathsep + os.environ["PATH"],
            PYTHON_BIN=Path(sys.executable).as_posix(),
            CALL_LOG=self.log.as_posix(),
            COMPOSE_PROJECT_NAME="unit-ops",
            BACKUP_DIR=(self.work / "backups").as_posix(),
            RESTORE_REPORT=(self.work / "report.json").as_posix(),
            MSYS_NO_PATHCONV="1",
            MSYS2_ARG_CONV_EXCL="*",
        )
        self.dump = self.work / "backup.dump"
        self.dump.write_bytes(b"DUMP")
        self.media = self.work / "backup.tar.gz"
        self.archive("safe.bin")
        self.sign(self.dump)

    def archive(self, name: str) -> None:
        with tarfile.open(self.media, "w:gz") as archive:
            item = tarfile.TarInfo(name)
            item.size = 1
            archive.addfile(item, io.BytesIO(b"x"))
        self.sign(self.media)

    @staticmethod
    def sign(path: Path) -> None:
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        Path(str(path) + ".sha256").write_text(digest + "  " + path.name + "\n")

    def run_script(self, name: str, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [str(BASH), (ROOT / "scripts" / name).as_posix(), *args],
            env=self.env,
            capture_output=True,
            text=True,
        )

    def restore(self) -> subprocess.CompletedProcess[str]:
        return self.run_script(
            "restore-postgres.sh",
            "--confirm-restore",
            self.dump.as_posix(),
            "--media",
            self.media.as_posix(),
        )

    def calls(self) -> str:
        return self.log.read_text() if self.log.exists() else ""

    def test_restore_requires_confirmation_and_explicit_project(self) -> None:
        self.assertEqual(self.run_script("restore-postgres.sh").returncode, 2)
        self.env.pop("COMPOSE_PROJECT_NAME")
        self.assertNotEqual(self.restore().returncode, 0)
        self.assertFalse(self.calls())

    def test_corruption_stops_before_docker(self) -> None:
        self.dump.write_bytes(b"corrupt")
        self.assertEqual(self.restore().returncode, 2)
        self.assertFalse(self.calls())

    def test_traversal_stops_before_docker(self) -> None:
        self.archive("../escape")
        self.assertNotEqual(self.restore().returncode, 0)
        self.assertFalse(self.calls())

    def test_active_writers_are_rejected(self) -> None:
        self.env["RUNNING"] = "postgres\nworker-media"
        self.assertEqual(self.restore().returncode, 2)
        self.assertNotIn("pg_restore", self.calls())

    def test_failed_service_discovery_is_not_silently_ignored(self) -> None:
        self.env["FAIL_MATCH"] = "ps --status"
        self.assertEqual(self.restore().returncode, 7)
        self.assertNotIn("pg_restore", self.calls())

    def test_backup_uses_matching_container_and_writes_checksum(self) -> None:
        result = self.run_script("backup-postgres.sh")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("exec -T postgres", self.calls())
        self.assertIn("pg_dump --format=custom", self.calls())
        artifact = next((self.work / "backups").glob("*.dump"))
        self.assertEqual(artifact.read_bytes(), b"DUMP")
        self.assertTrue(Path(str(artifact) + ".sha256").exists())
        self.assertIn('"master_key_included":false', Path(str(artifact) + ".meta.json").read_text())

    def test_restore_transaction_and_required_scanner(self) -> None:
        result = self.restore()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("--single-transaction --exit-on-error", self.calls())
        self.assertIn("archive-tools", self.calls())
        self.assertIn("--verify-sha256 --json", self.calls())

    def test_restore_propagates_scanner_failure(self) -> None:
        self.env["SCAN_EXIT"] = "2"
        result = self.restore()
        self.assertEqual(result.returncode, 2)
        self.assertIn("Consistency scan failed (exit 2)", result.stderr)
        self.assertTrue((self.work / "report.json").exists())

    def test_restarting_writers_are_rejected(self) -> None:
        self.env["RESTARTING"] = "worker-analysis"
        result = self.restore()
        self.assertEqual(result.returncode, 2)
        self.assertIn("worker-analysis", result.stderr)
        self.assertNotIn("pg_restore", self.calls())

    def test_uppercase_and_crlf_checksum_sidecar_is_accepted(self) -> None:
        digest = hashlib.sha256(self.dump.read_bytes()).hexdigest().upper()
        Path(str(self.dump) + ".sha256").write_bytes(
            (digest + " *" + self.dump.name + "\r\n").encode("ascii")
        )
        result = self.restore()
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_dump_failure_is_nonzero(self) -> None:
        self.env["FAIL_MATCH"] = "pg_dump"
        self.assertEqual(self.run_script("backup-postgres.sh").returncode, 7)
        self.assertFalse(list((self.work / "backups").glob("*.sha256")))

    def test_media_backup_reads_compose_named_volume(self) -> None:
        result = self.run_script("backup-media.sh")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("archive-tools tar -czf - -C /archive .", self.calls())
        self.assertTrue(list((self.work / "backups").glob("*.tar.gz.sha256")))

    def test_sql_restore_failure_stops_before_media_write(self) -> None:
        self.env["FAIL_MATCH"] = "pg_restore"
        self.assertEqual(self.restore().returncode, 7)
        self.assertNotIn("tar -xzf", self.calls())
        self.assertNotIn("consistency_cli", self.calls())

    def test_nonempty_archive_rejected_before_database_write(self) -> None:
        self.env["FAIL_MATCH"] = "find /archive"
        self.assertEqual(self.restore().returncode, 7)
        self.assertNotIn("pg_restore", self.calls())

    def test_symlink_archive_rejected(self) -> None:
        with tarfile.open(self.media, "w:gz") as archive:
            item = tarfile.TarInfo("link")
            item.type = tarfile.SYMTYPE
            item.linkname = "/etc/passwd"
            archive.addfile(item)
        self.sign(self.media)
        self.assertNotEqual(self.restore().returncode, 0)
        self.assertFalse(self.calls())
