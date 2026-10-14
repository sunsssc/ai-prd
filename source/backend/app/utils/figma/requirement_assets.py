from __future__ import annotations

import logging
import os
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable
from urllib.parse import parse_qs, unquote, urlparse
from uuid import uuid4

import requests

from app.utils.clickup.markdown_image_paths import build_relative_image_reference

logger = logging.getLogger(__name__)

FIGMA_API_BASE_URL = "https://api.figma.com/v1"
FIGMA_IMAGE_BATCH_SIZE = 10
FIGMA_NODE_BATCH_SIZE = 80
FIGMA_FALLBACK_NODE_LIMIT = 12
FIGMA_EXPORTABLE_NODE_TYPES = {"FRAME", "COMPONENT", "COMPONENT_SET", "SECTION", "INSTANCE"}
FIGMA_SECTION_START = "<!-- ai-prd-figma-assets:start -->"
FIGMA_SECTION_END = "<!-- ai-prd-figma-assets:end -->"

_FIGMA_URL_PATTERN = re.compile(
    r"https://(?:www\.)?figma\.com/(?:design|file|proto|board)/[A-Za-z0-9_-]+(?:/[^\s<>\"'\[\]()]*)?",
    re.IGNORECASE,
)
_GENERATED_SECTION_PATTERN = re.compile(
    rf"\n*{re.escape(FIGMA_SECTION_START)}.*?{re.escape(FIGMA_SECTION_END)}\s*$",
    re.DOTALL,
)


@dataclass(frozen=True)
class FigmaReference:
    url: str
    file_key: str
    node_id: str | None


@dataclass(frozen=True)
class FigmaAssetSyncResult:
    content: str
    downloaded: int = 0
    errors: int = 0


@dataclass(frozen=True)
class _SyncedAsset:
    reference: FigmaReference
    node_id: str
    node_name: str
    image_reference: str


