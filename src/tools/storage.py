"""
存储工具 - 证据和数据存储
"""

from typing import Dict, Any, List, Optional
from datetime import datetime
from pathlib import Path
import json
import shutil


class EvidenceStorage:
    """
    证据存储

    存储运行过程中的所有证据：
    - 截图
    - HTML 快照
    - 提取的数据
    - 错误日志
    - 性能指标
    """

    def __init__(self, base_dir: str = './evidence'):
        self.base_dir = Path(base_dir)
        self.base_dir.mkdir(parents=True, exist_ok=True)
        self.current_task_dir = None

    def create_task_dir(self, task_id: str) -> Path:
        """创建任务目录"""
        task_dir = self.base_dir / task_id
        task_dir.mkdir(parents=True, exist_ok=True)
        self.current_task_dir = task_dir

        # 创建子目录
        (task_dir / 'screenshots').mkdir(exist_ok=True)
        (task_dir / 'html').mkdir(exist_ok=True)
        (task_dir / 'data').mkdir(exist_ok=True)
        (task_dir / 'logs').mkdir(exist_ok=True)
        (task_dir / 'metrics').mkdir(exist_ok=True)

        return task_dir

    def save_screenshot(self, data: bytes, name: Optional[str] = None) -> str:
        """保存截图"""
        if not self.current_task_dir:
            raise ValueError('No task directory created')

        if name is None:
            name = datetime.now().strftime('%Y%m%d_%H%M%S.png')

        path = self.current_task_dir / 'screenshots' / name
        with open(path, 'wb') as f:
            f.write(data)

        return str(path)

    def save_html(self, html: str, name: Optional[str] = None) -> str:
        """保存 HTML 快照"""
        if not self.current_task_dir:
            raise ValueError('No task directory created')

        if name is None:
            name = datetime.now().strftime('%Y%m%d_%H%M%S.html')

        path = self.current_task_dir / 'html' / name
        with open(path, 'w', encoding='utf-8') as f:
            f.write(html)

        return str(path)

    def save_data(self, data: List[Dict[str, Any]],
                 name: Optional[str] = None) -> str:
        """保存提取的数据"""
        if not self.current_task_dir:
            raise ValueError('No task directory created')

        if name is None:
            name = datetime.now().strftime('%Y%m%d_%H%M%S.json')

        path = self.current_task_dir / 'data' / name
        with open(path, 'w', encoding='utf-8') as f:
            json.dump(data, f, indent=2, ensure_ascii=False)

        return str(path)

    def save_log(self, message: str, level: str = 'info') -> str:
        """保存日志"""
        if not self.current_task_dir:
            raise ValueError('No task directory created')

        log_path = self.current_task_dir / 'logs' / 'task.log'

        timestamp = datetime.now().isoformat()
        log_entry = f'[{timestamp}] [{level.upper()}] {message}\n'

        with open(log_path, 'a', encoding='utf-8') as f:
            f.write(log_entry)

        return str(log_path)

    def save_metrics(self, metrics: Dict[str, Any],
                    name: Optional[str] = None) -> str:
        """保存性能指标"""
        if not self.current_task_dir:
            raise ValueError('No task directory created')

        if name is None:
            name = datetime.now().strftime('%Y%m%d_%H%M%S.json')

        path = self.current_task_dir / 'metrics' / name
        with open(path, 'w', encoding='utf-8') as f:
            json.dump(metrics, f, indent=2, ensure_ascii=False)

        return str(path)

    def get_task_dir(self) -> Optional[Path]:
        """获取当前任务目录"""
        return self.current_task_dir

    def list_tasks(self) -> List[str]:
        """列出所有任务"""
        return [d.name for d in self.base_dir.iterdir() if d.is_dir()]

    def get_task_summary(self, task_id: str) -> Dict[str, Any]:
        """获取任务摘要"""
        task_dir = self.base_dir / task_id

        if not task_dir.exists():
            return {}

        summary = {
            'task_id': task_id,
            'screenshots': len(list((task_dir / 'screenshots').glob('*'))),
            'html_snapshots': len(list((task_dir / 'html').glob('*'))),
            'data_files': len(list((task_dir / 'data').glob('*'))),
            'log_entries': 0,
            'metrics_files': len(list((task_dir / 'metrics').glob('*')))
        }

        # 统计日志条目
        log_path = task_dir / 'logs' / 'task.log'
        if log_path.exists():
            with open(log_path, 'r', encoding='utf-8') as f:
                summary['log_entries'] = len(f.readlines())

        return summary


