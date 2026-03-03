"""
契约定义系统 - 战略层契约规范
定义所有行为必须遵守的契约规范

根据 IMPLEMENTATION.md 的完整设计，包含：
- SpecContract (Spec契约)
- StateContract (状态契约)
- RoutingDecision (路由决策契约)
- EvidenceContract (证据契约)
"""

from typing import Dict, List, Optional, Any, TypedDict, Literal
from datetime import datetime
import json


# ==================== Spec 契约 (IMPLEMENTATION.md 第1.1节) ====================

class SpecContract(TypedDict, total=False):
    """Spec契约结构定义 - 所有任务的完整规范"""

    # ===== 基本信息 =====
    version: str  # 版本号，如 "v1"
    freeze: bool  # 是否冻结，冻结后不可修改
    created_at: str  # 创建时间 ISO 8601格式
    updated_at: str  # 更新时间 ISO 8601格式

    # ===== 任务目标 =====
    goal: str  # 用户目标描述
    target_url: str  # 目标URL

    # ===== 约束条件 =====
    constraints: List[str]  # 约束列表
    max_execution_time: int  # 最大执行时间（秒）
    max_retries: int  # 最大重试次数
    max_iterations: int  # 最大迭代次数

    # ===== 完成门禁条件 =====
    completion_gate: List[str]
    # 支持的门禁条件：
    # - "html_snapshot_exists" - HTML快照存在
    # - "sense_analysis_valid" - 感知分析有效
    # - "code_syntax_valid" - 代码语法正确
    # - "execution_success" - 执行成功
    # - "quality_score >= 0.6" - 质量分数 >= 阈值
    # - "sample_count >= 5" - 样本数 >= 阈值

    # ===== 证据要求 =====
    evidence: Dict[str, List[str]]
    # {
    #     "required": ["spec.yaml", "sense_report.json", "generated_code.py", ...],
    #     "optional": ["screenshots/", "reflection_memory.json"]
    # }

    # ===== 能力需求 =====
    capabilities: List[str]
    # ["sense", "plan", "act", "verify", "judge", "explore", "reflect", "spa_handle"]

    # ===== 爬取模式（Issue #5 新增）=====
    crawl_mode: str
    # 支持：'single_page' | 'multi_page' | 'full_site'（默认）
    max_pages: int   # 最大爬取页面数（multi_page/full_site 模式）
    max_depth: int   # 最大爬取深度（multi_page/full_site 模式）
    url_patterns: List[str]  # URL 路径白名单正则列表（可选）


# ==================== State 契约 (IMPLEMENTATION.md 第1.2节) ====================

class StateContract(TypedDict, total=False):
    """状态契约 - 定义任务的运行时状态"""

    # ===== 任务信息 =====
    task_id: str
    url: str
    goal: str

    # ===== Spec契约 =====
    spec: SpecContract

    # ===== 执行状态 =====
    stage: str  # 'initialized', 'sensing', 'planning', 'acting', 'verifying', 'judging'
    iteration: int
    routing_decision: Optional[Dict[str, Any]]

    # ===== 时间戳 =====
    created_at: str
    updated_at: str
    started_at: Optional[str]
    completed_at: Optional[str]

    # ===== 数据 =====
    html_snapshot: Optional[str]
    sense_analysis: Optional[Dict[str, Any]]
    generated_code: Optional[str]
    execution_result: Optional[Dict[str, Any]]
    sample_data: Optional[List[Any]]
    quality_score: Optional[float]

    # ===== 验证 =====
    syntax_valid: Optional[bool]
    gate_passed: Optional[bool]
    passed_gates: Optional[List[str]]
    failed_gates: Optional[List[str]]

    # ===== 性能 =====
    performance_data: Dict[str, Any]
    # {
    #     "sense_duration": float,
    #     "plan_duration": float,
    #     "act_duration": float,
    #     "total_duration": float,
    #     "llm_calls": int
    # }

    # ===== 历史 =====
    failure_history: List[Dict[str, Any]]
    evidence_collected: Dict[str, Any]

    # ===== 爬取追踪（Issue #5 新增）=====
    visited_urls: List[str]   # 已访问 URL 列表
    queue_size: int           # 当前队列大小
    pages_crawled: int        # 已爬取页面数量
    per_url_results: Dict[str, Any]  # 每个 URL 的结果摘要


# ==================== RoutingDecision 契约 (IMPLEMENTATION.md 第1.3节) ====================

class RoutingDecision(TypedDict, total=False):
    """路由决策契约 - SmartRouter 的决策结果"""

    # ===== 策略信息 =====
    strategy: str  # 策略名称
    capabilities: List[str]  # 需要的能力列表
    expected_success_rate: float  # 预期成功率 0.0-1.0

    # ===== 分析信息 =====
    complexity: Literal['simple', 'medium', 'complex', 'extremely_complex']
    page_type: Literal['static', 'dynamic', 'spa', 'interactive', 'unknown']
    special_requirements: List[str]  # ['login', 'javascript', 'pagination', 'anti-bot']

    # ===== 执行参数 =====
    execution_params: Dict[str, Any]

    # ===== 备选方案 =====
    fallback_strategies: List[str]

    # ===== 元数据 =====
    decided_at: str  # ISO 8601格式
    decision_duration: float  # 秒


# ==================== Evidence 契约 (IMPLEMENTATION.md 第1.4节) ====================

