"""
LLM Client — Unified interface for LLM API calls with function calling support.

Merged from old project's llm_client.py, api_gateway_client.py, and llm_circuit_breaker.py.
Key addition: chat_with_tools() for OpenAI-compatible function calling.
"""

import asyncio
import hashlib
import json
import logging
import random
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum
from typing import Any, Optional

import httpx

logger = logging.getLogger("tools.llm")


# ---------------------------------------------------------------------------
# Error types
# ---------------------------------------------------------------------------

class ErrorType(str, Enum):
    NETWORK = "network"
    RATE_LIMIT = "rate_limit"
    AUTH = "auth"
    INVALID_REQUEST = "invalid_request"
    SERVER = "server"
    TIMEOUT = "timeout"
    UNKNOWN = "unknown"


@dataclass
class LLMError:
    error_type: ErrorType
    message: str
    status_code: int | None = None
    is_recoverable: bool = True
    retry_after: int | None = None

    def __str__(self):
        return f"[{self.error_type.value}] {self.message}"


class LLMException(Exception):
    def __init__(self, error: LLMError):
        self.error = error
        super().__init__(str(error))

    @property
    def is_recoverable(self) -> bool:
        return self.error.is_recoverable


class NetworkException(LLMException):
    pass


class RateLimitException(LLMException):
    pass


class AuthException(LLMException):
    pass


class ServerException(LLMException):
    pass


# ---------------------------------------------------------------------------
# Function calling data types
# ---------------------------------------------------------------------------

@dataclass
class ToolCall:
    """A single tool call from LLM response."""
    id: str
    name: str
    arguments: dict[str, Any]


@dataclass
class ChatResponse:
    """Structured LLM response with optional tool calls."""
    content: str | None
    tool_calls: list[ToolCall] = field(default_factory=list)
    finish_reason: str = "stop"
    usage: dict[str, int] = field(default_factory=dict)

    @property
    def has_tool_calls(self) -> bool:
        return len(self.tool_calls) > 0


# ---------------------------------------------------------------------------
# Core LLM Client
# ---------------------------------------------------------------------------

