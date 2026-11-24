"""Offline tests for the LLM providers (local HTTP server, fake CLI, fake `anthropic` module)."""

from __future__ import annotations

import json
import sys
import threading
import types
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from skillloop.core.config import LLMSection
from skillloop.core.registry import LLMS
from skillloop.llm.base import LLMError, LLMRouter, build_llm
from skillloop.llm.cli import CLILLM
from skillloop.llm.openai_compat import OpenAICompatLLM

PY = sys.executable

# --------------------------------------------------------------------------- openai-compatible


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_POST(self):
        srv = self.server
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        srv.seen.append(
            {
                "path": self.path,
                "auth": self.headers.get("Authorization"),
                "body": body,
                "x": self.headers.get("X-Extra"),
            }
        )
        if srv.fail_times > 0:
            srv.fail_times -= 1
            self.send_response(srv.fail_code)
            self.send_header("retry-after", "0")
            self.end_headers()
            self.wfile.write(b'{"error": "busy"}')
            return
        user = body["messages"][-1]["content"]
        payload = {
            "model": "served-model",
            "choices": [{"message": {"role": "assistant", "content": f"echo:{user}"}}],
            "usage": {"prompt_tokens": 11, "completion_tokens": 4},
        }
        self.send_response(200)
        self.end_headers()
        self.wfile.write(json.dumps(payload).encode())


@pytest.fixture()
def server():
    srv = HTTPServer(("127.0.0.1", 0), _Handler)
    srv.seen, srv.fail_times, srv.fail_code = [], 0, 503
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield srv, f"http://127.0.0.1:{srv.server_port}/v1"
    srv.shutdown()


def test_openai_compat_basic(server, monkeypatch):
    srv, base = server
    monkeypatch.setenv("MY_KEY", "k-123")
    llm = LLMS.create(
        "openai", model="m1", base_url=base, api_key_env="MY_KEY", headers={"X-Extra": "1"}, structured="json_schema"
    )
    assert isinstance(llm, OpenAICompatLLM)
    r = llm.complete("hi", system="be brief", max_tokens=99, json_schema={"type": "object"}, role="judge")
    assert r.text == "echo:hi" and r.model == "served-model"
    assert r.usage == {"input_tokens": 11.0, "output_tokens": 4.0}
    seen = srv.seen[-1]
    assert seen["path"] == "/v1/chat/completions" and seen["auth"] == "Bearer k-123" and seen["x"] == "1"
    assert seen["body"]["messages"][0] == {"role": "system", "content": "be brief"}
    assert seen["body"]["max_tokens"] == 99 and seen["body"]["model"] == "m1"
    assert seen["body"]["response_format"]["json_schema"]["schema"] == {"type": "object"}
    assert llm.healthcheck()[0]


def test_openai_compat_retries_and_errors(server):
    srv, base = server
    llm = OpenAICompatLLM(model="m", base_url=base, max_retries=2, backoff_s=0.01)
    srv.fail_times = 2
    assert llm.complete("x").text == "echo:x" and len(srv.seen) == 3
    assert "response_format" not in srv.seen[-1]["body"]  # structured="none" by default
    srv.fail_times = 5
    with pytest.raises(LLMError, match="after 3 attempts"):
        llm.complete("x")
    srv.fail_times, srv.fail_code = 1, 400
    n = len(srv.seen)
    with pytest.raises(LLMError, match="HTTP 400"):
        llm.complete("x")
    assert len(srv.seen) == n + 1  # 4xx is not retried
    srv.fail_times = 0
    with pytest.raises(LLMError):
        OpenAICompatLLM(model="m", base_url="http://127.0.0.1:9/v1", max_retries=0, timeout=2).complete("x")
    with pytest.raises(LLMError, match="model"):
        OpenAICompatLLM(base_url=base).complete("x")


def test_complete_json_via_openai(server):
    srv, base = server
    llm = OpenAICompatLLM(model="m", base_url=base)
    # the echo server returns 'echo:{"a": 1}', so tolerant extraction must find the JSON
    assert llm.complete_json('{"a": 1}') == {"a": 1}


# --------------------------------------------------------------------------- cli


