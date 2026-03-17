"""Docker container lifecycle management for audit runs."""

import asyncio
import io
import os
import subprocess
import tarfile
from pathlib import Path

import docker as docker_lib
from docker.errors import ImageNotFound, NotFound, APIError

# Always build and run as linux/amd64.  The Solidity toolchain does not publish
# native Linux ARM64 binaries — on Apple Silicon this causes fallback to WASM
# solc builds that hit memory limits and produce unreliable results.
PLATFORM = "linux/amd64"


class AuditContainer:
    """Manages the Docker container lifecycle for an audit run."""

    def __init__(self, image_name: str = "reentbot-tools"):
        self.image_name = image_name
        self._docker: docker_lib.DockerClient | None = None
        self._container = None

    def _get_client(self) -> docker_lib.DockerClient:
        if self._docker is None:
            try:
                self._docker = docker_lib.from_env()
                self._docker.ping()
            except docker_lib.errors.DockerException as e:
                raise RuntimeError(
                    "Docker is not running or not accessible. "
                    "Please start Docker and try again."
                ) from e
        return self._docker

    async def ensure_image(self, on_status=None) -> None:
        """Build the Docker image if it doesn't exist or has wrong architecture."""
        client = self._get_client()
        try:
            img = client.images.get(self.image_name)
            if img.attrs.get("Architecture") == "amd64":
                if on_status:
                    on_status("Image found (cached)")
                return
            # Wrong architecture (e.g. arm64 from before platform enforcement).
            # Rebuilding with the same tag replaces the old image.
            if on_status:
                on_status("Cached image is not amd64 — rebuilding...")
        except ImageNotFound:
            pass

        if on_status:
            on_status("Building audit container image (this may take several minutes on first run)...")

        # Find the Dockerfile bundled with the package
        dockerfile_path = Path(__file__).parent / "Dockerfile"
        if not dockerfile_path.exists():
            raise FileNotFoundError(
                f"Dockerfile not found at {dockerfile_path}. "
                "Ensure the package is installed correctly."
            )

        def _build():
            result = subprocess.run(
                [
                    "docker", "buildx", "build",
                    "--platform", PLATFORM,
                    "--load",
                    "-t", self.image_name,
                    "-f", str(dockerfile_path),
                    str(dockerfile_path.parent),
                ],
                capture_output=True,
                text=True,
            )
            if result.returncode != 0:
                raise RuntimeError(
                    f"Docker image build failed:\n{result.stderr}"
                )

        await asyncio.to_thread(_build)

        # Verify the built image has the correct architecture.
        try:
            img = client.images.get(self.image_name)
        except ImageNotFound:
            raise RuntimeError(
                "Docker image build completed but image was not found. "
                "This may indicate an issue with 'docker buildx build --load'."
            )
        if img.attrs.get("Architecture") != "amd64":
            raise RuntimeError(
                f"Docker image build completed but produced "
                f"'{img.attrs.get('Architecture')}' architecture instead of 'amd64'. "
                f"Ensure Docker Desktop has cross-platform build support enabled."
            )

        if on_status:
            on_status("Image built successfully")

    async def start(self, source_dir: str, rpc_url: str | None = None, on_status=None) -> None:
        """Start a container with source_dir mounted at /audit."""
        client = self._get_client()
        await self.ensure_image(on_status=on_status)

        abs_source = os.path.abspath(source_dir)
        if not os.path.isdir(abs_source):
            raise ValueError(f"Source directory does not exist: {abs_source}")

        env_vars = {}
        if rpc_url:
            env_vars["ETH_RPC_URL"] = rpc_url

        if on_status:
            on_status("Starting container...")

        def _create_and_start():
            container = client.containers.run(
                self.image_name,
                detach=True,
                volumes={
                    abs_source: {"bind": "/audit", "mode": "rw"},
                },
                tmpfs={"/workspace": "size=1G"},
                environment=env_vars,
                mem_limit="8g",
                cpu_period=100000,
                cpu_quota=200000,
                working_dir="/audit",
                # Network access enabled for cast/anvil/forge install
                network_mode="bridge",
            )
            return container

        self._container = await asyncio.to_thread(_create_and_start)
        await self._init_source(on_status=on_status)
        if on_status:
            on_status("Container ready")

    async def _init_source(self, on_status=None) -> None:
        """Initialize the mounted source: git config, submodules, and dependencies."""
        # Ensure git trusts the bind-mounted directory (ownership differs
        # between host user and container root).  Redundant with the
        # Dockerfile config but needed for cached / older images.
        await self.exec(
            "git config --global --add safe.directory '*'", timeout=5
        )
        # Initialize git submodules if present — Foundry projects store
        # dependencies (OpenZeppelin, forge-std, etc.) as submodules in lib/.
        exit_code, _ = await self.exec("[ -f .gitmodules ]", timeout=5)
        if exit_code == 0:
            if on_status:
                on_status("Initializing git submodules...")
            await self.exec(
                "git submodule update --init --recursive 2>/dev/null || true",
                timeout=120,
            )

        # Install npm/yarn dependencies if package.json exists.
        exit_code, _ = await self.exec("[ -f package.json ]", timeout=5)
        if exit_code == 0:
            if on_status:
                on_status("Installing npm dependencies...")
            yarn_check, _ = await self.exec("[ -f yarn.lock ]", timeout=5)
            if yarn_check == 0:
                await self.exec(
                    "yarn install 2>/dev/null || true", timeout=120
                )
            else:
                await self.exec(
                    "npm install 2>/dev/null || true", timeout=120
                )

        # Install forge-std if this is a Foundry project and forge-std is missing.
        # The agent needs forge-std to write custom Foundry tests and PoC exploits.
        exit_code, _ = await self.exec(
            "[ -d .git ] && [ -f foundry.toml ] && [ ! -d lib/forge-std ]",
            timeout=5,
        )
        if exit_code == 0:
            if on_status:
                on_status("Installing forge-std...")
            await self.exec(
                "forge install foundry-rs/forge-std --no-commit 2>/dev/null || true",
                timeout=60,
            )

    async def exec(
        self, command: str, working_dir: str = "/audit", timeout: int = 120
    ) -> tuple[int, str]:
        """Run a command inside the container. Returns (exit_code, output)."""
        if self._container is None:
            raise RuntimeError("Container not started")

        def _run():
            result = self._container.exec_run(
                ["bash", "-c", command],
                workdir=working_dir,
                demux=False,
            )
            output = (result.output or b"").decode("utf-8", errors="replace")
            return result.exit_code, output

        try:
            return await asyncio.wait_for(
                asyncio.to_thread(_run), timeout=timeout
            )
        except asyncio.TimeoutError:
            # Try to kill any lingering process
            return -1, f"Command timed out after {timeout}s"

    async def write_file(self, container_path: str, content: str) -> None:
        """Write a file into the container using put_archive."""
        if self._container is None:
            raise RuntimeError("Container not started")

        def _write():
            data = content.encode("utf-8")
            tarstream = io.BytesIO()
            tarinfo = tarfile.TarInfo(name=os.path.basename(container_path))
            tarinfo.size = len(data)
            with tarfile.open(fileobj=tarstream, mode="w") as tar:
                tar.addfile(tarinfo, io.BytesIO(data))
            tarstream.seek(0)
            self._container.put_archive(
                os.path.dirname(container_path) or "/", tarstream
            )

        await asyncio.to_thread(_write)

    async def read_file(self, container_path: str) -> str:
        """Read a file from the container."""
        exit_code, output = await self.exec(f"cat '{container_path}'")
        if exit_code != 0:
            raise FileNotFoundError(f"Failed to read {container_path}: {output}")
        return output

    async def stop(self) -> None:
        """Stop and remove the container."""
        if self._container is not None:
            def _stop():
                try:
                    self._container.stop(timeout=5)
                except (APIError, NotFound):
                    pass
                try:
                    self._container.remove(force=True)
                except (APIError, NotFound):
                    pass

            await asyncio.to_thread(_stop)
            self._container = None

    @property
    def is_running(self) -> bool:
        return self._container is not None
