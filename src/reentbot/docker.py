"""Docker container lifecycle management for audit runs."""

import asyncio
import io
import os
import tarfile
from pathlib import Path

import docker as docker_lib
from docker.errors import ImageNotFound, NotFound, APIError


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
        """Build the Docker image if it doesn't exist."""
        client = self._get_client()
        try:
            client.images.get(self.image_name)
            if on_status:
                on_status("Image found (cached)")
            return
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
            _, logs = client.images.build(
                path=str(dockerfile_path.parent),
                dockerfile=dockerfile_path.name,
                tag=self.image_name,
                rm=True,
            )
            return logs

        await asyncio.to_thread(_build)
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
                mem_limit="4g",
                cpu_period=100000,
                cpu_quota=200000,
                working_dir="/audit",
                # Network access enabled for cast/anvil/forge install
                network_mode="bridge",
            )
            return container

        self._container = await asyncio.to_thread(_create_and_start)
        if on_status:
            on_status("Container started")

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