class DataExport:
    """
    数据导出

    支持多种格式：
    - JSON
    - CSV
    - Excel
    """

    def __init__(self):
        pass

    @staticmethod
    def to_json(data: List[Dict[str, Any]], filepath: str,
                indent: int = 2) -> None:
        """导出为 JSON"""
        with open(filepath, 'w', encoding='utf-8') as f:
            json.dump(data, f, indent=indent, ensure_ascii=False)

    @staticmethod
    def to_csv(data: List[Dict[str, Any]], filepath: str) -> None:
        """导出为 CSV"""
        import csv

        if not data:
            return

        fieldnames = list(data[0].keys())

        with open(filepath, 'w', encoding='utf-8', newline='') as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(data)

    @staticmethod
    def to_excel(data: List[Dict[str, Any]], filepath: str) -> None:
        """导出为 Excel"""
        try:
            import pandas as pd
            df = pd.DataFrame(data)
            df.to_excel(filepath, index=False)
        except ImportError:
            # 降级到 CSV
            DataExport.to_csv(data, filepath.replace('.xlsx', '.csv'))

    @staticmethod
    def to_txt(data: List[Dict[str, Any]], filepath: str,
              format_type: str = 'simple') -> None:
        """导出为纯文本"""
        with open(filepath, 'w', encoding='utf-8') as f:
            if format_type == 'simple':
                for i, item in enumerate(data):
                    f.write(f'--- Item {i + 1} ---\n')
                    for key, value in item.items():
                        f.write(f'{key}: {value}\n')
                    f.write('\n')
            elif format_type == 'compact':
                for item in data:
                    f.write('\t'.join(str(v) for v in item.values()) + '\n')


class StateStorage:
    """
    状态存储

    持久化任务状态
    """

    def __init__(self, base_dir: str = './states'):
        self.base_dir = Path(base_dir)
        self.base_dir.mkdir(parents=True, exist_ok=True)

    def save_state(self, task_id: str, state: Dict[str, Any]) -> None:
        """保存状态"""
        state_path = self.base_dir / f'{task_id}_state.json'
        with open(state_path, 'w', encoding='utf-8') as f:
            json.dump(state, f, indent=2, ensure_ascii=False)

    def load_state(self, task_id: str) -> Optional[Dict[str, Any]]:
        """加载状态"""
        state_path = self.base_dir / f'{task_id}_state.json'

        if not state_path.exists():
            return None

        with open(state_path, 'r', encoding='utf-8') as f:
            return json.load(f)

    def list_states(self) -> List[str]:
        """列出所有状态文件"""
        return [
            f.stem.replace('_state', '')
            for f in self.base_dir.glob('*_state.json')
        ]

    def delete_state(self, task_id: str) -> bool:
        """删除状态"""
        state_path = self.base_dir / f'{task_id}_state.json'

        if state_path.exists():
            state_path.unlink()
            return True

        return False


class ConfigStorage:
    """
    配置存储

    管理配置文件
    """

    def __init__(self, config_dir: str = './config'):
        self.config_dir = Path(config_dir)
        self.config_dir.mkdir(parents=True, exist_ok=True)

    def save_config(self, name: str, config: Dict[str, Any]) -> None:
        """保存配置"""
        config_path = self.config_dir / f'{name}.json'
        with open(config_path, 'w', encoding='utf-8') as f:
            json.dump(config, f, indent=2, ensure_ascii=False)

    def load_config(self, name: str) -> Optional[Dict[str, Any]]:
        """加载配置"""
        config_path = self.config_dir / f'{name}.json'

        if not config_path.exists():
            return None

        with open(config_path, 'r', encoding='utf-8') as f:
            return json.load(f)

    def list_configs(self) -> List[str]:
        """列出所有配置"""
        return [
            f.stem for f in self.config_dir.glob('*.json')
        ]
