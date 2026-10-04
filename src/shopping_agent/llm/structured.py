"""Structured generation: schema-constrained output, validated, with one repair attempt."""

import json
import re

from pydantic import BaseModel, ValidationError

from shopping_agent.errors import ValidationFailedError
from shopping_agent.llm.client import LLMClient, LLMRole, RunUsage
from shopping_agent.llm.schema import strict_schema
from shopping_agent.llm.types import Message, ResponseSchema

_FENCE = re.compile(r"^\s*```(?:json)?\s*|\s*```\s*$", re.IGNORECASE)


def _extract_json(text: str) -> str:
    cleaned = _FENCE.sub("", text.strip())
    if cleaned.startswith(("{", "[")):
        return cleaned
    start, end = cleaned.find("{"), cleaned.rfind("}")
    return cleaned[start : end + 1] if start != -1 and end > start else cleaned


def _errors(exc: ValidationError | json.JSONDecodeError) -> str:
    if isinstance(exc, json.JSONDecodeError):
        return f"not valid JSON ({exc.msg})"
    return "; ".join(
        f"{'.'.join(str(p) for p in e['loc']) or 'root'}: {e['msg']}" for e in exc.errors()[:8]
    )


async def generate_structured[T: BaseModel](
    llm: LLMClient,
    role: LLMRole,
    messages: list[Message],
    model: type[T],
    *,
    usage: RunUsage | None = None,
    max_tokens: int | None = None,
) -> T:
    schema = ResponseSchema(name=model.__name__, json_schema=strict_schema(model))
    conversation = list(messages)
    last_error = ""
    for attempt in range(2):
        response = await llm.complete(
            role, conversation, response_schema=schema, usage=usage, max_tokens=max_tokens
        )
        try:
            return model.model_validate_json(_extract_json(response.text))
        except ValidationError as exc:
            last_error = _errors(exc)
        except json.JSONDecodeError as exc:
            last_error = _errors(exc)
        if attempt == 0:
            conversation = [
                *conversation,
                response.assistant_message,
                Message.user(
                    "Your previous reply did not match the required JSON schema: "
                    f"{last_error}. Reply again with only the corrected JSON object."
                ),
            ]
    raise ValidationFailedError(f"model output for {model.__name__} was invalid: {last_error}")