class FigmaClient:
    def __init__(
        self,
        token: str,
        *,
        base_url: str = FIGMA_API_BASE_URL,
        request_interval_seconds: float = 1.0,
        max_retries: int = 4,
        retry_base_delay_seconds: float = 2.0,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._headers = {"X-Figma-Token": token}
        self._request_interval_seconds = max(0.0, request_interval_seconds)
        self._max_retries = max(0, max_retries)
        self._retry_base_delay_seconds = max(0.0, retry_base_delay_seconds)
        self._last_request_at: float | None = None

    def get_file(self, file_key: str, *, depth: int = 2) -> dict:
        return self._get_json(f"/files/{file_key}", params={"depth": str(depth)})

    def get_nodes(self, file_key: str, node_ids: list[str]) -> dict[str, dict]:
        result: dict[str, dict] = {}
        for batch in _chunked(node_ids, FIGMA_NODE_BATCH_SIZE):
            payload = self._get_json(f"/files/{file_key}/nodes", params={"ids": ",".join(batch)})
            nodes = payload.get("nodes")
            if isinstance(nodes, dict):
                result.update({str(key): value for key, value in nodes.items() if isinstance(value, dict)})
        return result

    def get_image_urls(self, file_key: str, node_ids: list[str]) -> dict[str, str]:
        result: dict[str, str] = {}
        for batch in _chunked(node_ids, FIGMA_IMAGE_BATCH_SIZE):
            result.update(self._get_image_urls_batch(file_key, batch))
        return result

    def _get_image_urls_batch(self, file_key: str, node_ids: list[str]) -> dict[str, str]:
        try:
            payload = self._get_json(
                f"/images/{file_key}",
                params={"ids": ",".join(node_ids), "format": "png", "scale": "1"},
            )
        except RuntimeError as exc:
            if len(node_ids) <= 1 or "Render timeout" not in str(exc):
                raise
            midpoint = len(node_ids) // 2
            return {
                **self._get_image_urls_batch(file_key, node_ids[:midpoint]),
                **self._get_image_urls_batch(file_key, node_ids[midpoint:]),
            }
        images = payload.get("images")
        if not isinstance(images, dict):
            return {}
        return {str(key): value for key, value in images.items() if isinstance(value, str) and value}

    def download_image(self, url: str) -> bytes:
        response = self._request("GET", url, headers=None, params=None)
        if response.status_code >= 400:
            raise RuntimeError(f"下载 Figma 图片失败: HTTP {response.status_code}")
        return response.content

    def _get_json(self, path: str, *, params: dict[str, str]) -> dict:
        response = self._request("GET", f"{self._base_url}{path}", headers=self._headers, params=params)
        if response.status_code >= 400:
            raise RuntimeError(f"Figma API 请求失败: HTTP {response.status_code} {response.text[:300].strip()}")
        payload = response.json()
        if not isinstance(payload, dict):
            raise RuntimeError("Figma API 返回了非 JSON 对象。")
        return payload

    def _request(
        self,
        method: str,
        url: str,
        *,
        headers: dict[str, str] | None,
        params: dict[str, str] | None,
    ) -> requests.Response:
        last_response: requests.Response | None = None
        last_error: requests.RequestException | None = None
        for attempt in range(self._max_retries + 1):
            self._wait_before_request()
            try:
                response = requests.request(method, url, headers=headers, params=params, timeout=90)
            except requests.RequestException as exc:
                last_error = exc
                if attempt >= self._max_retries:
                    break
                time.sleep(self._retry_base_delay_seconds * (2**attempt))
                continue

            last_response = response
            if response.status_code != 429 and response.status_code < 500:
                return response
            if attempt >= self._max_retries:
                return response
            retry_after = _retry_after_seconds(response.headers.get("Retry-After"))
            time.sleep(retry_after if retry_after is not None else self._retry_base_delay_seconds * (2**attempt))

        if last_response is not None:
            return last_response
        raise RuntimeError(f"Figma API 请求失败: {last_error}") from last_error

    def _wait_before_request(self) -> None:
        now = time.monotonic()
        if self._last_request_at is not None and self._request_interval_seconds > 0:
            wait_seconds = self._request_interval_seconds - (now - self._last_request_at)
            if wait_seconds > 0:
                time.sleep(wait_seconds)
                now = time.monotonic()
        self._last_request_at = now


def extract_figma_references(content: str) -> list[FigmaReference]:
    references: list[FigmaReference] = []
    seen: set[tuple[str, str | None]] = set()
    for match in _FIGMA_URL_PATTERN.finditer(_strip_generated_section(content)):
        url = match.group(0).rstrip(".,;:!?)）】]")
        reference = _parse_figma_reference(url)
        if reference is None:
            continue
        key = (reference.file_key, reference.node_id)
        if key in seen:
            continue
        seen.add(key)
        references.append(reference)
    return references


def sync_requirement_figma_assets(
    content: str,
    *,
    images_dir: Path,
    markdown_dir: Path,
    token: str,
    request_interval_seconds: float = 1.0,
    max_retries: int = 4,
    retry_base_delay_seconds: float = 2.0,
    client: FigmaClient | None = None,
) -> FigmaAssetSyncResult:
    base_content = _strip_generated_section(content)
    references = extract_figma_references(base_content)
    if not references:
        return FigmaAssetSyncResult(content=base_content)
    if not token and client is None:
        logger.warning("需求包含 Figma 链接，但未配置 FIGMA_ACCESS_TOKEN")
        return FigmaAssetSyncResult(content=base_content, errors=len(references))

    resolved_client = client or FigmaClient(
        token,
        request_interval_seconds=request_interval_seconds,
        max_retries=max_retries,
        retry_base_delay_seconds=retry_base_delay_seconds,
    )
    figma_images_dir = images_dir / "figma"
    figma_images_dir.mkdir(parents=True, exist_ok=True)

    assets: list[_SyncedAsset] = []
    errors = 0
    references_by_file: dict[str, list[FigmaReference]] = {}
    for reference in references:
        references_by_file.setdefault(reference.file_key, []).append(reference)

    for file_key, file_references in references_by_file.items():
        try:
            node_references = _resolve_node_references(resolved_client, file_key, file_references)
            node_ids = list(node_references)
            node_payloads = resolved_client.get_nodes(file_key, node_ids) if node_ids else {}
            image_urls = resolved_client.get_image_urls(file_key, node_ids) if node_ids else {}
        except Exception:
            errors += len(file_references)
            logger.warning("Figma 设计稿读取失败: file_key=%s", file_key, exc_info=True)
            continue

        node_by_id = _index_node_payloads(node_payloads)
        render_references: dict[str, FigmaReference] = {
            node_id: reference
            for node_id, reference in node_references.items()
            if node_id in image_urls
        }
        for node_id, reference in node_references.items():
            if node_id in image_urls:
                continue
            fallback_ids = _collect_fallback_node_ids(node_by_id.get(node_id, {}))
            try:
                fallback_urls = resolved_client.get_image_urls(file_key, fallback_ids) if fallback_ids else {}
            except Exception:
                fallback_urls = {}
                logger.warning(
                    "Figma 子节点渲染失败: file_key=%s node_id=%s",
                    file_key,
                    node_id,
                    exc_info=True,
                )
            for fallback_id, fallback_url in fallback_urls.items():
                image_urls[fallback_id] = fallback_url
                render_references[fallback_id] = reference
            if not fallback_urls:
                errors += 1
                logger.warning("Figma 节点及其子节点均未返回可下载图片: file_key=%s node_id=%s", file_key, node_id)

        for node_id, reference in render_references.items():
            image_url = image_urls.get(node_id)
            try:
                target_path = figma_images_dir / _image_filename(file_key, node_id)
                _atomic_write_bytes(target_path, resolved_client.download_image(image_url))
                node_document = node_by_id.get(node_id, {})
                node_name = str(node_document.get("name") or node_id)
                assets.append(
                    _SyncedAsset(
                        reference=reference,
                        node_id=node_id,
                        node_name=node_name,
                        image_reference=build_relative_image_reference(markdown_dir, target_path),
                    )
                )
            except Exception:
                errors += 1
                logger.warning(
                    "Figma 设计稿下载失败: file_key=%s node_id=%s",
                    file_key,
                    node_id,
                    exc_info=True,
                )

    if not assets:
        return FigmaAssetSyncResult(content=base_content, errors=errors)
    section = _render_assets_section(assets)
    return FigmaAssetSyncResult(
        content=f"{base_content.rstrip()}\n\n{section}\n",
        downloaded=len(assets),
        errors=errors,
    )


def _parse_figma_reference(url: str) -> FigmaReference | None:
    parsed = urlparse(url)
    if parsed.netloc.lower() not in {"figma.com", "www.figma.com"}:
        return None
    parts = [part for part in parsed.path.split("/") if part]
    if len(parts) < 2 or parts[0].lower() not in {"design", "file", "proto", "board"}:
        return None
    file_key = parts[1]
    if not re.fullmatch(r"[A-Za-z0-9_-]{8,}", file_key):
        return None
    query = parse_qs(parsed.query)
    raw_node_id = (query.get("node-id") or query.get("node_id") or [""])[0]
    node_id = _normalize_node_id(raw_node_id) if raw_node_id else None
    return FigmaReference(url=url, file_key=file_key, node_id=node_id)


def _normalize_node_id(value: str) -> str:
    decoded = unquote(value).strip()
    if ":" not in decoded and re.fullmatch(r"\d+(?:-\d+)+", decoded):
        return decoded.replace("-", ":")
    return decoded


def _resolve_node_references(
    client: FigmaClient,
    file_key: str,
    references: list[FigmaReference],
) -> dict[str, FigmaReference]:
    result = {
        reference.node_id: reference
        for reference in references
        if reference.node_id
    }
    file_reference = next((reference for reference in references if reference.node_id is None), None)
    if file_reference is None:
        return result

    payload = client.get_file(file_key, depth=2)
    document = payload.get("document")
    pages = document.get("children") if isinstance(document, dict) else None
    if not isinstance(pages, list):
        return result
    for page in pages:
        children = page.get("children") if isinstance(page, dict) else None
        if not isinstance(children, list):
            continue
        for node in children:
            if not isinstance(node, dict) or node.get("type") not in FIGMA_EXPORTABLE_NODE_TYPES:
                continue
            node_id = str(node.get("id") or "")
            if node_id:
                result.setdefault(node_id, file_reference)
    return result


def _index_node_payloads(node_payloads: dict[str, dict]) -> dict[str, dict]:
    result: dict[str, dict] = {}
    for payload in node_payloads.values():
        document = payload.get("document") if isinstance(payload, dict) else None
        if not isinstance(document, dict):
            continue
        stack = [document]
        while stack:
            node = stack.pop()
            node_id = str(node.get("id") or "")
            if node_id:
                result[node_id] = node
            children = node.get("children")
            if isinstance(children, list):
                stack.extend(child for child in reversed(children) if isinstance(child, dict))
    return result


def _collect_fallback_node_ids(root: dict) -> list[str]:
    result: list[str] = []
    children = root.get("children") if isinstance(root, dict) else None
    stack = [child for child in reversed(children) if isinstance(child, dict)] if isinstance(children, list) else []
    while stack and len(result) < FIGMA_FALLBACK_NODE_LIMIT:
        node = stack.pop()
        node_id = str(node.get("id") or "")
        if node_id and node.get("type") in FIGMA_EXPORTABLE_NODE_TYPES:
            result.append(node_id)
            continue
        node_children = node.get("children")
        if isinstance(node_children, list):
            stack.extend(child for child in reversed(node_children) if isinstance(child, dict))
    return result


def _render_assets_section(assets: list[_SyncedAsset]) -> str:
    lines = [FIGMA_SECTION_START, "## Figma 设计稿（本地同步）", ""]
    for asset in assets:
        label = _escape_markdown(asset.node_name)
        lines.extend(
            [
                f"### {label}",
                "",
                f"- [在 Figma 中查看]({asset.reference.url})",
                f"- Node ID: `{asset.node_id}`",
                "",
                f"![{label}]({asset.image_reference})",
                "",
            ]
        )
    lines.append(FIGMA_SECTION_END)
    return "\n".join(lines)


def _strip_generated_section(content: str) -> str:
    match = _GENERATED_SECTION_PATTERN.search(content)
    if match is None:
        return content
    return content[: match.start()].rstrip() + "\n"


def _image_filename(file_key: str, node_id: str) -> str:
    safe_node_id = re.sub(r"[^A-Za-z0-9_-]+", "_", node_id).strip("_") or "node"
    return f"{file_key}_{safe_node_id}.png"


def _atomic_write_bytes(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    try:
        temporary_path.write_bytes(content)
        os.replace(temporary_path, path)
    finally:
        temporary_path.unlink(missing_ok=True)


def _retry_after_seconds(value: str | None) -> float | None:
    if not value:
        return None
    try:
        return max(0.0, float(value))
    except ValueError:
        return None


def _escape_markdown(value: str) -> str:
    return value.replace("[", "\\[").replace("]", "\\]")


def _chunked(items: list[str], size: int) -> Iterable[list[str]]:
    for index in range(0, len(items), size):
        yield items[index : index + size]
