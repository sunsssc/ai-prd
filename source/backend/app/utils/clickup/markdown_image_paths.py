from __future__ import annotations

import os
import re
from pathlib import Path


_MARKDOWN_IMAGE_PATTERN = re.compile(
    r"!\[(?P<alt>[^\]]*)\]\((?P<target>[^\s)]+)(?P<suffix>[^)]*)\)"
)


def build_relative_image_reference(markdown_dir: Path, image_path: Path) -> str:
    """返回从 markdown 所在目录到图片文件的相对路径。"""
    return Path(os.path.relpath(image_path, start=markdown_dir)).as_posix()


def rewrite_shared_images_references(
    markdown_text: str,
    *,
    markdown_file: Path,
    shared_images_dir: Path,
) -> tuple[str, int]:
    """
    将 markdown 中形如 images/<name> 的图片引用，改写为相对当前文件的正确路径。

    仅处理共享图片目录场景下的本地图片引用：
    - images/<name>
    - ./images/<name>
    """
    markdown_dir = markdown_file.parent
    replacements = 0

    def _replace(match: re.Match[str]) -> str:
        nonlocal replacements
        target = match.group("target")
        normalized_target = target.removeprefix("./")
        if not normalized_target.startswith("images/"):
            return match.group(0)

        image_name = normalized_target.removeprefix("images/")
        if not image_name:
            return match.group(0)

        relative_target = build_relative_image_reference(markdown_dir, shared_images_dir / image_name)
        if relative_target == target:
            return match.group(0)

        replacements += 1
        return f"![{match.group('alt')}]({relative_target}{match.group('suffix')})"

    return _MARKDOWN_IMAGE_PATTERN.sub(_replace, markdown_text), replacements
