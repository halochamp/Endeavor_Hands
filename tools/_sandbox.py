"""Internal macOS sandbox execution seam.

Production callers use :class:`RealSandboxBackend`, which always invokes the
real ``sandbox-exec`` binary.  Tests may explicitly inject
:class:`DirectExecTestBackend`; there is deliberately no environment-variable
or runtime auto-detection that can downgrade production to unsandboxed exec.
"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
import os
import shutil
import subprocess
import tempfile
from typing import Iterator, Sequence


def build_sandbox_profile(
    workspace: str,
    extra_write_paths: tuple[str, ...] = (),
    extra_read_paths: tuple[str, ...] = (),
    extra_unlink_paths: tuple[str, ...] = (),
) -> str:
    """Build the shared macOS sandbox profile used by guarded subprocess tools."""
    home = os.path.expanduser("~")
    extra = "".join(f' (subpath "{os.path.realpath(p)}")' for p in extra_write_paths)
    extra_read = "".join(f' (subpath "{os.path.realpath(p)}")' for p in extra_read_paths)
    extra_unlink = "".join(f' (subpath "{os.path.realpath(p)}")' for p in extra_unlink_paths)
    return f"""(version 1)
(allow default)

; deny writes to protected/user-sensitive paths
(deny file-write*
  (subpath "/etc") (subpath "/private/etc")
  (subpath "/usr") (subpath "/bin") (subpath "/sbin")
  (subpath "/System") (subpath "/Library") (subpath "/Applications")
  (subpath "{home}/Desktop")
  (subpath "{home}/Documents") (subpath "{home}/Downloads")
  (subpath "{home}/Movies") (subpath "{home}/Music") (subpath "{home}/Pictures")
  (subpath "{home}/.ssh") (subpath "{home}/.aws")
  (subpath "{home}/.config") (subpath "{home}/.gnupg")
  (subpath "{home}/Library")
)

; deny read credentials + session history
(deny file-read*
  (subpath "{home}/.ssh")
  (subpath "{home}/.aws")
  (subpath "{home}/.gnupg")
  (subpath "{home}/.claude")
  (subpath "{home}/.config")
)

; workspace + /tmp + explicit capability overrides
(allow file-write* (subpath "{workspace}") (subpath "/private/tmp"){extra})
(allow file-read*  (subpath "{workspace}"){extra_read})

; ordinary shell/Python callers may edit but never unlink workspace files
(deny file-write-unlink (subpath "{workspace}"))

; narrowly scoped capabilities (currently Git metadata) may opt into unlink
{f'(allow file-write-unlink{extra_unlink})' if extra_unlink else ''}
"""


@dataclass(frozen=True)
class SandboxInvocation:
    argv: tuple[str, ...]
    profile_path: str | None


@dataclass(frozen=True)
class SandboxSpawn:
    process: subprocess.Popen
    profile_path: str | None


def cleanup_profile(path: str | None) -> None:
    if path:
        try:
            os.unlink(path)
        except OSError:
            pass


class RealSandboxBackend:
    """Production backend. Never falls back to direct execution."""

    def __init__(self, sandbox_exec: str | None = None) -> None:
        self.sandbox_exec = sandbox_exec or shutil.which("sandbox-exec") or "/usr/bin/sandbox-exec"

    def _write_profile(self, profile: str, profile_dir: str | os.PathLike[str] | None = None) -> str:
        if profile_dir is not None:
            os.makedirs(profile_dir, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            mode="w",
            suffix=".sb",
            dir=profile_dir,
            delete=False,
        ) as handle:
            handle.write(profile)
            return handle.name

    @contextmanager
    def prepare(
        self,
        argv: Sequence[str],
        *,
        profile: str,
        profile_dir: str | os.PathLike[str] | None = None,
    ) -> Iterator[SandboxInvocation]:
        profile_path = self._write_profile(profile, profile_dir)
        try:
            yield SandboxInvocation(
                (self.sandbox_exec, "-f", profile_path, *(str(v) for v in argv)),
                profile_path,
            )
        finally:
            cleanup_profile(profile_path)

    def run(
        self,
        argv: Sequence[str],
        *,
        profile: str,
        profile_dir: str | os.PathLike[str] | None = None,
        **kwargs,
    ) -> subprocess.CompletedProcess:
        with self.prepare(argv, profile=profile, profile_dir=profile_dir) as invocation:
            return subprocess.run(list(invocation.argv), **kwargs)

    def spawn(
        self,
        argv: Sequence[str],
        *,
        profile: str,
        profile_dir: str | os.PathLike[str] | None = None,
        **kwargs,
    ) -> SandboxSpawn:
        """Spawn and transfer profile cleanup ownership to the caller.

        ``sandbox-exec`` reads/compiles its profile after ``Popen`` returns, so
        background callers must retain the profile until the child is known to
        have exited.  This method therefore never deletes it automatically.
        """
        profile_path = self._write_profile(profile, profile_dir)
        try:
            process = subprocess.Popen(
                [self.sandbox_exec, "-f", profile_path, *(str(v) for v in argv)],
                **kwargs,
            )
            return SandboxSpawn(process, profile_path)
        except Exception:
            cleanup_profile(profile_path)
            raise


class DirectExecTestBackend:
    """Explicit unit-test backend; never selected by production automatically."""

    def __init__(self) -> None:
        self.prepared_argv: list[tuple[str, ...]] = []

    @contextmanager
    def prepare(
        self,
        argv: Sequence[str],
        *,
        profile: str,
        profile_dir: str | os.PathLike[str] | None = None,
    ) -> Iterator[SandboxInvocation]:
        del profile, profile_dir
        direct = tuple(str(v) for v in argv)
        self.prepared_argv.append(direct)
        yield SandboxInvocation(direct, None)

    def run(
        self,
        argv: Sequence[str],
        *,
        profile: str,
        profile_dir: str | os.PathLike[str] | None = None,
        **kwargs,
    ) -> subprocess.CompletedProcess:
        with self.prepare(argv, profile=profile, profile_dir=profile_dir) as invocation:
            return subprocess.run(list(invocation.argv), **kwargs)

    def spawn(
        self,
        argv: Sequence[str],
        *,
        profile: str,
        profile_dir: str | os.PathLike[str] | None = None,
        **kwargs,
    ) -> SandboxSpawn:
        del profile, profile_dir
        direct = tuple(str(v) for v in argv)
        self.prepared_argv.append(direct)
        return SandboxSpawn(subprocess.Popen(list(direct), **kwargs), None)
