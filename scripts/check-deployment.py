"""Destructive ONLY to UUID-named local smoke projects created here; never uses .env.

Runs real production API/nginx TLS login, PG16 backup/restore, named-volume media
and independently recovered master key. Requires Docker Compose, Bash, runtime deps.
"""

from __future__ import annotations

import base64
import json
import os
import secrets
import shutil
import ssl
import subprocess
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import httpx
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

ROOT = Path(__file__).resolve().parents[1]
BASH = "D:/Program Files/Git/bin/bash.exe" if os.name == "nt" else "bash"


def certificate(directory: Path) -> tuple[Path, Path]:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "localhost")])
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(datetime.now(UTC) - timedelta(minutes=5))
        .not_valid_after(datetime.now(UTC) + timedelta(days=1))
        .add_extension(x509.SubjectAlternativeName([x509.DNSName("localhost")]), False)
        .add_extension(x509.BasicConstraints(ca=True, path_length=0), True)
        .sign(key, hashes.SHA256())
    )
    cert_path, key_path = directory / "local-only.pem", directory / "local-only.key"
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    key_path.chmod(0o600)
    return cert_path, key_path


def run(
    args: list[str], env: dict[str, str], data: bytes | None = None, expected: int = 0
) -> bytes:
    result = subprocess.run(args, cwd=ROOT, env=env, input=data, capture_output=True)
    if result.returncode != expected:
        # Commands may contain passwords in environment or stdin. Neither is logged.
        output = (result.stdout + result.stderr).decode("utf-8", errors="replace")
        for key, value in env.items():
            if any(token in key for token in ("PASSWORD", "MASTER_KEY")) and value:
                output = output.replace(value, "[REDACTED]")
        raise RuntimeError(f"Command failed ({result.returncode}, expected {expected}):\n{output}")
    return result.stdout


