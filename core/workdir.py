"""工作目录管理模块 - 管理工作目录的创建和操作"""
from __future__ import annotations

import os
import subprocess
import re
from pathlib import Path

from core.config import WORKDIR_ROOT
from core.utils import _sanitize_auto_dir_name


def normalize_workdir(workdir: str) -> Path:
    """规范化工作目录路径"""
    p = Path(str(workdir or "")).expanduser()
    if not p.is_absolute():
        p = (WORKDIR_ROOT / p).resolve()
    else:
        p = p.resolve()
    return p


def workdir_base(cwd: str) -> str:
    """获取工作目录的basename"""
    p = Path(str(cwd or "")).name
    return p if p else str(cwd or "")


def destination_from_parent(parent_dir: str, new_dir_name: str) -> Path:
    """从父目录和目标名称生成目标路径"""
    parent = Path(str(parent_dir or "")).expanduser().resolve()
    name = str(new_dir_name or "").strip()
    name = re.sub(r"[^\w\-_]", "_", name)
    name = name.strip("_")
    if not name:
        name = "untitled"
    return parent / name


def ensure_github_repo_url(url: str) -> str:
    """确保是GitHub仓库URL"""
    s = str(url or "").strip()
    if not s:
        return ""
    if "/" in s and not s.startswith("http") and not s.startswith("git@"):
        parts = s.split("/")
        if len(parts) == 2:
            s = f"https://github.com/{s}"
    if s.startswith("https://") or s.startswith("git@"):
        return s
    return ""


def create_workdir_by_clone(parent_dir: str, repo_url: str, new_dir_name: str) -> Path:
    """通过克隆创建工作目录"""
    dest = destination_from_parent(parent_dir, new_dir_name)
    if dest.exists():
        return dest
    url = ensure_github_repo_url(repo_url)
    if not url:
        raise ValueError(f"Invalid repo URL: {repo_url}")
    parent = Path(parent_dir).expanduser().resolve()
    parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["git", "clone", url, str(dest.name)],
        cwd=str(parent),
        check=True,
        capture_output=True,
        timeout=120,
    )
    return dest


def create_workdir_by_worktree(source_dir: str, branch_name: str, new_dir_name: str) -> Path:
    """通过worktree创建工作目录"""
    source = Path(source_dir).expanduser().resolve()
    dest = destination_from_parent(source.parent, new_dir_name)
    if dest.exists():
        return dest
    parent = dest.parent
    parent.mkdir(parents=True, exist_ok=True)
    safe_branch = re.sub(r"[^\w\-_]", "_", str(branch_name or "").strip())
    subprocess.run(
        ["git", "worktree", "add", "-b", safe_branch, str(dest)],
        cwd=str(source),
        check=True,
        capture_output=True,
        timeout=60,
    )
    return dest


def create_workdir_by_copy(source_dir: str, new_dir_name: str | None = None) -> Path:
    """通过复制创建工作目录"""
    source = Path(source_dir).expanduser().resolve()
    name = str(new_dir_name or "").strip()
    if not name:
        name = f"{source.name}_copy"
    dest = destination_from_parent(source.parent, name)
    if dest.exists():
        return dest
    import shutil
    shutil.copytree(source, dest, ignore=shutil.ignore_patterns(".git", "__pycache__", "*.pyc", ".pytest_cache"))
    return dest


def relative_workdir(path: Path) -> str:
    """返回相对于WORKDIR_ROOT的路径"""
    try:
        rel = path.relative_to(WORKDIR_ROOT)
        return str(rel)
    except Exception:
        return str(path)


def list_workdir_children(workdir: str | None, allow_outside_root: bool = False) -> dict:
    """列出工作目录子目录（兼容前端目录选择器返回结构）"""
    if allow_outside_root:
        base_value = str(workdir or "/")
        current = Path(base_value).expanduser().resolve()
        if not current.exists():
            raise ValueError(f"workdir does not exist: {current}")
        if not current.is_dir():
            raise ValueError(f"workdir is not a directory: {current}")
    else:
        current = normalize_workdir(workdir or str(WORKDIR_ROOT))
        if not current.exists():
            raise ValueError(f"workdir does not exist: {current}")
        if not current.is_dir():
            raise ValueError(f"workdir is not a directory: {current}")

    children: list[dict[str, str]] = []
    for child in sorted(current.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower())):
        if not child.is_dir() or child.name.startswith('.'):
            continue
        children.append({
            "name": child.name,
            "path": str(child),
            "relative_path": relative_workdir(child),
        })

    if allow_outside_root:
        is_root = current.parent == current
    else:
        is_root = current == WORKDIR_ROOT

    parent_path = None if is_root else str(current.parent)
    if is_root:
        parent_relative_path = None
    elif allow_outside_root:
        parent_relative_path = str(current.parent)
    else:
        parent_relative_path = relative_workdir(current.parent)

    root_value = "/" if allow_outside_root else str(WORKDIR_ROOT)
    current_relative = str(current) if allow_outside_root else relative_workdir(current)

    return {
        "root": root_value,
        "current": str(current),
        "current_relative": current_relative,
        "parent": parent_path,
        "parent_relative": parent_relative_path,
        "children": children,
    }


def get_workdir_summary(path: Path) -> dict:
    """获取工作目录摘要信息"""
    return {
        "path": str(path),
        "name": path.name,
        "relative": relative_workdir(path),
        "exists": path.exists(),
        "is_dir": path.is_dir() if path.exists() else False,
    }