class LLMClient:
    """OpenAI-compatible LLM client with function calling support.

    Key methods:
    - generate(): Simple text generation (backward compatible).
    - chat_with_tools(): Function calling for the controller loop (NEW).
    """

    DEFAULT_MAX_RETRIES = 3
    DEFAULT_RETRY_DELAY = 1.0
    DEFAULT_RETRY_MULTIPLIER = 2.0

    def __init__(
        self,
        api_key: str,
        model: str = "claude-opus-4-5-20251101",
        api_base: str | None = None,
        timeout: int = 120,
    ):
        self.api_key = api_key
        self.model = model
        self.api_base = (api_base or "http://45.78.224.156:3000/v1").rstrip("/")
        self.api_url = f"{self.api_base}/chat/completions"
        self.client = httpx.AsyncClient(timeout=timeout)

        # Stats
        self.call_count = 0
        self.total_tokens = 0
        self.error_count = 0

    # --- Core: Function Calling (NEW) ---

    async def chat_with_tools(
        self,
        messages: list[dict],
        tools: list[dict] | None = None,
        tool_choice: str = "auto",
        max_tokens: int = 4096,
        temperature: float = 0.3,
        max_retries: int | None = None,
    ) -> ChatResponse:
        """Chat completion with OpenAI function calling.

        This is the core interface for the LLM-as-Controller architecture.

        Args:
            messages: OpenAI-format message list.
            tools: OpenAI function calling tool schemas.
            tool_choice: "auto" | "none" | "required".
            max_tokens: Max response tokens.
            temperature: Sampling temperature.
            max_retries: Override default retry count.

        Returns:
            ChatResponse with content and/or tool_calls.
        """
        body = {
            "model": self.model,
            "messages": messages,
            "max_tokens": max_tokens,
            "temperature": temperature,
        }
        if tools:
            body["tools"] = tools
            body["tool_choice"] = tool_choice

        raw = await self._call_with_retry(body, max_retries)
        self.call_count += 1
        self.total_tokens += raw.get("usage", {}).get("total_tokens", 0)

        choice = raw["choices"][0]
        message = choice["message"]

        tool_calls = []
        if message.get("tool_calls"):
            for tc in message["tool_calls"]:
                args_str = tc["function"]["arguments"]
                try:
                    args = json.loads(args_str) if isinstance(args_str, str) else args_str
                except json.JSONDecodeError:
                    args = {"_raw": args_str}
                tool_calls.append(ToolCall(
                    id=tc["id"],
                    name=tc["function"]["name"],
                    arguments=args,
                ))

        return ChatResponse(
            content=message.get("content"),
            tool_calls=tool_calls,
            finish_reason=choice.get("finish_reason", "stop"),
            usage=raw.get("usage", {}),
        )

    # --- Simple text generation (backward compatible) ---

    async def generate(
        self,
        prompt: str,
        system_prompt: str | None = None,
        max_tokens: int = 1024,
        temperature: float = 0.7,
        max_retries: int | None = None,
    ) -> str:
        """Generate text (no function calling)."""
        messages = []
        if system_prompt:
            messages.append({"role": "system", "content": system_prompt})
        messages.append({"role": "user", "content": prompt})

        body = {
            "model": self.model,
            "messages": messages,
            "max_tokens": max_tokens,
            "temperature": temperature,
        }

        raw = await self._call_with_retry(body, max_retries)
        self.call_count += 1
        self.total_tokens += raw.get("usage", {}).get("total_tokens", 0)

        return raw["choices"][0]["message"].get("content", "")

    # --- Retry logic ---

    async def _call_with_retry(
        self, body: dict, max_retries: int | None = None
    ) -> dict:
        """Call API with exponential backoff retry."""
        max_retries = max_retries or self.DEFAULT_MAX_RETRIES
        last_error = None

        for attempt in range(max_retries):
            try:
                result = await self._call_api(body)
                if result is not None:
                    return result
                last_error = LLMError(
                    ErrorType.UNKNOWN, "Empty response", is_recoverable=True
                )
            except LLMException as e:
                last_error = e.error
                if not e.is_recoverable:
                    raise
            except Exception as e:
                last_error = LLMError(
                    ErrorType.UNKNOWN, str(e), is_recoverable=True
                )

            if attempt < max_retries - 1 and last_error.is_recoverable:
                delay = self.DEFAULT_RETRY_DELAY * (
                    self.DEFAULT_RETRY_MULTIPLIER ** attempt
                )
                if last_error.retry_after:
                    delay = max(delay, last_error.retry_after)
                # Add jitter
                delay += random.uniform(0, delay * 0.1)
                logger.warning(
                    f"LLM call failed, retrying in {delay:.1f}s "
                    f"(attempt {attempt + 2}/{max_retries}): {last_error}"
                )
                await asyncio.sleep(delay)

        self.error_count += 1
        raise LLMException(last_error)

    async def _call_api(self, body: dict) -> dict | None:
        """Single API call."""
        if not self.api_key or not self.api_key.strip():
            raise LLMException(LLMError(
                ErrorType.AUTH, "API key not set", is_recoverable=False
            ))

        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.api_key}",
        }

        try:
            response = await self.client.post(
                self.api_url, headers=headers, json=body
            )
            if response.status_code == 200:
                return response.json()
            else:
                error = self._classify_error(response.status_code, response.text)
                logger.error(f"API error {response.status_code}: {response.text[:200]}")
                if not error.is_recoverable:
                    raise LLMException(error)
                return None

        except httpx.TimeoutException:
            raise LLMException(
                self._classify_error(None, "Request timeout")
            )
        except httpx.ConnectError as e:
            raise LLMException(
                self._classify_error(None, f"Connection error: {e}")
            )
        except LLMException:
            raise
        except Exception as e:
            error = self._classify_error(None, str(e))
            if not error.is_recoverable:
                raise LLMException(error)
            return None

    @staticmethod
    def _classify_error(status_code: int | None, message: str) -> LLMError:
        """Classify API error for retry decisions."""
        if status_code in (401, 403):
            return LLMError(ErrorType.AUTH, message, status_code, is_recoverable=False)
        elif status_code == 429:
            return LLMError(
                ErrorType.RATE_LIMIT, message, status_code,
                is_recoverable=True, retry_after=60,
            )
        elif status_code and 500 <= status_code < 600:
            return LLMError(ErrorType.SERVER, message, status_code, is_recoverable=True)
        elif status_code and 400 <= status_code < 500:
            return LLMError(
                ErrorType.INVALID_REQUEST, message, status_code, is_recoverable=False
            )
        elif "timeout" in message.lower():
            return LLMError(ErrorType.TIMEOUT, message, is_recoverable=True)
        elif "network" in message.lower() or "connection" in message.lower():
            return LLMError(ErrorType.NETWORK, message, is_recoverable=True)
        else:
            return LLMError(ErrorType.UNKNOWN, message, status_code, is_recoverable=False)

    async def close(self):
        await self.client.aclose()

    def get_stats(self) -> dict[str, Any]:
        return {
            "model": self.model,
            "api_url": self.api_url,
            "call_count": self.call_count,
            "total_tokens": self.total_tokens,
            "error_count": self.error_count,
        }

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        await self.close()


# ---------------------------------------------------------------------------
# Circuit Breaker
# ---------------------------------------------------------------------------