def test_cli_llm(tmp_path):
    script = tmp_path / "fake_llm.py"
    script.write_text(
        "import sys\nargs = sys.argv[1:]\nprint('SYS=' + args[args.index('-s') + 1])\n"
        "print('MODEL=' + args[args.index('-m') + 1])\nprint('IN=' + sys.stdin.read())\n"
    )
    llm = LLMS.create("cli", model="m9", command=[PY, str(script), "-m", "{model}", "-s", "{system}"])
    assert isinstance(llm, CLILLM)
    r = llm.complete("hello", system="sys text")
    assert r.text == "SYS=sys text\nMODEL=m9\nIN=hello"
    assert llm.healthcheck()[0]

    # no {system} placeholder -> system prepended; {prompt} placeholder -> no stdin
    llm = CLILLM(command=[PY, "-c", "import sys; print(sys.argv[1])", "{prompt}"])
    assert llm.complete("q", system="S").text == "S\n\nq"
    assert "ONLY a JSON" in llm.complete("q", json_schema={"type": "object"}).text

    with pytest.raises(LLMError, match="failed"):
        CLILLM(command=[PY, "-c", "import sys; sys.exit(4)"]).complete("x")
    with pytest.raises(LLMError, match="no output"):
        CLILLM(command=[PY, "-c", "pass"]).complete("x")
    with pytest.raises(LLMError):
        CLILLM(command=["no-such-llm-binary"]).complete("x")
    assert not CLILLM(command=["no-such-llm-binary"]).healthcheck()[0]
    default = CLILLM(model="sonnet").command
    assert default[:2] == ["claude", "-p"] and default[-2:] == ["--model", "{model}"]
    assert "--dangerously-skip-permissions" not in default


# --------------------------------------------------------------------------- anthropic (fake SDK)


def _fake_anthropic(script: list):
    """A stand-in `anthropic` module. `script` is a list of responses/exceptions consumed per call."""
    mod = types.ModuleType("anthropic")

    class APIError(Exception):
        def __init__(self, message="err", status_code=500):
            super().__init__(message)
            self.message, self.status_code = message, status_code

    class APIConnectionError(APIError): ...

    class APIStatusError(APIError): ...

    class BadRequestError(APIStatusError): ...

    class AuthenticationError(APIStatusError): ...

    class PermissionDeniedError(APIStatusError): ...

    class NotFoundError(APIStatusError): ...

    class RateLimitError(APIStatusError): ...

    for c in (
        APIError,
        APIConnectionError,
        APIStatusError,
        BadRequestError,
        AuthenticationError,
        PermissionDeniedError,
        NotFoundError,
        RateLimitError,
    ):
        setattr(mod, c.__name__, c)
    calls: list[dict] = []

    class _Stream:
        def __init__(self, kwargs):
            self.kwargs = kwargs

        def __enter__(self):
            calls.append(self.kwargs)
            item = script.pop(0)
            if isinstance(item, Exception):
                raise item
            self.item = item
            return self

        def __exit__(self, *a):
            return False

        def get_final_message(self):
            return self.item

    class _Messages:
        def __init__(self, beta):
            self.beta = beta

        def stream(self, **kwargs):
            return _Stream({**kwargs, "_beta": self.beta})

    class Anthropic:
        def __init__(self, **kwargs):
            self.kwargs = kwargs
            self.messages = _Messages(False)
            self.beta = types.SimpleNamespace(messages=_Messages(True))

    mod.Anthropic = Anthropic
    mod.calls = calls
    return mod


def _msg(text="hello", stop_reason="end_turn", category=None):
    blocks = [types.SimpleNamespace(type="thinking", thinking=""), types.SimpleNamespace(type="text", text=text)]
    return types.SimpleNamespace(
        content=blocks if stop_reason != "refusal" else [],
        stop_reason=stop_reason,
        model="claude-opus-5-5",
        stop_details=types.SimpleNamespace(category=category) if category else None,
        usage=types.SimpleNamespace(
            input_tokens=12, output_tokens=5, cache_read_input_tokens=0, cache_creation_input_tokens=None
        ),
    )


def test_anthropic_missing_sdk(monkeypatch):
    monkeypatch.setitem(sys.modules, "anthropic", None)  # makes `import anthropic` raise ImportError
    llm = LLMS.create("anthropic")
    with pytest.raises(LLMError, match=r"pip install 'skillloop\[anthropic\]'"):
        llm.complete("x")
    assert not llm.healthcheck()[0]


