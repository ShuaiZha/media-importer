"""The small structural contracts consumed by the transfer engine.

Adapters translate their native identity, telemetry, errors and file metadata here.
Discovery observations use type, serial_number, model_name, firmware_version,
url and compatibility (import_supported/delete_verified/validation/reason).
identify() verifies the configured source and returns its normalized identity;
the serial_number/model_name fields do not require a vendor's HTTP response.

Source modules provide defaults/validate/create/probe/discover. Discovery receives
identity/address hints and a cancellation event; address derivation and scanning
stay inside the adapter. Optional media_category(asset) overrides standard file
extensions with video/photo/other, or returns None to use the common fallback.

Optional fork(stop=None, *, purpose='transfer') creates an independent client.
The monitor and keep_alive purposes let the adapter bound its own timeout/retries.
Optional keep_alive requires a positive keep_alive_interval_seconds; telemetry
requires idle_reason(state). Optional deletion_effects(asset, members) returns
the verified member identities that a delete may affect; omission means only the
requested file. An adapter must not claim unsafe or unverified capabilities.
"""
from pathlib import Path
from typing import Callable, Dict, Iterable, List, Optional, Protocol, Set

from .model import Asset

Progress = Callable[[int, int], None]


class MediaSource(Protocol):
    def identify(self) -> dict: ...
    def snapshot(self) -> List[Asset]: ...
    def paths(self) -> Set[str]: ...
    def groups(self, assets: Iterable[Asset]) -> List[dict]: ...
    def download(self, asset: Asset, path: Path, progress: Progress) -> Optional[str]:
        """Write the whole file. Return its SHA-256 when the bytes were hashed on the way in."""
        ...
    def deletion_candidates(self, assets: Iterable[Asset]) -> Dict[str, Optional[str]]:
        """Map stable identity to None if deletable, otherwise an explanation."""
        ...
    def delete(self, asset: Asset) -> None: ...


class ArchiveDestination(Protocol):
    def check(self) -> dict: ...
    def stat(self, path: str) -> Optional[dict]:
        """Return {size: int, is_directory: bool}, or None if absent."""
        ...
    def list_names(self, directory: str) -> List[str]: ...
    def upload(self, path: Path, temporary: str, progress: Progress, created: Callable, staged: Callable) -> None: ...
    def file_identity(self, path: str) -> Optional[dict]: ...
    def delete_owned(self, path: str, identity: dict) -> None: ...
    def publish(self, temporary: str, final: str) -> None:
        """Publish without overwriting; raise NameConflict if final is occupied."""
        ...
    def close(self) -> None: ...
