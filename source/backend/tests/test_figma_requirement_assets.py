from __future__ import annotations

from pathlib import Path

from app.utils.figma.requirement_assets import (
    FIGMA_SECTION_START,
    FigmaReference,
    extract_figma_references,
    sync_requirement_figma_assets,
)


class FakeFigmaClient:
    def __init__(self) -> None:
        self.file_requests: list[str] = []
        self.node_requests: list[tuple[str, tuple[str, ...]]] = []
        self.image_requests: list[tuple[str, tuple[str, ...]]] = []

    def get_file(self, file_key: str, *, depth: int = 2) -> dict:
        self.file_requests.append(file_key)
        assert depth == 2
        return {
            "document": {
                "children": [
                    {
                        "type": "CANVAS",
                        "children": [
                            {"id": "1:2", "name": "首页", "type": "FRAME"},
                            {"id": "1:3", "name": "说明", "type": "TEXT"},
                        ],
                    }
                ]
            }
        }

    def get_nodes(self, file_key: str, node_ids: list[str]) -> dict[str, dict]:
        self.node_requests.append((file_key, tuple(node_ids)))
        return {
            node_id: {"document": {"id": node_id, "name": f"节点 {node_id}"}}
            for node_id in node_ids
        }

    def get_image_urls(self, file_key: str, node_ids: list[str]) -> dict[str, str]:
        self.image_requests.append((file_key, tuple(node_ids)))
        return {node_id: f"https://images.example/{node_id}.png" for node_id in node_ids}

    def download_image(self, url: str) -> bytes:
        return f"png:{url}".encode()


class FakeContainerFigmaClient(FakeFigmaClient):
    def get_nodes(self, file_key: str, node_ids: list[str]) -> dict[str, dict]:
        del file_key
        return {
            node_ids[0]: {
                "document": {
                    "id": node_ids[0],
                    "name": "需求容器",
                    "type": "GROUP",
                    "children": [
                        {"id": "9:1", "name": "页面一", "type": "FRAME"},
                        {"id": "9:2", "name": "说明", "type": "TEXT"},
                    ],
                }
            }
        }

    def get_image_urls(self, file_key: str, node_ids: list[str]) -> dict[str, str]:
        del file_key
        return {
            node_id: f"https://images.example/{node_id}.png"
            for node_id in node_ids
            if node_id == "9:1"
        }


def test_extract_figma_references_reads_clickup_markdown_links_and_deduplicates() -> None:
    content = (
        "[设计稿](https://www.figma.com/design/FileKey12345/Page?node-id=123-456&t=abc)\n"
        "重复：https://www.figma.com/design/FileKey12345/Page?node-id=123-456&t=other\n"
        "白板：https://www.figma.com/board/BoardKey12345/Flow"
    )

    references = extract_figma_references(content)

    assert [(item.file_key, item.node_id) for item in references] == [
        ("FileKey12345", "123:456"),
        ("BoardKey12345", None),
    ]


def test_extract_figma_references_stops_before_adjacent_markdown_link() -> None:
    url = "https://www.figma.com/design/FileKey12345/Page?node-id=123-456&t=abc"

    references = extract_figma_references(f"[设计稿]({url}]({url})")

    assert references == [FigmaReference(url=url, file_key="FileKey12345", node_id="123:456")]


def test_sync_requirement_figma_assets_downloads_linked_node_and_replaces_generated_section(
    tmp_path: Path,
) -> None:
    client = FakeFigmaClient()
    content = "# 需求\n\n设计稿：https://www.figma.com/design/FileKey12345/Page?node-id=123-456"

    first = sync_requirement_figma_assets(
        content,
        images_dir=tmp_path / "images",
        markdown_dir=tmp_path,
        token="token",
        client=client,
    )
    second = sync_requirement_figma_assets(
        first.content,
        images_dir=tmp_path / "images",
        markdown_dir=tmp_path,
        token="token",
        client=client,
    )

    assert first.downloaded == 1
    assert first.errors == 0
    assert first.content.count(FIGMA_SECTION_START) == 1
    assert second.content.count(FIGMA_SECTION_START) == 1
    assert "节点 123:456" in second.content
    assert "images/figma/FileKey12345_123_456.png" in second.content
    assert (tmp_path / "images/figma/FileKey12345_123_456.png").read_bytes().startswith(b"png:")
    assert client.file_requests == []


def test_sync_requirement_figma_assets_exports_top_level_frames_for_file_link(tmp_path: Path) -> None:
    client = FakeFigmaClient()
    result = sync_requirement_figma_assets(
        "设计稿：https://www.figma.com/file/FileKey12345/Page",
        images_dir=tmp_path / "images",
        markdown_dir=tmp_path,
        token="token",
        client=client,
    )

    assert result.downloaded == 1
    assert result.errors == 0
    assert client.file_requests == ["FileKey12345"]
    assert client.image_requests == [("FileKey12345", ("1:2",))]
    assert "节点 1:2" in result.content


def test_sync_requirement_figma_assets_falls_back_to_renderable_child(tmp_path: Path) -> None:
    result = sync_requirement_figma_assets(
        "设计稿：https://www.figma.com/design/FileKey12345/Page?node-id=8-1",
        images_dir=tmp_path / "images",
        markdown_dir=tmp_path,
        token="token",
        client=FakeContainerFigmaClient(),
    )

    assert result.downloaded == 1
    assert result.errors == 0
    assert "页面一" in result.content
    assert "FileKey12345_9_1.png" in result.content


def test_sync_requirement_figma_assets_reports_missing_token_without_downloading(tmp_path: Path) -> None:
    content = "设计稿：https://www.figma.com/design/FileKey12345/Page?node-id=1-2"

    result = sync_requirement_figma_assets(
        content,
        images_dir=tmp_path / "images",
        markdown_dir=tmp_path,
        token="",
    )

    assert result.content == content
    assert result.downloaded == 0
    assert result.errors == 1
    assert not (tmp_path / "images").exists()
