import json

import google.auth
import pytest
from google.adk.models.llm_request import LlmRequest
from google.genai import types

google.auth.default = lambda *args, **kwargs: (None, "test-project")

from story_teller_agent.dedicated_predict import (  # noqa: E402
    DedicatedPredictModel,
    prediction_text,
    prompt_text,
)
from story_teller_agent import agent as story_agent  # noqa: E402

_RESOURCE = "projects/1/locations/us-central1/endpoints/abc"


class _Response:
    def __init__(self, status, payload=None, text=""):
        self.status_code = status
        self._payload = payload or {}
        self.text = text or json.dumps(self._payload)

    def json(self):
        return self._payload


class _Session:
    def __init__(self, responses):
        self.calls = []
        self._responses = list(responses)

    def get(self, url, timeout=0):
        self.calls.append(("GET", url, None))
        return self._responses.pop(0)

    def post(self, url, json=None, timeout=0):
        self.calls.append(("POST", url, json))
        response = self._responses.pop(0)
        if response.status_code < 400 and json:
            prompt = json["instances"][0]["prompt"]
            response._payload = {"predictions": [f"Prompt:\n{prompt}\nOutput:\nok"]}
        return response


def _request(prompt="hi", temperature=0.2, max_output_tokens=None):
    return LlmRequest(
        contents=[types.Content(role="user", parts=[types.Part(text=prompt)])],
        config=types.GenerateContentConfig(
            system_instruction="Write one chapter.",
            temperature=temperature,
            max_output_tokens=max_output_tokens,
        ),
    )


def test_prediction_text_strips_only_the_echoed_prompt():
    raw = "Prompt:\nhi\nOutput:\nhello\nOutput:\nagain"
    assert prediction_text({"predictions": [raw]}, "hi") == "hello\nOutput:\nagain"


def test_prediction_text_rejects_a_missing_echo():
    with pytest.raises(RuntimeError, match="did not echo"):
        prediction_text({"predictions": ["hello"]}, "hi")


def test_prompt_joins_instruction_and_user_text():
    assert prompt_text(_request("a station")) == "Write one chapter.\n\na station"


def test_predict_posts_to_the_dedicated_host():
    session = _Session([
        _Response(200, {"dedicatedEndpointDns": "gemma.example.prediction.vertexai.goog"}),
        _Response(200, {"predictions": ["Prompt:\nhi\nOutput:\nok"]}),
    ])
    model = DedicatedPredictModel(model=_RESOURCE, region="us-central1", max_output_tokens=800)
    model._session = session
    assert model._predict(_request()) == "ok"
    assert session.calls[0][0] == "GET"
    assert session.calls[0][1].startswith("https://us-central1-aiplatform.googleapis.com/v1/")
    method, url, body = session.calls[1]
    assert method == "POST"
    assert url == f"https://gemma.example.prediction.vertexai.goog/v1/{_RESOURCE}:predict"
    assert body["instances"] == [{"prompt": "Write one chapter.\n\nhi"}]
    assert body["parameters"]["temperature"] == 0.2
    assert body["parameters"]["maxOutputTokens"] == 800


def test_request_token_cap_replaces_the_model_cap():
    session = _Session([
        _Response(200, {"dedicatedEndpointDns": "gemma.example.prediction.vertexai.goog"}),
        _Response(200, {"predictions": ["Prompt:\nhi\nOutput:\nok"]}),
    ])
    model = DedicatedPredictModel(model=_RESOURCE, region="us-central1", max_output_tokens=800)
    model._session = session
    model._predict(_request(max_output_tokens=50))
    assert session.calls[1][2]["parameters"]["maxOutputTokens"] == 50


def test_predict_error_includes_the_provider_status():
    session = _Session([
        _Response(200, {"dedicatedEndpointDns": "gemma.example.prediction.vertexai.goog"}),
        _Response(403, text="permission denied"),
    ])
    model = DedicatedPredictModel(model=_RESOURCE, region="us-central1", max_output_tokens=800)
    model._session = session
    with pytest.raises(RuntimeError, match="predict 403: permission denied"):
        model._predict(_request())


def test_story_model_is_one_instance_and_uses_the_region(monkeypatch):
    previous = story_agent._MODEL
    story_agent._MODEL = None
    monkeypatch.setenv("STORY_MODEL_ENDPOINT", _RESOURCE)
    monkeypatch.setenv("STORY_MODEL_REGION", "us-central1")
    try:
        first = story_agent.story_model()
        assert story_agent.story_model() is first
        assert first.region == "us-central1"
        assert first.max_output_tokens == (story_agent.N_CHAPTERS + 1) * story_agent.MAX_WORDS * 2
    finally:
        story_agent._MODEL = previous


def test_story_model_rejects_a_region_that_disagrees_with_the_resource(monkeypatch):
    previous = story_agent._MODEL
    story_agent._MODEL = None
    monkeypatch.setenv("STORY_MODEL_ENDPOINT", _RESOURCE)
    monkeypatch.setenv("STORY_MODEL_REGION", "europe-west1")
    try:
        with pytest.raises(RuntimeError, match="does not match"):
            story_agent.story_model()
    finally:
        story_agent._MODEL = previous