def test_anthropic_request_shape(monkeypatch):
    fake = _fake_anthropic([_msg("answer")])
    monkeypatch.setitem(sys.modules, "anthropic", fake)
    llm = LLMS.create("anthropic", effort="high")
    assert llm.model == "claude-opus-5-5"
    r = llm.complete("q", system="S", max_tokens=1234, json_schema={"type": "object"})
    assert r.text == "answer" and r.usage == {
        "input_tokens": 12.0,
        "output_tokens": 5.0,
        "cache_read_input_tokens": 0.0,
    }
    call = fake.calls[0]
    assert (
        call["_beta"] is True
        and call["betas"] == ["server-side-fallback-2026-07-01"]
        and call["fallbacks"] == "default"
    )
    assert call["model"] == "claude-opus-5-5" and call["max_tokens"] == 1234 and call["system"] == "S"
    assert call["thinking"] == {"type": "adaptive"}
    assert call["output_config"] == {"effort": "high", "format": {"type": "json_schema", "schema": {"type": "object"}}}
    assert "temperature" not in call and "top_p" not in call
    assert call["messages"] == [{"role": "user", "content": "q"}]


def test_anthropic_degrades_on_bad_request(monkeypatch):
    fake = _fake_anthropic([])
    monkeypatch.setitem(sys.modules, "anthropic", fake)
    fake_script = [
        fake.BadRequestError("no beta", 400),
        fake.BadRequestError("bad schema", 400),
        _msg('{"a": 1}'),
        _msg("second"),
    ]
    llm = LLMS.create("anthropic")
    # route the script through the stream objects
    import skillloop.llm.anthropic as mod  # noqa: F401

    fake.Anthropic  # (client is created lazily from the fake module)
    script_holder = fake_script

    class _Patched(fake.Anthropic):
        pass

    # re-create the fake with the real script
    fake2 = _fake_anthropic(script_holder)
    for _name in ("BadRequestError",):
        script_holder[0] = fake2.BadRequestError("no beta", 400)
        script_holder[1] = fake2.BadRequestError("bad schema", 400)
    monkeypatch.setitem(sys.modules, "anthropic", fake2)
    r = llm.complete("q", json_schema={"type": "object"})
    assert r.text == '{"a": 1}'
    assert [c["_beta"] for c in fake2.calls] == [True, False, False]
    assert "format" in fake2.calls[1]["output_config"] and "output_config" not in fake2.calls[2]
    llm.complete("again")
    assert fake2.calls[3]["_beta"] is False  # remembered: fallbacks unsupported on this deployment


def test_anthropic_refusal_and_errors(monkeypatch):
    fake = _fake_anthropic([])
    monkeypatch.setitem(sys.modules, "anthropic", fake)
    script = [
        _msg(stop_reason="refusal", category="cyber"),
        fake.AuthenticationError("bad key", 401),
        fake.RateLimitError("slow down", 429),
        fake.NotFoundError("nope", 404),
        fake.APIStatusError("oops", 503),
        fake.APIConnectionError("net"),
    ]
    fake2 = _fake_anthropic(script)
    script[1:] = [
        fake2.AuthenticationError("bad key", 401),
        fake2.RateLimitError("slow down", 429),
        fake2.NotFoundError("nope", 404),
        fake2.APIStatusError("oops", 503),
        fake2.APIConnectionError("net"),
    ]
    monkeypatch.setitem(sys.modules, "anthropic", fake2)
    llm = LLMS.create("anthropic", fallbacks="")
    for pattern in ("refused.*cyber", "authentication", "rate limit", "not found", "API error 503", "connection"):
        with pytest.raises(LLMError, match=pattern):
            llm.complete("x")
    assert all(c["_beta"] is False for c in fake2.calls)


# --------------------------------------------------------------------------- registry / router


def test_registry_and_router():
    for n in ("mock", "anthropic", "openai", "cli"):
        assert n in LLMS.names()
    llm = build_llm(
        LLMSection(
            provider="mock",
            roles={"judge": {"provider": "cli", "model": "x", "options": {"command": [PY, "-c", "print('J')"]}}},
        )
    )
    assert isinstance(llm, LLMRouter)
    assert llm.complete("p", role="judge").text == "J"
    assert llm.complete("p", role="other").text.startswith("[mock:")
    assert isinstance(LLMS.create("skillloop.llm.cli:CLILLM", command=["x"]), CLILLM)
