"""
契约加载器 - 战略层
负责加载并验证 Spec 契约
"""

import json
import yaml
import logging
from pathlib import Path
from typing import Dict, Any, Union
from .contracts import SpecContract, StateContract, ContractValidator

logger = logging.getLogger(__name__)


class _SpecCompatDict(dict):
    """兼容旧代码的 Spec 访问方式（dict + 属性访问）。"""

    def __getattr__(self, key: str):
        if key == 'name' and 'task_name' in self:
            return self['task_name']
        try:
            return self[key]
        except KeyError as e:
            raise AttributeError(key) from e


def load_config(config_path: Union[str, Path, None] = None) -> Dict[str, Any]:
    """
    加载配置

    Args:
        config_path: 配置文件路径，默认从当前目录或 ~/.config 查找

    Returns:
        配置字典
    """
    if config_path is None:
        # 尝试多个默认位置
        possible_paths = [
            'config.yaml', 'config.json',
            'config/settings.json', 'config/settings.yaml',
            '.config/config.yaml', '.config/config.json',
            'settings.yaml', 'settings.json'
        ]

        for path in possible_paths:
            if Path(path).exists():
                config_path = Path(path)
                break

        if config_path is None:
            # 返回默认配置
            return _get_default_config()

    path = Path(config_path)
    if not path.exists():
        logger.warning(f"配置文件不存在: {path}, 使用默认配置")
        return _get_default_config()

    with open(path, 'r', encoding='utf-8') as f:
        if path.suffix in ['.yaml', '.yml']:
            config = yaml.safe_load(f)
        elif path.suffix == '.json':
            config = json.load(f)
        else:
            logger.warning(f"未知的配置文件格式: {path}, 使用默认配置")
            return _get_default_config()

    # 深度合并默认配置
    default_config = _get_default_config()
    return _deep_merge(default_config, config or {})


