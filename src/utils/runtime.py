"""
运行环境检测工具

检测 agent 是否运行在 Docker 容器内，并提供运行时信息。
"""

import os
import shutil
import logging
from typing import Dict, Any

logger = logging.getLogger(__name__)

_docker_cache: bool | None = None


def is_docker() -> bool:
    """
    检测当前进程是否运行在 Docker 容器内。

    检测顺序：
    1. 环境变量 DOCKER_CONTAINER=1（我们自己在 Dockerfile 中设置）
    2. /.dockerenv 文件存在（Docker 自动创建）
    3. /proc/1/cgroup 中包含 docker 字样（Linux cgroup 检测）
    """
    global _docker_cache
    if _docker_cache is not None:
        return _docker_cache

    # 方法 1: 显式环境变量（最可靠，我们自己控制）
    if os.environ.get('DOCKER_CONTAINER') == '1':
        _docker_cache = True
        return True

    # 方法 2: Docker 自动创建的标记文件
    if os.path.exists('/.dockerenv'):
        _docker_cache = True
        return True

    # 方法 3: Linux cgroup 检测
    try:
        with open('/proc/1/cgroup', 'r') as f:
            content = f.read()
            if 'docker' in content or 'containerd' in content:
                _docker_cache = True
                return True
    except (FileNotFoundError, PermissionError):
        pass

    _docker_cache = False
    return False


def get_runtime_info() -> Dict[str, Any]:
    """
    返回当前运行环境的详细信息。
    用于日志、调试和能力检测。
    """
    in_docker = is_docker()

    info: Dict[str, Any] = {
        'is_docker': in_docker,
        'python_version': _get_python_version(),
        'platform': os.name,
    }

    if in_docker:
        info.update({
            'workspace': os.environ.get('WORKSPACE_DIR', '/workspace'),
            'has_bash': shutil.which('bash') is not None,
            'has_curl': shutil.which('curl') is not None,
            'has_playwright': _check_playwright(),
            'sandbox_strict': False,
        })
    else:
        info.update({
            'workspace': os.getcwd(),
            'has_bash': shutil.which('bash') is not None,
            'has_curl': shutil.which('curl') is not None,
            'has_playwright': _check_playwright(),
            'sandbox_strict': True,
        })

    return info


def _get_python_version() -> str:
    import sys
    return f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"


def _check_playwright() -> bool:
    """检测 Playwright 是否可用"""
    try:
        import playwright  # noqa: F401
        return True
    except ImportError:
        return False
