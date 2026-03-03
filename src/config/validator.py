"""
配置验证器 - 环境变量和配置验证
"""

import os
import logging
from dataclasses import dataclass, field
from typing import Optional, List, Dict, Any, Callable
from enum import Enum

logger = logging.getLogger(__name__)


class ConfigLevel(Enum):
    """配置级别"""
    REQUIRED = "required"      # 必需，缺失时报错
    RECOMMENDED = "recommended"  # 推荐，缺失时警告
    OPTIONAL = "optional"       # 可选，无提示


@dataclass
class ConfigField:
    """配置字段定义"""
    name: str
    level: ConfigLevel
    default: Optional[str] = None
    validator: Optional[Callable[[str], bool]] = None
    description: str = ""
    example: str = ""


@dataclass
class ConfigValidationResult:
    """配置验证结果"""
    valid: bool
    errors: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    config: Dict[str, Any] = field(default_factory=dict)


class ConfigValidator:
    """
    配置验证器

    职责：
    - 验证环境变量配置
    - 检查必需配置项
    - 提供友好的错误提示

    使用示例：
        validator = ConfigValidator()
        result = validator.validate()
        if not result.valid:
            print(result.errors)
    """

    # 配置字段定义
    CONFIG_FIELDS: Dict[str, ConfigField] = {
        # LLM 配置
        'ZHIPU_API_KEY': ConfigField(
            name='ZHIPU_API_KEY',
            level=ConfigLevel.RECOMMENDED,
            description='智谱 AI API Key，用于 LLM 调用',
            example='your-api-key-here'
        ),
        'LLM_MODEL': ConfigField(
            name='LLM_MODEL',
            level=ConfigLevel.OPTIONAL,
            default='glm-4',
            description='LLM 模型名称',
            example='glm-4, glm-4-flash, qwen-turbo'
        ),
        'LLM_API_BASE': ConfigField(
            name='LLM_API_BASE',
            level=ConfigLevel.OPTIONAL,
            default=None,
            description='LLM API 基础 URL（自定义端点）',
            example='https://open.bigmodel.cn/api/paas/v4/'
        ),

        # 浏览器配置
        'HEADLESS': ConfigField(
            name='HEADLESS',
            level=ConfigLevel.OPTIONAL,
            default='true',
            description='浏览器无头模式',
            example='true, false'
        ),
        'BROWSER_TIMEOUT': ConfigField(
            name='BROWSER_TIMEOUT',
            level=ConfigLevel.OPTIONAL,
            default='30000',
            description='浏览器操作超时时间（毫秒）',
            example='30000'
        ),

        # 调试配置
        'DEBUG': ConfigField(
            name='DEBUG',
            level=ConfigLevel.OPTIONAL,
            default='false',
            description='调试模式开关',
            example='true, false'
        ),
        'LOG_LEVEL': ConfigField(
            name='LOG_LEVEL',
            level=ConfigLevel.OPTIONAL,
            default='INFO',
            description='日志级别',
            example='DEBUG, INFO, WARNING, ERROR'
        ),
    }

    def __init__(self, extra_fields: Optional[Dict[str, ConfigField]] = None):
        """
        初始化验证器

        Args:
            extra_fields: 额外的配置字段定义
        """
        self.fields = dict(self.CONFIG_FIELDS)
        if extra_fields:
            self.fields.update(extra_fields)

    def validate(self, fail_fast: bool = False) -> ConfigValidationResult:
        """
        验证所有配置

        Args:
            fail_fast: 遇到第一个错误即返回

        Returns:
            ConfigValidationResult: 验证结果
        """
        result = ConfigValidationResult(valid=True)

        for name, field_def in self.fields.items():
            value = os.getenv(name)

            # 检查必需字段
            if field_def.level == ConfigLevel.REQUIRED and not value:
                result.valid = False
                error_msg = self._format_missing_error(field_def)
                result.errors.append(error_msg)
                logger.error(error_msg)
                if fail_fast:
                    return result
                continue

            # 检查推荐字段
            if field_def.level == ConfigLevel.RECOMMENDED and not value:
                warning_msg = self._format_missing_warning(field_def)
                result.warnings.append(warning_msg)
                logger.warning(warning_msg)

            # 验证字段值
            if value and field_def.validator:
                if not field_def.validator(value):
                    result.valid = False
                    error_msg = f"配置验证失败: {name} 的值 '{value}' 无效"
                    result.errors.append(error_msg)
                    logger.error(error_msg)
                    if fail_fast:
                        return result
                    continue

            # 设置默认值或使用配置值
            result.config[name] = value if value is not None else field_def.default

        return result

    def get_config(self) -> Dict[str, Any]:
        """
        获取配置字典（包含默认值）

        Returns:
            Dict: 配置字典
        """
        config = {}
        for name, field_def in self.fields.items():
            value = os.getenv(name)
            config[name] = value if value is not None else field_def.default
        return config

    def get(self, name: str, default: Optional[str] = None) -> Optional[str]:
        """
        获取单个配置值

        Args:
            name: 配置名称
            default: 默认值

        Returns:
            配置值或默认值
        """
        value = os.getenv(name)
        if value is not None:
            return value
        if default is not None:
            return default
        field_def = self.fields.get(name)
        if field_def:
            return field_def.default
        return None

    def require(self, name: str) -> str:
        """
        获取必需的配置值

        Args:
            name: 配置名称

        Returns:
            配置值

        Raises:
            ValueError: 配置缺失
        """
        value = self.get(name)
        if value is None:
            field_def = self.fields.get(name)
            if field_def:
                raise ValueError(self._format_missing_error(field_def))
            raise ValueError(f"缺少必需配置: {name}")
        return value

    def _format_missing_error(self, field: ConfigField) -> str:
        """格式化缺失错误消息"""
        msg = f"缺少必需配置: {field.name}"
        if field.description:
            msg += f"\n  说明: {field.description}"
        if field.example:
            msg += f"\n  示例: {field.example}"
        msg += f"\n  设置方式: export {field.name}='your-value'"
        return msg

    def _format_missing_warning(self, field: ConfigField) -> str:
        """格式化缺失警告消息"""
        msg = f"推荐配置 {field.name} 未设置"
        if field.description:
            msg += f" - {field.description}"
        if field.example:
            msg += f" (示例: {field.example})"
        return msg


