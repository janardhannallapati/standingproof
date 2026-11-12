"""The Anthropic implementation of ModelClient. The only module that imports the SDK."""

import logging
from typing import Any, Literal

import anthropic

from standingproof.agent.loop import ModelCallError
from standingproof.agent.types import AnswerDraft, ModelTurn, ToolCall, Usage

log = logging.getLogger(__name__)

Effort = Literal["low", "medium", "high", "xhigh", "max"]


class AnthropicModel:
    def __init__(
        self,
        client: anthropic.AsyncAnthropic,
        *,
        model: str,
        effort: Effort,
        max_tokens: int = 16_000,
    ) -> None:
        self._client = client
        self._model = model
        self._effort = effort
        self._max_tokens = max_tokens

    async def complete(
        self, *, system: str, messages: list[dict[str, Any]], tools: list[dict[str, Any]]
    ) -> ModelTurn:
        try:
            response = await self._client.messages.parse(
                model=self._model,
                max_tokens=self._max_tokens,
                # The breakpoint caches tools + system, which are identical on every call.
                system=[{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
                messages=messages,  # type: ignore[arg-type]
                tools=tools,  # type: ignore[arg-type]
                # The reply must validate against AnswerDraft. Newer models reject a forced
                # tool choice, so a schema on the output is how a typed result is obtained.
                output_format=AnswerDraft,
                output_config={"effort": self._effort},
            )
        except anthropic.APIConnectionError as error:
            log.warning("model call failed: connection error")
            raise ModelCallError("could not reach the model provider") from error
        except anthropic.RateLimitError as error:
            log.warning("model call failed: rate limited")
            raise ModelCallError("rate limited by the model provider") from error
        except anthropic.APIStatusError as error:
            # 4xx here is our bug (bad request, bad key); 5xx is theirs. Both stop the run.
            log.warning("model call failed: HTTP %s", error.status_code)
            raise ModelCallError(f"model provider returned HTTP {error.status_code}") from error

        tool_calls = [
            ToolCall(id=block.id, name=block.name, arguments=dict(block.input))
            for block in response.content
            if block.type == "tool_use"
        ]
        draft = response.parsed_output if response.stop_reason == "end_turn" else None
        return ModelTurn(
            stop_reason=response.stop_reason or "unknown",
            tool_calls=tool_calls,
            draft=draft,
            usage=Usage(
                input_tokens=response.usage.input_tokens,
                output_tokens=response.usage.output_tokens,
                cache_creation_input_tokens=response.usage.cache_creation_input_tokens or 0,
                cache_read_input_tokens=response.usage.cache_read_input_tokens or 0,
            ),
            # Priced by the model we asked for; the response may name a dated variant.
            model=self._model,
            # Passed back as-is on the next call, including thinking blocks.
            assistant_content=response.content,
        )
