"""Call a dedicated Vertex endpoint with predict.

generateContent on the shared host is refused. generateContent on
dedicatedEndpointDns returns 500. predict on that DNS returns text.
"""

from __future__ import annotations

import asyncio
from typing import AsyncGenerator

import google.auth
from google.adk.models.base_llm import BaseLlm
from google.adk.models.llm_request import LlmRequest
from google.adk.models.llm_response import LlmResponse
from google.auth.transport.requests import AuthorizedSession
from google.genai import types
from pydantic import PrivateAttr
from typing_extensions import override

_SCOPES = ["https://www.googleapis.com/auth/cloud-platform"]


def resource_region(resource: str) -> str:
    parts = resource.split("/")
    if len(parts) < 6 or parts[0] != "projects" or parts[2] != "locations" or parts[4] != "endpoints":
        raise RuntimeError(f"not an endpoint resource: {resource}")
    return parts[3]


def prompt_text(llm_request: LlmRequest) -> str:
    chunks: list[str] = []
    instruction = llm_request.config.system_instruction
    if isinstance(instruction, str) and instruction.strip():
        chunks.append(instruction.strip())
    for content in llm_request.contents:
        for part in content.parts or []:
            if part.text:
                chunks.append(part.text)
    return "\n\n".join(chunks)


def prediction_text(payload: dict, prompt: str) -> str:
    preds = payload.get("predictions") or []
    if len(preds) != 1 or not isinstance(preds[0], str):
        raise RuntimeError("endpoint returned no text prediction")
    prefix = f"Prompt:\n{prompt}\nOutput:\n"
    raw = preds[0]
    if not raw.startswith(prefix):
        raise RuntimeError("endpoint prediction did not echo the prompt")
    return raw[len(prefix):].strip()


class DedicatedPredictModel(BaseLlm):
    region: str
    max_output_tokens: int
    _session: AuthorizedSession | None = PrivateAttr(default=None)
    _host: str = PrivateAttr(default="")

    def _http(self) -> AuthorizedSession:
        if self._session is None:
            creds, _ = google.auth.default(scopes=_SCOPES)
            self._session = AuthorizedSession(creds)
        return self._session

    def _body(self, response, what: str) -> dict:
        if response.status_code >= 400:
            raise RuntimeError(f"{what} {response.status_code}: {response.text[:300]}")
        return response.json()

    def _dedicated_host(self) -> str:
        if self._host:
            return self._host
        doc = self._body(
            self._http().get(
                f"https://{self.region}-aiplatform.googleapis.com/v1/{self.model}",
                timeout=60,
            ),
            "endpoint",
        )
        host = str(doc.get("dedicatedEndpointDns") or "").strip()
        if not host:
            raise RuntimeError(f"{self.model} has no dedicatedEndpointDns")
        self._host = host
        return host

    def _predict(self, llm_request: LlmRequest) -> str:
        prompt = prompt_text(llm_request)
        parameters: dict = {"maxOutputTokens": self.max_output_tokens}
        if llm_request.config.temperature is not None:
            parameters["temperature"] = llm_request.config.temperature
        if llm_request.config.max_output_tokens:
            parameters["maxOutputTokens"] = llm_request.config.max_output_tokens
        payload = self._body(
            self._http().post(
                f"https://{self._dedicated_host()}/v1/{self.model}:predict",
                json={"instances": [{"prompt": prompt}], "parameters": parameters},
                timeout=60,
            ),
            "predict",
        )
        return prediction_text(payload, prompt)

    @override
    async def generate_content_async(
        self, llm_request: LlmRequest, stream: bool = False
    ) -> AsyncGenerator[LlmResponse, None]:
        text = await asyncio.to_thread(self._predict, llm_request)
        yield LlmResponse(
            content=types.Content(role="model", parts=[types.Part(text=text)]),
            partial=False,
            turn_complete=True,
        )
