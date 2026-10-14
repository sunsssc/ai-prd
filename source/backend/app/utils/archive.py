from __future__ import annotations

import shutil
import stat
import zipfile
from pathlib import Path, PurePosixPath


def normalize_zip_archive_entries(
    infos: list[zipfile.ZipInfo],
) -> tuple[list[tuple[zipfile.ZipInfo, PurePosixPath]], str | None]:
    """规范化 ZIP 条目，返回 (清洗后的条目列表, 单层包装目录名)。

    拒绝绝对路径、路径穿越、符号链接、加密 ZIP 与重复路径；剔除 __MACOSX 与 .DS_Store；
    若所有条目都在同一个顶层目录下，则剥离该包装目录。
    """
    normalized: list[tuple[zipfile.ZipInfo, PurePosixPath]] = []
    seen_paths: set[str] = set()
    for info in infos:
        raw_name = info.filename.replace("\\", "/")
        path = PurePosixPath(raw_name)
        if not raw_name or raw_name.startswith("/") or path.is_absolute() or ".." in path.parts:
            raise FileNotFoundError("ZIP 中包含不安全的文件路径。")
        if info.flag_bits & 0x1:
            raise FileNotFoundError("不支持加密 ZIP。")
        unix_mode = info.external_attr >> 16
        if unix_mode and stat.S_ISLNK(unix_mode):
            raise FileNotFoundError("ZIP 中不能包含符号链接。")
        clean_parts = tuple(part for part in path.parts if part not in {"", "."})
        if not clean_parts or clean_parts[0] == "__MACOSX" or clean_parts[-1] == ".DS_Store":
            continue
        clean_path = PurePosixPath(*clean_parts)
        key = clean_path.as_posix().rstrip("/")
        if key in seen_paths:
            raise FileNotFoundError("ZIP 中包含重复路径。")
        seen_paths.add(key)
        normalized.append((info, clean_path))

    if not normalized or not any(not info.is_dir() for info, _ in normalized):
        raise FileNotFoundError("ZIP 中没有可用文件。")

    top_parts = {path.parts[0] for _, path in normalized}
    has_single_wrapper = len(top_parts) == 1 and all(len(path.parts) > 1 or info.is_dir() for info, path in normalized)
    wrapper_name = next(iter(top_parts)) if has_single_wrapper else None
    if has_single_wrapper:
        stripped: list[tuple[zipfile.ZipInfo, PurePosixPath]] = []
        for info, path in normalized:
            if len(path.parts) == 1:
                continue
            stripped.append((info, PurePosixPath(*path.parts[1:])))
        normalized = stripped

    return normalized, wrapper_name


def extract_zip_safely(*, archive_path: Path, dest_dir: Path, max_files: int, max_total_bytes: int) -> int:
    """安全解压 ZIP 到 dest_dir，返回解压出的文件数量。

    ZIP 条目声明的 file_size 不可信：解压时流式分块写盘并统计实际字节数，
    文件数或总字节数超限立即中止并清理已写入内容。
    """
    try:
        archive_file = zipfile.ZipFile(archive_path)
    except zipfile.BadZipFile as exc:
        raise ValueError("ZIP 文件损坏或格式不正确。") from exc

    with archive_file:
        entries, _ = normalize_zip_archive_entries(archive_file.infolist())
        file_count = sum(1 for info, _ in entries if not info.is_dir())
        if file_count > max_files:
            raise ValueError(f"ZIP 内文件数量超过上限（{max_files} 个）。")
        dest_dir.mkdir(parents=True, exist_ok=True)
        total_bytes = 0
        try:
            for info, relative_path in entries:
                target = dest_dir.joinpath(*relative_path.parts)
                if info.is_dir():
                    target.mkdir(parents=True, exist_ok=True)
                    continue
                target.parent.mkdir(parents=True, exist_ok=True)
                with archive_file.open(info) as source, target.open("wb") as destination:
                    while True:
                        chunk = source.read(1024 * 1024)
                        if not chunk:
                            break
                        total_bytes += len(chunk)
                        if total_bytes > max_total_bytes:
                            raise ValueError(f"ZIP 解压后的总大小超过上限（{max_total_bytes} 字节）。")
                        destination.write(chunk)
        except Exception:
            shutil.rmtree(dest_dir, ignore_errors=True)
            raise
    return file_count
