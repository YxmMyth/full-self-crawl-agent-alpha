"""
文件下载器 - 支持任意类型文件下载

核心原则：type 由 LLM 动态判断，不硬编码固定类型
"""

import os
import asyncio
import aiohttp
import hashlib
import logging
from typing import Dict, Any, Optional, Tuple
from pathlib import Path
from urllib.parse import urlparse, unquote
from datetime import datetime

logger = logging.getLogger('downloader')


class FileDownloader:
    """
    通用文件下载器

    支持任意类型文件下载，类型由 LLM 动态判断
    """

    def __init__(self, download_dir: str = './downloads'):
        """
        初始化下载器

        Args:
            download_dir: 下载目录
        """
        self.download_dir = Path(download_dir)
        self.download_dir.mkdir(parents=True, exist_ok=True)

        # 支持的文件扩展名映射（仅用于辅助，不限制）
        self.type_extensions = {
            'pdf': ['.pdf'],
            'image': ['.png', '.jpg', '.jpeg', '.gif', '.webp', '.svg', '.bmp'],
            'video': ['.mp4', '.mov', '.avi', '.mkv', '.webm'],
            'audio': ['.mp3', '.wav', '.flac', '.aac', '.ogg'],
            'zip': ['.zip', '.rar', '.7z', '.tar', '.gz'],
            'python': ['.py'],
            'javascript': ['.js', '.ts', '.jsx', '.tsx'],
            'code': ['.py', '.js', '.ts', '.java', '.cpp', '.c', '.go', '.rs'],
            'csv': ['.csv'],
            'json': ['.json'],
            'xml': ['.xml'],
            'html': ['.html', '.htm'],
            'text': ['.txt', '.md', '.rst'],
            'dataset': ['.csv', '.json', '.xlsx', '.parquet', '.h5'],
            'document': ['.doc', '.docx', '.pdf', '.txt'],
        }

    async def download(self, url: str, file_type: str, filename: str = None,
                       referer: str = None) -> Dict[str, Any]:
        """
        下载文件到本地

        Args:
            url: 文件 URL
            file_type: LLM 判断的类型（pdf/image/zip/video/python/csv/json...任意）
            filename: 可选的文件名
            referer: 来源页面 URL（用于防盗链）

        Returns:
            {
                "type": file_type,
                "name": filename,
                "content": local_path,
                "source": url,
                "size": file_size,
                "success": bool
            }
        """
        try:
            # 生成文件名
            if not filename:
                filename = self._generate_filename(url, file_type)

            # 确保文件名安全
            filename = self._sanitize_filename(filename)

            # 构建本地路径
            local_path = self.download_dir / filename

            # 下载文件
            success, size = await self._download_file(url, local_path, referer)

            if success:
                logger.info(f"下载成功: {filename} ({size} bytes)")
                return {
                    "type": file_type,
                    "name": filename,
                    "content": str(local_path.absolute()),
                    "source": url,
                    "size": size,
                    "success": True
                }
            else:
                return {
                    "type": file_type,
                    "name": filename,
                    "content": None,
                    "source": url,
                    "success": False,
                    "error": "下载失败"
                }

        except Exception as e:
            logger.error(f"下载异常: {url} - {e}")
            return {
                "type": file_type,
                "name": filename or "unknown",
                "content": None,
                "source": url,
                "success": False,
                "error": str(e)
            }

    async def _download_file(self, url: str, local_path: Path,
                             referer: str = None) -> Tuple[bool, int]:
        """
        通用文件下载

        Args:
            url: 文件 URL
            local_path: 本地保存路径
            referer: 来源页面 URL

        Returns:
            (是否成功, 文件大小)
        """
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
        }
        if referer:
            headers['Referer'] = referer

        timeout = aiohttp.ClientTimeout(total=120, connect=30)

        async with aiohttp.ClientSession(timeout=timeout, headers=headers) as session:
            async with session.get(url, allow_redirects=True) as response:
                if response.status == 200:
                    # 检查文件大小
                    content_length = response.headers.get('Content-Length')
                    if content_length and int(content_length) > 100 * 1024 * 1024:  # 100MB
                        logger.warning(f"文件过大: {content_length} bytes")

                    # 写入文件
                    with open(local_path, 'wb') as f:
                        total_size = 0
                        async for chunk in response.content.iter_chunked(8192):
                            f.write(chunk)
                            total_size += len(chunk)

                    return True, total_size
                else:
                    logger.warning(f"下载失败 HTTP {response.status}: {url}")
                    return False, 0

    def _generate_filename(self, url: str, file_type: str) -> str:
        """
        根据URL和类型生成文件名

        Args:
            url: 文件 URL
            file_type: LLM 判断的类型

        Returns:
            生成的文件名
        """
        # 尝试从 URL 提取文件名
        parsed = urlparse(url)
        path = unquote(parsed.path)

        # 获取原始文件名
        original_name = os.path.basename(path)

        # 如果有扩展名，直接使用
        if '.' in original_name:
            return original_name

        # 否则根据类型添加扩展名
        default_extensions = {
            'pdf': '.pdf',
            'image': '.png',
            'video': '.mp4',
            'audio': '.mp3',
            'zip': '.zip',
            'python': '.py',
            'javascript': '.js',
            'csv': '.csv',
            'json': '.json',
            'xml': '.xml',
            'html': '.html',
            'text': '.txt',
        }

        ext = default_extensions.get(file_type.lower(), '')

        # 使用 URL hash 作为文件名
        url_hash = hashlib.md5(url.encode()).hexdigest()[:8]
        timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')

        return f"download_{timestamp}_{url_hash}{ext}"

    def _sanitize_filename(self, filename: str) -> str:
        """
        清理文件名，移除不安全字符

        Args:
            filename: 原始文件名

        Returns:
            安全的文件名
        """
        # 移除不安全字符
        unsafe_chars = '<>:"/\\|?*'
        for char in unsafe_chars:
            filename = filename.replace(char, '_')

        # 限制长度
        if len(filename) > 200:
            name, ext = os.path.splitext(filename)
            filename = name[:200-len(ext)] + ext

        return filename

    def get_download_stats(self) -> Dict[str, Any]:
        """获取下载统计信息"""
        files = list(self.download_dir.glob('*'))
        total_size = sum(f.stat().st_size for f in files if f.is_file())

        return {
            'download_dir': str(self.download_dir),
            'file_count': len(files),
            'total_size': total_size,
            'total_size_mb': round(total_size / (1024 * 1024), 2)
        }

    def clear_downloads(self, older_than_days: int = None):
        """
        清理下载文件

        Args:
            older_than_days: 清理多少天前的文件，None 表示全部清理
        """
        if older_than_days is None:
            # 清理全部
            for f in self.download_dir.glob('*'):
                if f.is_file():
                    f.unlink()
            logger.info("已清理所有下载文件")
        else:
            # 清理指定天数前的文件
            import time
            cutoff = time.time() - older_than_days * 86400

            for f in self.download_dir.glob('*'):
                if f.is_file() and f.stat().st_mtime < cutoff:
                    f.unlink()

            logger.info(f"已清理 {older_than_days} 天前的下载文件")