def main() -> None:
    prefix = f"tgcurator-ops-smoke-{uuid4().hex[:12]}"
    with tempfile.TemporaryDirectory(prefix=prefix) as tmp:
        work = Path(tmp)
        cert, key = certificate(work)
        master = work / "master-key"
        master.write_bytes(base64.b64encode(secrets.token_bytes(32)))
        master.chmod(0o644)  # Container UID 10001 must read this disposable file.
        recovered = work / "independent-vault-copy"
        shutil.copyfile(master, recovered)
        envfile = work / "empty.env"
        envfile.write_text("", encoding="utf-8")
        env = {
            k: v
            for k, v in os.environ.items()
            if not k.startswith(("COMPOSE_", "TGCURATOR_", "POSTGRES_"))
        }
        env.update(
            COMPOSE_PROJECT_NAME=prefix,
            COMPOSE_FILE=str(ROOT / "docker-compose.yml"),
            COMPOSE_ENV_FILE=str(envfile),
            TGCURATOR_ENVIRONMENT="production",
            POSTGRES_USER="smoke",
            POSTGRES_DB="smoke",
            POSTGRES_PASSWORD=secrets.token_hex(24),
            TGCURATOR_BIND_ADDRESS="127.0.0.1",
            TGCURATOR_HTTP_PORT="0",
            TGCURATOR_HTTPS_PORT="0",
            TGCURATOR_TLS_CERT_HOST_PATH=str(cert),
            TGCURATOR_TLS_KEY_HOST_PATH=str(key),
            TGCURATOR_MASTER_KEY_HOST_PATH=str(master),
            MSYS_NO_PATHCONV="1",
            MSYS2_ARG_CONV_EXCL="*",
        )
        target = dict(
            env,
            COMPOSE_PROJECT_NAME=prefix + "-restore",
            TGCURATOR_MASTER_KEY_HOST_PATH=str(recovered),
        )

        def compose(
            e: dict[str, str], *args: str, data: bytes | None = None, expected: int = 0
        ) -> bytes:
            return run(
                [
                    "docker",
                    "compose",
                    "--project-name",
                    e["COMPOSE_PROJECT_NAME"],
                    "--env-file",
                    str(envfile),
                    "--file",
                    env["COMPOSE_FILE"],
                    *args,
                ],
                e,
                data,
                expected,
            )

        endpoint = (
            env.get("DOCKER_HOST")
            or run(["docker", "context", "inspect", "--format", "{{.Endpoints.docker.Host}}"], env)
            .decode()
            .strip()
        )
        if not endpoint.startswith(("unix://", "npipe://")):
            raise RuntimeError("Smoke requires a local Unix-socket/npipe Docker Engine")
        try:
            print("Building isolated API and TLS frontend", flush=True)
            compose(env, "build", "api", "web")
            compose(env, "up", "-d", "--wait", "postgres", "redis")
            compose(env, "run", "--rm", "-T", "--no-deps", "api", "alembic", "upgrade", "head")
            admin_password = secrets.token_urlsafe(24)
            seed = f"PASSWORD = {admin_password!r}\n" + SEED
            compose(
                env,
                "run",
                "--rm",
                "-T",
                "--no-deps",
                "--entrypoint",
                "python",
                "api",
                "-",
                data=seed.encode(),
            )
            compose(
                env,
                "run",
                "--rm",
                "-T",
                "--no-deps",
                "archive-tools",
                "sh",
                "-c",
                "printf smoke-media > /archive/smoke-orphan.bin",
            )
            compose(env, "up", "-d", "--wait", "api", "web")
            https_port = compose(env, "port", "web", "443").decode().strip().rsplit(":", 1)[1]
            http_port = compose(env, "port", "web", "80").decode().strip().rsplit(":", 1)[1]
            # Keep OS-selected random ports while testing the real nonstandard-port redirect.
            env.update(
                TGCURATOR_HTTPS_PORT=https_port,
                TGCURATOR_HTTP_PORT=http_port,
                TGCURATOR_HTTPS_REDIRECT_PORT=https_port,
            )
            compose(env, "up", "-d", "--wait", "web")
            tls_login(https_port, http_port, cert, admin_password)
            print("TLS verified; Secure cookie login and /auth/me passed", flush=True)
            compose(env, "stop", "api", "web")
            env["BACKUP_DIR"] = (work / "backups").as_posix()
            run([BASH, (ROOT / "scripts/backup-postgres.sh").as_posix()], env)
            run([BASH, (ROOT / "scripts/backup-media.sh").as_posix()], env)
            dump = next((work / "backups").glob("*.dump"))
            media = next((work / "backups").glob("*.tar.gz"))
            compose(target, "build", "api")
            compose(target, "up", "-d", "--wait", "postgres")
            target["RESTORE_REPORT"] = (work / "consistency.json").as_posix()
            # The deliberate orphan exercises the mandatory scanner's nonzero status.
            run(
                [
                    BASH,
                    (ROOT / "scripts/restore-postgres.sh").as_posix(),
                    "--confirm-restore",
                    dump.as_posix(),
                    "--media",
                    media.as_posix(),
                ],
                target,
                expected=2,
            )
            report = json.loads((work / "consistency.json").read_text())
            assert report["orphan_files"], "orphan discrepancy was not reported"
            contents = compose(
                target,
                "run",
                "--rm",
                "-T",
                "--no-deps",
                "archive-tools",
                "cat",
                "/archive/smoke-orphan.bin",
            )
            assert contents == b"smoke-media"
            compose(
                target,
                "run",
                "--rm",
                "-T",
                "--no-deps",
                "--entrypoint",
                "python",
                "api",
                "-",
                data=VERIFY.encode(),
            )
            compose(
                target,
                "run",
                "--rm",
                "-T",
                "--no-deps",
                "archive-tools",
                "rm",
                "/archive/smoke-orphan.bin",
            )
            compose(
                target,
                "run",
                "--rm",
                "-T",
                "--no-deps",
                "--entrypoint",
                "python",
                "api",
                "-m",
                "apps.worker.consistency_cli",
                "--verify-sha256",
                "--json",
            )
            print(
                "PG16 + media + separate key restored; orphan failure and clean scan verified",
                flush=True,
            )
        finally:
            cleanup_errors = []
            for e in (target, env):
                try:
                    compose(e, "down", "--volumes", "--remove-orphans")
                except Exception as exc:
                    cleanup_errors.append(exc)
            for image in (prefix + "-api", prefix + "-web", prefix + "-restore-api"):
                exists = (
                    subprocess.run(
                        ["docker", "image", "inspect", image], env=env, capture_output=True
                    ).returncode
                    == 0
                )
                if exists:
                    try:
                        run(["docker", "image", "rm", image], env)
                    except Exception as exc:
                        cleanup_errors.append(exc)
            if cleanup_errors:
                raise RuntimeError(
                    "Smoke cleanup failed; inspect only " + prefix
                ) from cleanup_errors[0]
            print("Removed only this smoke run's two projects and volumes", flush=True)