class CircuitState(str, Enum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


class CircuitBreaker:
    """Prevents cascading failures by tracking error rates."""

    def __init__(
        self,
        failure_threshold: int = 5,
        recovery_timeout: int = 60,
        half_open_max: int = 3,
    ):
        self.failure_threshold = failure_threshold
        self.recovery_timeout = recovery_timeout
        self.half_open_max = half_open_max

        self.state = CircuitState.CLOSED
        self.failure_count = 0
        self.last_failure_time: datetime | None = None
        self._half_open_count = 0

        # Stats
        self.total = 0
        self.successes = 0
        self.failures = 0
        self.rejections = 0

    def call_allowed(self) -> bool:
        if self.state == CircuitState.CLOSED:
            return True
        elif self.state == CircuitState.OPEN:
            if (
                self.last_failure_time
                and datetime.now() - self.last_failure_time
                > timedelta(seconds=self.recovery_timeout)
            ):
                self.state = CircuitState.HALF_OPEN
                self._half_open_count = 0
                logger.info("Circuit breaker → HALF_OPEN")
                return True
            self.rejections += 1
            return False
        else:  # HALF_OPEN
            if self._half_open_count < self.half_open_max:
                self._half_open_count += 1
                return True
            return False

    def record_success(self):
        self.total += 1
        self.successes += 1
        self.failure_count = 0
        self.state = CircuitState.CLOSED

    def record_failure(self):
        self.total += 1
        self.failures += 1
        self.failure_count += 1
        self.last_failure_time = datetime.now()
        if self.failure_count >= self.failure_threshold:
            self.state = CircuitState.OPEN
            logger.warning(
                f"Circuit breaker → OPEN (failures: {self.failure_count})"
            )

    def get_stats(self) -> dict:
        return {
            "state": self.state.value,
            "failure_count": self.failure_count,
            "total": self.total,
            "successes": self.successes,
            "failures": self.failures,
            "rejections": self.rejections,
        }


class ResilientLLMClient:
    """LLMClient wrapped with circuit breaker for resilience."""

    def __init__(self, client: LLMClient, breaker: CircuitBreaker | None = None):
        self.client = client
        self.breaker = breaker or CircuitBreaker()

    async def chat_with_tools(self, *args, **kwargs) -> ChatResponse:
        if not self.breaker.call_allowed():
            raise LLMException(LLMError(
                ErrorType.SERVER,
                f"Circuit breaker is {self.breaker.state.value}",
                is_recoverable=True,
            ))
        try:
            result = await self.client.chat_with_tools(*args, **kwargs)
            self.breaker.record_success()
            return result
        except Exception as e:
            self.breaker.record_failure()
            raise

    async def generate(self, *args, **kwargs) -> str:
        if not self.breaker.call_allowed():
            raise LLMException(LLMError(
                ErrorType.SERVER,
                f"Circuit breaker is {self.breaker.state.value}",
                is_recoverable=True,
            ))
        try:
            result = await self.client.generate(*args, **kwargs)
            self.breaker.record_success()
            return result
        except Exception as e:
            self.breaker.record_failure()
            raise

    async def close(self):
        await self.client.close()

    def get_stats(self) -> dict:
        return {
            "client": self.client.get_stats(),
            "circuit_breaker": self.breaker.get_stats(),
        }

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        await self.close()


# ---------------------------------------------------------------------------
# LLM Cache
# ---------------------------------------------------------------------------

class LLMCache:
    """Simple in-memory LRU cache for LLM responses."""

    def __init__(self, max_size: int = 500):
        self._cache: dict[str, dict] = {}
        self.max_size = max_size

    @staticmethod
    def _key(prompt: str, system_prompt: str | None = None) -> str:
        content = f"{prompt}|{system_prompt or ''}"
        return hashlib.md5(content.encode()).hexdigest()

    def get(self, key: str) -> str | None:
        entry = self._cache.get(key)
        if entry:
            entry["hits"] += 1
            return entry["response"]
        return None

    def set(self, key: str, response: str) -> None:
        if len(self._cache) >= self.max_size:
            least = min(self._cache, key=lambda k: self._cache[k]["hits"])
            del self._cache[least]
        self._cache[key] = {
            "response": response,
            "timestamp": datetime.now(),
            "hits": 1,
        }


class CachedLLMClient(LLMClient):
    """LLMClient with response caching for generate() calls."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.cache = LLMCache()

    async def generate(self, prompt: str, system_prompt: str | None = None,
                       **kwargs) -> str:
        key = self.cache._key(prompt, system_prompt)
        cached = self.cache.get(key)
        if cached is not None:
            return cached
        result = await super().generate(prompt, system_prompt, **kwargs)
        self.cache.set(key, result)
        return result