class EvidenceContract(TypedDict, total=False):
    """证据契约 - 各阶段的结构化证据"""

    # ===== 感知证据 =====
    html_snapshot: str
    sense_analysis: Dict[str, Any]

    # ===== 规划证据 =====
    generated_code: str
    reasoning: str

    # ===== 执行证据 =====
    execution_log: Dict[str, Any]
    screenshots: List[str]  # base64编码
    result: Dict[str, Any]

    # ===== 验证证据 =====
    sample_data: List[Any]
    quality_report: Dict[str, Any]
    issues: List[str]

    # ===== 评判证据 =====
    judge_decision: Dict[str, Any]

    # ===== 元数据 =====
    collected_at: str  # ISO 8601格式
    evidence_size: int  # 字节数


# ==================== 契约验证工具 ====================

class ContractValidator:
    """契约验证器"""

    @staticmethod
    def validate_spec(spec: Dict[str, Any]) -> bool:
        """验证Spec契约合法性"""
        required_fields = ['version', 'freeze', 'goal', 'completion_gate']

        for field in required_fields:
            if field not in spec:
                raise ValueError(f"Spec missing required field: {field}")

        if spec.get('freeze') is not True:
            raise ValueError("Spec must be frozen (freeze=true)")

        if not isinstance(spec['completion_gate'], list):
            raise ValueError("completion_gate must be a list")

        return True

    @staticmethod
    def validate_state(state: Dict[str, Any]) -> bool:
        """验证State契约合法性"""
        required_fields = ['task_id', 'url', 'stage', 'iteration']

        for field in required_fields:
            if field not in state:
                raise ValueError(f"State missing required field: {field}")

        if not isinstance(state['iteration'], int):
            raise ValueError("iteration must be an integer")

        return True

    @staticmethod
    def validate_routing_decision(decision: Dict[str, Any]) -> bool:
        """验证RoutingDecision契约合法性"""
        required_fields = ['strategy', 'capabilities', 'expected_success_rate']

        for field in required_fields:
            if field not in decision:
                raise ValueError(f"RoutingDecision missing required field: {field}")

        if not (0 <= decision['expected_success_rate'] <= 1):
            raise ValueError("expected_success_rate must be between 0 and 1")

        valid_capabilities = {'sense', 'plan', 'act', 'verify', 'judge', 'explore', 'reflect'}
        if not all(cap in valid_capabilities for cap in decision['capabilities']):
            raise ValueError(f"Invalid capabilities in {decision['capabilities']}")

        return True


# ==================== 契约工厂 ====================

class ContractFactory:
    """契约工厂 - 创建标准化契约对象"""

    @staticmethod
    def create_spec(
        goal: str,
        target_url: str,
        completion_gate: Optional[List[str]] = None,
        max_execution_time: int = 300,
        max_retries: int = 3,
        max_iterations: int = 10,
        crawl_mode: str = 'full_site',
        max_pages: int = 200,
        max_depth: int = 5,
        url_patterns: Optional[List[str]] = None,
    ) -> SpecContract:
        """创建Spec契约"""
        if completion_gate is None:
            completion_gate = [
                'html_snapshot_exists',
                'sense_analysis_valid',
                'execution_success',
                'quality_score >= 0.6'
            ]

        spec: SpecContract = {
            'version': 'v1',
            'freeze': True,
            'created_at': datetime.now().isoformat(),
            'updated_at': datetime.now().isoformat(),
            'goal': goal,
            'target_url': target_url,
            'constraints': [],
            'max_execution_time': max_execution_time,
            'max_retries': max_retries,
            'max_iterations': max_iterations,
            'completion_gate': completion_gate,
            'evidence': {
                'required': ['spec.yaml', 'sense_report.json', 'generated_code.py'],
                'optional': []
            },
            'capabilities': ['sense', 'plan', 'act', 'verify'],
            'crawl_mode': crawl_mode,
            'max_pages': max_pages,
            'max_depth': max_depth,
            'url_patterns': url_patterns or [],
        }

        return spec

    @staticmethod
    def create_initial_state(
        task_id: str,
        url: str,
        goal: str,
        spec: SpecContract
    ) -> StateContract:
        """创建初始状态"""
        state: StateContract = {
            'task_id': task_id,
            'url': url,
            'goal': goal,
            'spec': spec,
            'stage': 'initialized',
            'iteration': 0,
            'created_at': datetime.now().isoformat(),
            'updated_at': datetime.now().isoformat(),
            'performance_data': {},
            'failure_history': [],
            'evidence_collected': {}
        }

        return state

    @staticmethod
    def create_routing_decision(
        strategy: str,
        capabilities: List[str],
        expected_success_rate: float,
        complexity: str = 'simple',
        page_type: str = 'unknown'
    ) -> RoutingDecision:
        """创建路由决策"""
        decision: RoutingDecision = {
            'strategy': strategy,
            'capabilities': capabilities,
            'expected_success_rate': expected_success_rate,
            'complexity': complexity,
            'page_type': page_type,
            'special_requirements': [],
            'execution_params': {},
            'fallback_strategies': [],
            'decided_at': datetime.now().isoformat(),
            'decision_duration': 0.0
        }

        return decision


# ==================== 辅助函数 ====================

def spec_to_json(spec: SpecContract) -> str:
    """将Spec契约转换为JSON字符串"""
    return json.dumps(spec, ensure_ascii=False, indent=2)


def json_to_spec(json_str: str) -> SpecContract:
    """将JSON字符串转换为Spec契约"""
    spec = json.loads(json_str)
    ContractValidator.validate_spec(spec)
    return spec


def state_to_json(state: StateContract) -> str:
    """将State契约转换为JSON字符串"""
    return json.dumps(state, ensure_ascii=False, indent=2)


def json_to_state(json_str: str) -> StateContract:
    """将JSON字符串转换为State契约"""
    state = json.loads(json_str)
    ContractValidator.validate_state(state)
    return state
