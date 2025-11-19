from dataclasses import dataclass
from typing import Optional


@dataclass(frozen=True)
class FileInfo:
    filename: str
    url: str
    hash: Optional[str] = None


@dataclass(frozen=True)
class Resource:
    resource_id: str
    parent_id: Optional[str]
    title: str
    category_id: Optional[int]
    version: int
    file: FileInfo
    is_watching: bool = False
    is_special: bool = False
    is_licensed: bool = False


@dataclass(frozen=True)
class DownloadTask:
    """Metadata needed to download a resource (with version tracking)."""
    resource_id: str
    title: Optional[str]
    category_id: int
    remote_version: int
    local_version: Optional[int]
    parent_resource_id: Optional[str]
    download_url: str
    filename: str
    file_hash: Optional[str]

    @property
    def needs_download(self) -> bool:
        """Check if this resource needs to be downloaded."""
        return self.local_version is None or self.local_version < self.remote_version
        
    @property
    def is_dependency(self) -> bool:
        """Check if this is a dependency (has a parent resource)."""
        return self.parent_resource_id is not None

