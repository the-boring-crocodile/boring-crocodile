"""Controller file-tool registration; effects and root policy belong to the worker."""
from __future__ import annotations
from typing import Annotated, Callable
from mcp.server.mcpserver import MCPServer
from mcp.types import ToolAnnotations
from pydantic import BaseModel, ConfigDict, Field
from boring_worker_kit.batch_contracts import Encoding, Newline, SearchLimit
from .worker_client import WorkerClient

class FileEdit(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    old_text: str = Field(min_length=1)
    new_text: str

FileLine = Annotated[int, Field(strict=True, ge=1, le=10000)]

FileLimit = Annotated[int, Field(strict=True, ge=1, le=500)]


def register_file_tools(mcp: MCPServer, workers: WorkerClient,
                        selected_environment: Callable[[], str]) -> None:
    @mcp.tool(annotations=ToolAnnotations(read_only_hint=True, destructive_hint=False,
                                           idempotent_hint=True, open_world_hint=False))
    async def file_read(root: str, path: str, start_line: FileLine = 1,
                        line_count: FileLine = 1000) -> dict[str, object]:
        """Read bounded UTF-8/UTF-16 text with LF display, disk format and whole-file byte hash."""
        return await workers.file_call(selected_environment(), "file_read", root=root, path=path,
                                       start_line=start_line, line_count=line_count)

    @mcp.tool()
    async def file_write(root: str, path: str, content: str,
                         expected_sha256: str | None, encoding: Encoding | None = None,
                         bom: Annotated[bool, Field(strict=True)] | None = None, newline: Newline | None = None) -> dict[str, object]:
        """Create with a null hash or replace the version read. Preserve format; new files use UTF-8/LF.

        Mixed line endings require explicit newline. Binary replacement uses file_write_bytes.
        """
        return await workers.file_call(selected_environment(), "file_write", root=root, path=path,
                                       content=content, expected_sha256=expected_sha256, encoding=encoding, bom=bom, newline=newline)

    @mcp.tool()
    async def file_patch(root: str, path: str, expected_sha256: str,
                         edits: list[FileEdit], newline: Newline | None = None) -> dict[str, object]:
        """Replace unique original spans together if the whole-file hash still matches."""
        return await workers.file_call(selected_environment(), "file_patch", root=root, path=path,
                                       expected_sha256=expected_sha256,
                                       edits=[edit.model_dump() for edit in edits], newline=newline)

    @mcp.tool(annotations=ToolAnnotations(read_only_hint=True, destructive_hint=False,
                                           idempotent_hint=True, open_world_hint=False))
    async def file_read_bytes(root: str, path: str,
                              offset: Annotated[int, Field(strict=True, ge=0)] = 0,
                              length: Annotated[int, Field(strict=True, ge=1, le=65536)] = 16384) -> dict[str, object]:
        """Read a bounded base64 byte range and the whole-file hash. File/root policy still applies."""
        return await workers.file_call(selected_environment(), "file_read_bytes", root=root, path=path,
                                       offset=offset, length=length)

    @mcp.tool()
    async def file_write_bytes(root: str, path: str, data: Annotated[str, Field(max_length=87384)],
                               expected_sha256: str | None,
                               offset: Annotated[int, Field(strict=True, ge=0)] | None = None) -> dict[str, object]:
        """Write at most 64 KiB of base64-decoded bytes if the hash matches; null hash creates.

        Null offset replaces the whole file. A byte offset overwrites a range and may
        extend at the end, but cannot leave a gap. File/root policy still applies.
        """
        return await workers.file_call(selected_environment(), "file_write_bytes", root=root, path=path,
                                       data=data, expected_sha256=expected_sha256, offset=offset)

    @mcp.tool(annotations=ToolAnnotations(read_only_hint=True, destructive_hint=False,
                                           idempotent_hint=True, open_world_hint=False))
    async def file_list(root: str, path: str = ".", limit: FileLimit = 100) -> dict[str, object]:
        """List immediate visible entries within an explicitly granted file root."""
        return await workers.file_call(selected_environment(), "file_list", root=root, path=path,
                                       limit=limit)

    @mcp.tool(annotations=ToolAnnotations(read_only_hint=True, destructive_hint=False,
                                           idempotent_hint=True, open_world_hint=False))
    async def file_search(root: str, query: str, path: str = ".",
                          limit: SearchLimit = 100) -> dict[str, object]:
        """Find literal case-sensitive text within bounded visible files."""
        return await workers.file_call(selected_environment(), "file_search", root=root,
                                       query=query, path=path, limit=limit)
