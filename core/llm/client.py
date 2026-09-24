"""Azure OpenAI access - chat with structured output, plus embeddings.

Credentials come from Vault rather than a JSON file on disk. Both entry points
retry with exponential backoff and are safe to call from a thread pool, which
is how the orchestrator fans out across tickets.
"""

import json
import threading
import time
from typing import Any, Dict, List, Optional, Tuple

from openai import AzureOpenAI

from core.logging_setup import get_logger

log = get_logger('core.llm')

_RETRYABLE = ('rate limit', 'timeout', 'timed out', 'overloaded', 'temporarily',
              '429', '500', '502', '503', '504', 'connection')


class LlmError(RuntimeError):
    pass


class LlmClient:
    def __init__(self, settings, vault):
        secret_path = settings.get('vault.paths.azure_openai', 'sd_advisor_llm')
        secret = vault.secret(secret_path)

        endpoint = secret.get('azure_endpoint') or settings.get('llm.azure_endpoint')
        api_key = secret.get('azure_api_key') or secret.get('api_key')
        if not endpoint or not api_key:
            raise LlmError(
                f"Vault secret '{secret_path}' must contain azure_endpoint and azure_api_key"
            )

        self.api_version = secret.get('azure_api_version') or settings.get(
            'llm.api_version', '2024-10-21')
        self.chat_deployment = settings.require('llm.chat_deployment')
        self.embedding_deployment = settings.require('llm.embedding_deployment')
        self.embedding_dims = int(settings.get('llm.embedding_dims', 1536))
        self.max_retries = int(settings.get('llm.max_retries', 3))
        self.timeout = int(settings.get('llm.request_timeout_seconds', 90))

        self._client = AzureOpenAI(
            api_key=api_key,
            api_version=self.api_version,
            azure_endpoint=endpoint,
            timeout=self.timeout,
            max_retries=0,          # we run our own backoff so it is observable
        )

        self._usage_lock = threading.Lock()
        self.usage = {'chat_calls': 0, 'embed_calls': 0,
                      'prompt_tokens': 0, 'completion_tokens': 0}

        log.info('Azure OpenAI ready (chat=%s, embed=%s, api=%s)',
                 self.chat_deployment, self.embedding_deployment, self.api_version)

    # -- chat --------------------------------------------------------------

    def structured(self, system_prompt: str, user_prompt: str,
                   json_schema: Dict[str, Any],
                   temperature: float = 0.1) -> Tuple[Dict[str, Any], Dict[str, Any]]:
        """One structured-output call. Returns (parsed_json, call_metadata)."""
        messages = [
            {'role': 'system', 'content': system_prompt},
            {'role': 'user', 'content': user_prompt},
        ]

        started = time.time()
        response = self._with_retry(
            lambda: self._client.chat.completions.create(
                model=self.chat_deployment,
                messages=messages,
                temperature=temperature,
                response_format={'type': 'json_schema', 'json_schema': json_schema},
            ),
            what='chat',
        )

        content = response.choices[0].message.content or ''
        finish_reason = response.choices[0].finish_reason

        if finish_reason == 'length':
            raise LlmError('Model output truncated before the JSON was complete')
        if finish_reason == 'content_filter':
            raise LlmError('Response blocked by the Azure content filter')

        try:
            parsed = json.loads(content)
        except json.JSONDecodeError as exc:
            raise LlmError(f'Model did not return valid JSON: {exc}') from exc

        usage = getattr(response, 'usage', None)
        meta = {
            'model': getattr(response, 'model', self.chat_deployment),
            'latency_ms': int((time.time() - started) * 1000),
            'prompt_tokens': getattr(usage, 'prompt_tokens', None),
            'completion_tokens': getattr(usage, 'completion_tokens', None),
            'finish_reason': finish_reason,
            'raw': content,
        }

        with self._usage_lock:
            self.usage['chat_calls'] += 1
            self.usage['prompt_tokens'] += meta['prompt_tokens'] or 0
            self.usage['completion_tokens'] += meta['completion_tokens'] or 0

        return parsed, meta

    # -- embeddings --------------------------------------------------------

    def embed(self, texts: List[str], batch_size: int = 64) -> List[List[float]]:
        """Embed a list of texts, batched. Order is preserved."""
        cleaned = [(t or '').strip()[:8000] or ' ' for t in texts]
        vectors: List[List[float]] = []

        for start in range(0, len(cleaned), batch_size):
            batch = cleaned[start:start + batch_size]
            response = self._with_retry(
                lambda b=batch: self._client.embeddings.create(
                    model=self.embedding_deployment, input=b),
                what='embed',
            )
            vectors.extend(item.embedding for item in
                           sorted(response.data, key=lambda d: d.index))

            with self._usage_lock:
                self.usage['embed_calls'] += 1

        return vectors

    def embed_one(self, text: str) -> List[float]:
        return self.embed([text])[0]

    # -- retry -------------------------------------------------------------

    def _with_retry(self, call, what: str):
        last_error: Optional[Exception] = None

        for attempt in range(1, self.max_retries + 1):
            try:
                return call()
            except Exception as exc:
                last_error = exc
                message = str(exc).lower()
                retryable = any(token in message for token in _RETRYABLE)

                if not retryable or attempt == self.max_retries:
                    break

                delay = min(2 ** attempt, 30)
                log.warning('%s call failed (attempt %d/%d): %s - retrying in %ss',
                            what, attempt, self.max_retries, str(exc)[:200], delay)
                time.sleep(delay)

        raise LlmError(f'{what} call failed after {self.max_retries} attempts: {last_error}')