class LLMConfigValidator(ConfigValidator):
    """LLM 配置专用验证器"""

    def __init__(self):
        extra_fields = {
            'LLM_TEMPERATURE': ConfigField(
                name='LLM_TEMPERATURE',
                level=ConfigLevel.OPTIONAL,
                default='0.7',
                description='LLM 温度参数',
                example='0.0 - 2.0'
            ),
            'LLM_MAX_TOKENS': ConfigField(
                name='LLM_MAX_TOKENS',
                level=ConfigLevel.OPTIONAL,
                default='4096',
                description='LLM 最大 Token 数',
                example='1024, 2048, 4096'
            ),
        }
        super().__init__(extra_fields)

    def validate_llm_available(self) -> bool:
        """
        验证 LLM 是否可用

        Returns:
            bool: LLM 是否可用
        """
        api_key = os.getenv('ZHIPU_API_KEY')
        return api_key is not None and len(api_key) > 0

    def get_llm_config(self) -> Dict[str, Any]:
        """
        获取 LLM 配置

        Returns:
            Dict: LLM 配置字典
        """
        return {
            'api_key': self.get('ZHIPU_API_KEY'),
            'model': self.get('LLM_MODEL', 'glm-4'),
            'api_base': self.get('LLM_API_BASE'),
            'temperature': float(self.get('LLM_TEMPERATURE', '0.7')),
            'max_tokens': int(self.get('LLM_MAX_TOKENS', '4096')),
        }


def validate_config(strict: bool = False) -> ConfigValidationResult:
    """
    快捷函数：验证配置

    Args:
        strict: 严格模式，遇到警告也视为失败

    Returns:
        ConfigValidationResult: 验证结果
    """
    validator = ConfigValidator()
    result = validator.validate()

    if strict and result.warnings:
        result.valid = False

    return result


def check_requirements() -> bool:
    """
    检查系统要求

    Returns:
        bool: 是否满足所有要求
    """
    issues = []

    # 检查 Python 版本
    import sys
    if sys.version_info < (3, 8):
        issues.append(f"Python 版本过低: {sys.version}, 需要 >= 3.8")

    # 检查关键依赖
    required_packages = [
        ('playwright', 'playwright'),
        ('bs4', 'beautifulsoup4'),
        ('yaml', 'pyyaml'),
        ('dotenv', 'python-dotenv'),
    ]

    for module, package in required_packages:
        try:
            __import__(module)
        except ImportError:
            issues.append(f"缺少依赖包: {package}")

    if issues:
        for issue in issues:
            logger.error(issue)
        return False

    return True