class DownloadManager:
    """
    下载管理器

    批量下载管理，支持并发控制和进度追踪
    """

    def __init__(self, download_dir: str = './downloads', max_concurrent: int = 3):
        self.downloader = FileDownloader(download_dir)
        self.max_concurrent = max_concurrent
        self.results: list = []

    async def download_batch(self, items: list, referer: str = None) -> list:
        """
        批量下载

        Args:
            items: 下载项列表 [{"url": "...", "type": "...", "name": "..."}, ...]
            referer: 来源页面

        Returns:
            下载结果列表
        """
        semaphore = asyncio.Semaphore(self.max_concurrent)

        async def download_one(item):
            async with semaphore:
                return await self.downloader.download(
                    url=item['url'],
                    file_type=item.get('type', 'unknown'),
                    filename=item.get('name'),
                    referer=referer
                )

        tasks = [download_one(item) for item in items]
        self.results = await asyncio.gather(*tasks)

        return self.results

    def get_summary(self) -> Dict[str, Any]:
        """获取下载摘要"""
        success_count = sum(1 for r in self.results if r.get('success'))
        total_size = sum(r.get('size', 0) for r in self.results if r.get('success'))

        return {
            'total': len(self.results),
            'success': success_count,
            'failed': len(self.results) - success_count,
            'total_size': total_size,
            'total_size_mb': round(total_size / (1024 * 1024), 2)
        }