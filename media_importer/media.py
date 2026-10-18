"""Small media-scope policy shared by import, cleanup and presentation."""
import json
from pathlib import PurePosixPath

from .model import Asset
from .adapters import source_media_category


MEDIA_SCOPES = {"all": "全部素材", "video": "仅视频", "photo": "仅照片"}
VIDEO_EXTENSIONS = {".mp4", ".mov"}
PHOTO_EXTENSIONS = {".jpg", ".jpeg", ".dng", ".raw", ".png", ".heic", ".heif"}


def category(asset):
    specific = source_media_category(asset)
    if specific is not None:
        return specific
    extension = PurePosixPath(asset.path).suffix.lower()
    return "video" if extension in VIDEO_EXTENSIONS else "photo" if extension in PHOTO_EXTENSIONS else "other"


def group_category(members):
    # The adapter's association boundary is preserved. Unknown sidecars follow
    # a recognized member; conflicting media kinds are never guessed.
    kinds = {category(asset) for asset in members} - {"other"}
    return kinds.pop() if len(kinds) == 1 else "other"


def select_groups(groups, scope):
    return [group for group in groups if scope == "all" or group_category(group["members"]) == scope]


def selected_file_ids(rows, scope, *, include_excluded=False):
    if scope == "all":
        return {row["id"] for row in rows if include_excluded or row['state'] != 'excluded'}
    groups = {}
    for row in rows:
        groups.setdefault(row.get("group_id") or row["id"], []).append(row)
    return {row["id"] for members in groups.values()
            if group_category([Asset.from_dict(json.loads(row["asset"])) for row in members]) == scope
            for row in members if include_excluded or row['state'] != 'excluded'}