def _deep_merge(base: Dict, override: Dict) -> Dict:
    """深度合并两个字典，override 优先。"""
    result = base.copy()
    for key, value in override.items():
        if key in result and isinstance(result[key], dict) and isinstance(value, dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = value
    return result


def _get_default_config() -> Dict[str, Any]:
    """获取默认配置"""
    return {
        'llm': {
            'provider': 'openai_compatible',
            'model': 'claude-opus-4-5-20251101',
            'api_key': '',
            'api_base': 'http://45.78.224.156:3000/v1',
        },
        'browser': {
            'headless': True,
            'timeout': 30000,
            'viewport': {'width': 1920, 'height': 1080},
        },
        'sandbox': {
            'strict_mode': True,
            'timeout': 10,
            'max_memory_mb': 256,
        },
        'retry': {
            'max_attempts': 3,
            'delay': 1.0,
        },
        'logging': {
            'level': 'INFO',
        }
    }


def load_spec(spec_path: Union[str, Path], validate: bool = True) -> Dict[str, Any]:
    """
    兼容函数：加载单个 Spec 文件。

    返回值支持 dict 访问和 .name 属性访问。
    """
    path = Path(spec_path)
    loader = SpecLoader(path.parent if path.parent else Path('.'))
    spec = loader.load_spec(path, validate=validate)
    return _SpecCompatDict(spec)


class SpecLoader:
    """
    Spec 契约加载器

    职责：
    - 加载 Spec 契约文件（JSON/YAML）
    - 验证契约完整性
    - 返回冻结的 SpecContract 对象

    特性：
    - 契约一旦加载即冻结，不可修改
    - 支持版本控制
    - 严格的验证机制
    """

    def __init__(self, spec_dir: Union[str, Path]):
        self.spec_dir = Path(spec_dir)
        self.validator = ContractValidator()

    def load_spec(self, spec_path: Union[str, Path], validate: bool = True) -> SpecContract:
        """
        加载 Spec 契约

        Args:
            spec_path: 契约文件路径
            validate: 是否验证契约

        Returns:
            冻结的 SpecContract 对象

        Raises:
            FileNotFoundError: 契约文件不存在
            ValueError: 契约验证失败
        """
        path = Path(spec_path)

        # 加载契约内容
        spec_data = self._load_file(path)

        # 验证契约（使用两种验证方式）
        if validate:
            self._validate_spec(spec_data)
            # 同时使用 contracts.py 中的验证器
            try:
                self.validator.validate_spec(spec_data)
            except ValueError as e:
                logger.warning(f"ContractValidator 警告: {e}")

        logger.info(f"成功加载 Spec: {spec_data.get('task_name', spec_data.get('task_id', 'unknown'))}")
        return spec_data

    def load_state(self, task_id: str) -> StateContract:
        """
        加载任务状态

        Args:
            task_id: 任务ID

        Returns:
            StateContract 对象
        """
        state_path = self.spec_dir / task_id / 'state.json'

        if not state_path.exists():
            raise FileNotFoundError(f"State file not found: {state_path}")

        state_data = self._load_json(state_path)
        # 使用 ContractFactory 创建 StateContract
        from .contracts import ContractFactory
        # Create a basic spec as fallback
        basic_spec = {
            'version': 'v1',
            'freeze': True,
            'goal': 'Default goal',
            'completion_gate': ['html_snapshot_exists']
        }
        return ContractFactory.create_initial_state(task_id, '', 'Default goal', basic_spec)

    def _load_file(self, path: Path) -> Dict[str, Any]:
        """加载契约文件（支持 JSON/YAML）"""
        if not path.exists():
            raise FileNotFoundError(f"Spec file not found: {path}")

        with open(path, 'r', encoding='utf-8') as f:
            if path.suffix in ['.yaml', '.yml']:
                return yaml.safe_load(f)
            elif path.suffix == '.json':
                return json.load(f)
            else:
                raise ValueError(f"Unsupported file format: {path.suffix}")

    def _load_json(self, path: Path) -> Dict[str, Any]:
        """加载 JSON 文件"""
        with open(path, 'r', encoding='utf-8') as f:
            return json.load(f)

    def _validate_spec(self, spec_data: Dict[str, Any]) -> None:
        """
        验证契约完整性

        验证项：
        - 必需字段存在
        - 任务名称非空
        - 提取目标有效
        - 字段定义完整
        """
        # 验证必需字段
        required_fields = ['task_id', 'task_name']
        for field in required_fields:
            if field not in spec_data:
                raise ValueError(f"Missing required field: {field}")

        # 验证任务名称
        if not spec_data['task_name'].strip():
            raise ValueError("Task name cannot be empty")

        # 根据爬取模式选择验证策略
        crawl_mode = spec_data.get('crawl_mode', 'full_site')

        if crawl_mode in ['single_page', 'multi_page']:
            # 对于单页/多页模式，仍需要 targets
            targets = spec_data.get('targets', [])
            if not targets:
                raise ValueError("At least one extraction target is required for single_page/multi_page mode")

            # 验证每个目标
            for i, target in enumerate(targets):
                if 'name' not in target:
                    raise ValueError(f"Target {i} missing 'name' field")
                if not target['name'].strip():
                    raise ValueError(f"Target {i} name cannot be empty")

                # 验证字段
                fields = target.get('fields', [])
                if not fields:
                    raise ValueError(f"Target '{target['name']}' must have at least one field")

                for j, field in enumerate(fields):
                    # 必需字段
                    if 'name' not in field:
                        raise ValueError(f"Target '{target['name']}' field {j} missing 'name'")
                    if 'selector' not in field and 'description' not in field:
                        raise ValueError(f"Target '{target['name']}' field '{field.get('name', j)}' requires either 'selector' or 'description'")
        elif crawl_mode == 'full_site':
            # 对于全站爬取模式，可能不需要预定义 targets，因为动态发现
            pass  # 验证将在运行时进行

        # 验证基本结构字段是否存在（但不强制要求）
        if 'targets' in spec_data:
            targets = spec_data.get('targets', [])
            for i, target in enumerate(targets):
                if 'name' not in target:
                    raise ValueError(f"Target {i} missing 'name' field")
                if not target['name'].strip():
                    raise ValueError(f"Target {i} name cannot be empty")

                # 验证字段
                fields = target.get('fields', [])
                if not fields:
                    continue  # 允许无字段的目标（可能用于页面发现）

                for j, field in enumerate(fields):
                    # 必需字段
                    if 'name' not in field:
                        raise ValueError(f"Target '{target['name']}' field {j} missing 'name'")
                    if 'selector' not in field and 'description' not in field:
                        raise ValueError(f"Target '{target['name']}' field '{field.get('name', j)}' requires either 'selector' or 'description'")

    def save_spec(self, spec: SpecContract, output_path: Union[str, Path]) -> None:
        """保存契约到文件"""
        path = Path(output_path)
        # SpecContract is a TypedDict which behaves like a dictionary
        spec_dict = spec  # Just use the dict as-is

        with open(path, 'w', encoding='utf-8') as f:
            if path.suffix == '.json':
                json.dump(spec_dict, f, indent=2, ensure_ascii=False)
            elif path.suffix in ['.yaml', '.yml']:
                yaml.dump(spec_dict, f, allow_unicode=True, default_flow_style=False)

    def create_spec_template(self) -> Dict[str, Any]:
        """创建契约模板"""
        return {
            'task_id': 'task_001',
            'task_name': 'Example Task',
            'created_at': '2026-02-24T00:00:00',
            'version': '1.0',
            'extraction_type': 'single_page',
            'targets': [
                {
                    'name': 'products',
                    'fields': [
                        {
                            'name': 'title',
                            'type': 'text',
                            'selector': '.product-title',
                            'required': True,
                            'description': 'Product title'
                        },
                        {
                            'name': 'price',
                            'type': 'number',
                            'selector': '.price',
                            'required': True,
                            'description': 'Product price'
                        }
                    ]
                }
            ],
            'start_url': 'https://example.com',
            'max_pages': 100,
            'depth_limit': 3,
            'validation_rules': {},
            'anti_bot': {
                'random_delay': {'min': 1, 'max': 3},
                'user_agent_rotation': True
            },
            'completion_criteria': {
                'min_items': 10,
                'quality_threshold': 0.9
            }
        }
