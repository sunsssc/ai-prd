from __future__ import annotations

import importlib.util
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[3]
MODULE_PATH = REPO_ROOT / "source/scripts/clickup/markdown_image_paths.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("markdown_image_paths", MODULE_PATH)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_build_relative_image_reference_uses_markdown_directory() -> None:
    module = _load_module()

    markdown_dir = REPO_ROOT / "workspace/knowledge/requirements/docs/平台机制"
    image_path = REPO_ROOT / "workspace/knowledge/requirements/docs/images/demo.png"

    result = module.build_relative_image_reference(markdown_dir, image_path)

    assert result == "../images/demo.png"


def test_rewrite_shared_images_references_only_rewrites_shared_images_targets() -> None:
    module = _load_module()

    markdown_file = REPO_ROOT / "workspace/knowledge/requirements/docs/平台机制/经纪商返佣.md"
    shared_images_dir = REPO_ROOT / "workspace/knowledge/requirements/docs/images"
    content = "\n".join(
        [
            "封面图：![](images/demo.png)",
            "外链图：![](https://example.com/demo.png)",
            "已经正确：![](../images/already-ok.png)",
        ]
    )

    rewritten, replacements = module.rewrite_shared_images_references(
        content,
        markdown_file=markdown_file,
        shared_images_dir=shared_images_dir,
    )

    assert replacements == 1
    assert "![](../images/demo.png)" in rewritten
    assert "![](https://example.com/demo.png)" in rewritten
    assert "![](../images/already-ok.png)" in rewritten