def tls_login(https_port: str, http_port: str, cert: Path, password: str) -> None:
    with httpx.Client(
        verify=ssl.create_default_context(cafile=str(cert)),
        trust_env=False,
        base_url=f"https://localhost:{https_port}",
    ) as client:
        response = client.post(
            "/api/v1/auth/login", json={"username": "smoke", "password": password}
        )
        assert response.status_code == 200, f"login status {response.status_code}"
        cookie = response.headers["set-cookie"].lower()
        assert "secure" in cookie and "httponly" in cookie and "samesite=lax" in cookie
        assert client.get("/api/v1/auth/me").status_code == 200
        redirect = client.get(f"http://localhost:{http_port}/api/v1/auth/me")
        assert redirect.status_code == 308 and redirect.headers["location"].startswith("https://")
        assert redirect.headers["location"] == f"https://localhost:{https_port}/api/v1/auth/me"
        assert "cookie" not in redirect.request.headers
        assert client.get("/metrics").status_code == 404
    # Default trust must reject this local-only self-signed certificate.
    try:
        httpx.get(f"https://localhost:{https_port}/health/live", trust_env=False)
    except httpx.ConnectError as exc:
        assert "CERTIFICATE_VERIFY_FAILED" in str(exc), "expected certificate trust failure"
    else:
        raise AssertionError("Local-only test certificate unexpectedly trusted")


SEED = """
import asyncio
from pathlib import Path
from tgcurator.application import get_settings
from tgcurator.application.admin import BootstrapAdminService
from tgcurator.infrastructure.database import AsyncDatabase, SqlAlchemyAdminBootstrapRepository
from tgcurator.infrastructure.database.secret_vault import SqlAlchemySecretVault
from tgcurator.infrastructure.security import Argon2idPasswordHasher
from tgcurator.infrastructure.security.aes_gcm import AesGcmSecretCipher
async def seed():
    settings = get_settings()
    db = AsyncDatabase(settings.database_url)
    try:
        await BootstrapAdminService(repository=SqlAlchemyAdminBootstrapRepository(db),
            password_hasher=Argon2idPasswordHasher()).bootstrap(username="smoke", password=PASSWORD)
        cipher = AesGcmSecretCipher(master_key=Path('/run/secrets/app_master_key').read_bytes())
        await SqlAlchemySecretVault(db, cipher).store(
            secret_type="smoke", plaintext=b"independent-key-check")
    finally:
        await db.dispose()
asyncio.run(seed())
"""

VERIFY = """
import asyncio
from pathlib import Path
from tgcurator.application import get_settings
from tgcurator.infrastructure.database import AsyncDatabase
from tgcurator.infrastructure.database.secret_vault import SqlAlchemySecretVault
from tgcurator.infrastructure.security.aes_gcm import AesGcmSecretCipher, SecretDecryptionError
async def verify():
    db = AsyncDatabase(get_settings().database_url)
    try:
        cipher = AesGcmSecretCipher(master_key=Path('/run/secrets/app_master_key').read_bytes())
        vault = SqlAlchemySecretVault(db, cipher)
        entries = await vault.list_statuses()
        assert len(entries) == 1
        assert await vault.resolve(secret_id=entries[0].secret_id) == b"independent-key-check"
        import base64
        wrong_cipher = AesGcmSecretCipher(master_key=base64.b64encode(b'x' * 32))
        wrong = SqlAlchemySecretVault(db, wrong_cipher)
        try:
            await wrong.resolve(secret_id=entries[0].secret_id)
        except SecretDecryptionError:
            pass
        else:
            raise AssertionError('wrong master key accepted')
    finally:
        await db.dispose()
asyncio.run(verify())
"""

if __name__ == "__main__":
    main()
