from __future__ import annotations

import subprocess
import hashlib
import os
import stat

from boring_worker_kit.framing import MAX_FRAME_BYTES

from .instructions import WELCOME
from collections.abc import Mapping
from pathlib import Path


class ContextBootstrap:
    """Load one Pond's maintained context from its fixed, trusted root."""

    def __init__(
        self,
        root: Path,
        files: tuple[str, ...],
        projects: Mapping[str, tuple[str, ...]] | None = None,
        welcome: str = WELCOME,
        welcome_file: str | None = None,
    ) -> None:
        self.root = root
        self.files = files
        self.projects = dict(projects or {})
        self.welcome = welcome
        self.welcome_file = welcome_file

    @classmethod
    def for_environment(cls, settings, environment):
        pond = next((p for p in settings.ponds.values() if p.environment == environment), None)
        if pond is None:
            return None
        return cls(pond.context_root, pond.files, pond.projects, pond.welcome, pond.welcome_file)

    def load(self, project: str | None = None) -> dict[str, object]:
        handbook = self._load_location(self.root, self.files)
        welcome_document = None
        if self.welcome_file is not None:
            welcome_document = next((doc for doc in handbook["documents"]
                                     if doc["filename"] == self.welcome_file), None)
            if welcome_document is None:
                welcome_document = self._load_location(
                    self.root, (self.welcome_file,))["documents"][0]
        if welcome_document is not None:
            # The selected welcome has its own content and hash in the response.
            # Keep all context, but do not repeat its text in the handbook.
            handbook["documents"] = [doc for doc in handbook["documents"]
                                     if doc["filename"] != self.welcome_file]
            manifest = "\n".join(doc["filename"] + ":" + doc["sha256"]
                                 for doc in handbook["documents"])
            handbook["content_revision"] = "sha256:" + hashlib.sha256(manifest.encode()).hexdigest()
        result: dict[str, object] = {
            "welcome": welcome_document["content"] if welcome_document else self.welcome,
            "welcome_document": ({key: welcome_document[key] for key in ("filename", "sha256")}
                                 if welcome_document else None),
            "handbook": handbook,
            "project": None,
        }

        if project is not None:
            result["project"] = self._load_project(project)

        result["status"] = "loaded"
        return result

    def _load_project(self, project: str) -> dict[str, object]:
        if project not in self.projects:
            raise ValueError(
                f"Unknown project {project!r}. Known projects: {', '.join(sorted(self.projects))}")
        context = self._load_location(self.root, self.projects[project])
        context["name"] = project
        return context

    @staticmethod
    def _load_location(
        root: Path,
        document_names: tuple[str, ...],
    ) -> dict[str, object]:
        root = root.resolve(strict=True)
        if not document_names:
            raise ValueError("Context file list must not be empty")
        documents = []
        for name in document_names:
            relative = Path(name)
            if relative.is_absolute() or ".." in relative.parts:
                raise ValueError("Context filenames must stay within their configured root")
            path = (root / relative).resolve(strict=True)
            if root not in path.parents:
                raise ValueError("Context file escapes its configured root")
            # A mutable handbook must not block a worker on a FIFO or allocate
            # unbounded memory. The transport owns the final encoded-size limit.
            fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW)
            with os.fdopen(fd, "rb") as stream:
                if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                    raise ValueError("Context document must be a regular file")
                raw = stream.read(MAX_FRAME_BYTES + 1)
            if len(raw) > MAX_FRAME_BYTES:
                raise ValueError("Context document exceeds the transport limit")
            documents.append({"filename": name, "sha256": hashlib.sha256(raw).hexdigest(),
                              "content": raw.decode("utf-8")})
        manifest = "\n".join(item["filename"] + ":" + item["sha256"] for item in documents)
        return {
            "root": str(root),
            "revision": ContextBootstrap._git_revision(root),
            "content_revision": "sha256:" + hashlib.sha256(manifest.encode()).hexdigest(),
            "documents": documents,
        }

    @staticmethod
    def _git_revision(root: Path) -> str | None:
        try:
            completed = subprocess.run(
                ["git", "-C", str(root), "rev-parse", "HEAD"],
                check=False,
                capture_output=True,
                text=True,
            )
        except FileNotFoundError:
            # Published snapshots need content hashes, not a Git installation.
            return None
        if completed.returncode != 0:
            return None
        return completed.stdout.strip()
