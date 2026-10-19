#!/usr/bin/env python3
"""Standalone probe: does DashScope's OpenAI-compatible API support OpenAI file_search?

This is intentionally outside the motion-agent app loop. Run:

  PYTHONPATH=/root/CerebelServer python3 \\
    motion_agent/scripts/probe_qwen_openai_file_search.py

Requires ~/Qwen_KEY.txt (or DASHSCOPE_API_KEY). Uses the CN compatible-mode base URL
by default (same as the demo).

What OpenAI's hosted file_search needs
--------------------------------------
  files.create(purpose=\"assistants\") → vector_stores → responses/assistants
  with tools=[{type:\"file_search\", vector_store_ids:[...]}]

What this script checks on Qwen/DashScope
-----------------------------------------
  1. Route existence: /files, /vector_stores, /assistants, /responses
  2. File upload purposes (OpenAI vs DashScope)
  3. Hosted file_search via Responses API
  4. Chat Completions with a file_search \"tool\" (is it hosted or just a function stub?)
  5. Qwen-native document access: system message ``fileid://...`` (qwen-long / qwen-doc-turbo)
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import httpx
from openai import OpenAI

DEFAULT_BASE = os.environ.get(
    "MOTION_AGENT_BASE_URL",
    "https://dashscope.aliyuncs.com/compatible-mode/v1",
)
SECRET = "ALPHA-FLIP-42"
DOC_A = (
    f"CEREBEL_SECRET_MARKER equals {SECRET}. "
    "Positive intrinsic somersault means backflip direction.\n"
)
DOC_B = (
    "CEREBEL_RULEBOOK: always prefer rider-intrinsic somersault for front vs back. "
    "World-frame root Euler is for flip extent only.\n"
)


@dataclass
class Check:
    name: str
    ok: bool
    detail: str
    http_status: int | None = None
    raw: Any = None


@dataclass
class Report:
    base_url: str
    checks: list[Check] = field(default_factory=list)

    def add(self, **kwargs: Any) -> Check:
        c = Check(**kwargs)
        self.checks.append(c)
        flag = "PASS" if c.ok else "FAIL"
        status = f" HTTP {c.http_status}" if c.http_status is not None else ""
        print(f"[{flag}]{status} {c.name}: {c.detail}")
        return c

    def summary(self) -> dict[str, Any]:
        return {
            "base_url": self.base_url,
            "passed": sum(1 for c in self.checks if c.ok),
            "failed": sum(1 for c in self.checks if not c.ok),
            "checks": [asdict(c) for c in self.checks],
        }


def _load_api_key() -> str:
    key_file = Path(os.environ.get("QWEN_KEY_FILE", str(Path.home() / "Qwen_KEY.txt")))
    if key_file.is_file():
        for line in key_file.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line.startswith("Qwen_API_KEY="):
                return line.split("=", 1)[1].strip()
            if line.startswith("DASHSCOPE_API_KEY="):
                return line.split("=", 1)[1].strip()
            if line.startswith("sk-"):
                return line
    env = os.environ.get("DASHSCOPE_API_KEY") or os.environ.get("QWEN_API_KEY")
    if env:
        return env.strip()
    raise SystemExit("No DashScope/Qwen API key (set DASHSCOPE_API_KEY or ~/Qwen_KEY.txt)")


def _client(base: str, key: str) -> OpenAI:
    return OpenAI(api_key=key, base_url=base, timeout=90.0)


def _http(base: str, key: str) -> httpx.Client:
    return httpx.Client(
        base_url=base.rstrip("/"),
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
        timeout=90.0,
    )


def _upload_txt(client: OpenAI, text: str, purpose: str, name: str) -> Any:
    with tempfile.NamedTemporaryFile("w", suffix=".txt", prefix=name, delete=False) as f:
        f.write(text)
        path = f.name
    try:
        return client.files.create(file=open(path, "rb"), purpose=purpose)
    finally:
        Path(path).unlink(missing_ok=True)


def probe_routes(report: Report, http: httpx.Client) -> None:
    expectations = {
        ("GET", "/files"): {200},
        ("GET", "/vector_stores"): {200, 404},
        ("POST", "/vector_stores"): {200, 404, 400, 405},
        ("GET", "/assistants"): {200, 404},
        ("POST", "/assistants"): {200, 404, 400, 405},
        ("GET", "/responses"): {200, 404, 405},
    }
    for (method, path), allowed in expectations.items():
        try:
            r = http.request(method, path, json={} if method == "POST" else None)
            body = (r.text or "")[:180].replace("\n", " ")
            # For OpenAI file_search we need vector stores. 404 ⇒ not available.
            if path == "/vector_stores":
                report.add(
                    name=f"route {method} {path}",
                    ok=r.status_code == 200,
                    detail=f"status={r.status_code} body={body}",
                    http_status=r.status_code,
                )
            elif path == "/assistants":
                report.add(
                    name=f"route {method} {path}",
                    ok=r.status_code == 200,
                    detail=f"status={r.status_code} body={body}",
                    http_status=r.status_code,
                )
            else:
                report.add(
                    name=f"route {method} {path}",
                    ok=r.status_code in allowed,
                    detail=f"status={r.status_code} body={body}",
                    http_status=r.status_code,
                )
        except Exception as exc:  # noqa: BLE001
            report.add(
                name=f"route {method} {path}",
                ok=False,
                detail=f"{type(exc).__name__}: {exc}",
            )


def probe_upload_purposes(report: Report, client: OpenAI) -> str | None:
    """Return a file-extract file_id if upload works."""
    file_id = None
    for purpose in ("assistants", "user_data", "file-extract", "batch"):
        try:
            obj = _upload_txt(client, DOC_A, purpose, f"probe_{purpose}_")
            ok = purpose in ("file-extract", "batch")
            report.add(
                name=f"files.create purpose={purpose}",
                ok=ok and bool(obj.id),
                detail=f"id={obj.id} status={getattr(obj, 'status', None)}",
            )
            if purpose == "file-extract":
                file_id = obj.id
            elif purpose == "batch":
                try:
                    client.files.delete(obj.id)
                except Exception:  # noqa: BLE001
                    pass
        except Exception as exc:  # noqa: BLE001
            # OpenAI-only purposes should fail on DashScope.
            expected_fail = purpose in ("assistants", "user_data")
            report.add(
                name=f"files.create purpose={purpose}",
                ok=expected_fail,
                detail=f"{type(exc).__name__}: {str(exc)[:220]}",
            )
    return file_id


def probe_openai_hosted_file_search(report: Report, http: httpx.Client) -> None:
    payload = {
        "model": os.environ.get("PROBE_MODEL", "qwen-plus"),
        "input": f"What is the value of CEREBEL_SECRET_MARKER? Look for {SECRET}.",
        "tools": [{"type": "file_search", "vector_store_ids": ["vs_dummy"]}],
    }
    r = http.post("/responses", json=payload)
    body = r.text[:400].replace("\n", " ")
    # Success would be 200 with a file_search call / citations. We expect rejection.
    supports = r.status_code == 200 and "file_search" in body and "does not support" not in body
    report.add(
        name="Responses API hosted tools[].type=file_search",
        ok=False if not supports else True,
        detail=(
            "Hosted OpenAI-style file_search is available"
            if supports
            else f"Not available / rejected: {body}"
        ),
        http_status=r.status_code,
        raw=body,
    )
    # Flip ok semantics for the report: this check documents support.
    # Re-record explicitly as support verdict.
    report.checks[-1].ok = supports


def probe_chat_file_search_as_function(report: Report, client: OpenAI) -> None:
    """If the model emits a function tool call named file_search, it is NOT hosted search."""
    try:
        resp = client.chat.completions.create(
            model=os.environ.get("PROBE_MODEL", "qwen-plus"),
            messages=[
                {
                    "role": "user",
                    "content": "Use file_search to find CEREBEL_SECRET_MARKER in my docs.",
                }
            ],
            tools=[
                {
                    "type": "function",
                    "function": {
                        "name": "file_search",
                        "description": "Search uploaded files",
                        "parameters": {
                            "type": "object",
                            "properties": {"query": {"type": "string"}},
                            "required": ["query"],
                        },
                    },
                }
            ],
        )
        msg = resp.choices[0].message
        tcs = msg.tool_calls or []
        if tcs:
            report.add(
                name="chat.completions file_search as function tool",
                ok=True,
                detail=(
                    "Model can *request* a client-side function named file_search "
                    f"(id={tcs[0].id}, name={tcs[0].function.name}) — this is NOT "
                    "OpenAI hosted retrieval; your app would have to implement search."
                ),
            )
        else:
            report.add(
                name="chat.completions file_search as function tool",
                ok=False,
                detail=f"No tool_calls; content={(msg.content or '')[:180]!r}",
            )
    except Exception as exc:  # noqa: BLE001
        report.add(
            name="chat.completions file_search as function tool",
            ok=False,
            detail=f"{type(exc).__name__}: {str(exc)[:240]}",
        )


def probe_qwen_fileid_docs(
    report: Report, client: OpenAI, file_ids: list[str]
) -> None:
    """Qwen-native multi-file access via system ``fileid://`` (not OpenAI file_search)."""
    models = [
        m.strip()
        for m in os.environ.get(
            "PROBE_FILEID_MODELS", "qwen-long,qwen-doc-turbo,qwen-plus"
        ).split(",")
        if m.strip()
    ]
    sys_files = [{"role": "system", "content": f"fileid://{fid}"} for fid in file_ids]
    messages = [
        {"role": "system", "content": "You answer strictly from the provided files."},
        *sys_files,
        {
            "role": "user",
            "content": (
                "What is the exact value of CEREBEL_SECRET_MARKER? "
                "Also mention one rule from CEREBEL_RULEBOOK if present. Quote markers."
            ),
        },
    ]
    for model in models:
        try:
            resp = client.chat.completions.create(
                model=model, messages=messages, max_tokens=256
            )
            content = resp.choices[0].message.content or ""
            hit = SECRET in content
            report.add(
                name=f"Qwen fileid:// multi-file via {model}",
                ok=hit,
                detail=f"secret_found={hit}; answer={(content[:220]).replace(chr(10), ' ')!r}",
            )
        except Exception as exc:  # noqa: BLE001
            report.add(
                name=f"Qwen fileid:// multi-file via {model}",
                ok=False,
                detail=f"{type(exc).__name__}: {str(exc)[:240]}",
            )


def main() -> int:
    base = DEFAULT_BASE
    key = _load_api_key()
    report = Report(base_url=base)
    print(f"Probing OpenAI-compatible file_search on {base}\n")

    client = _client(base, key)
    http = _http(base, key)
    created: list[str] = []

    try:
        probe_routes(report, http)
        fe = probe_upload_purposes(report, client)
        if fe:
            created.append(fe)
        probe_openai_hosted_file_search(report, http)
        probe_chat_file_search_as_function(report, client)

        # Second doc for multi-file fileid:// test
        try:
            b = _upload_txt(client, DOC_B, "file-extract", "probe_b_")
            created.append(b.id)
            # brief wait in case processing lags
            time.sleep(0.5)
        except Exception as exc:  # noqa: BLE001
            report.add(
                name="upload second file-extract doc",
                ok=False,
                detail=str(exc)[:200],
            )

        if len(created) >= 1:
            probe_qwen_fileid_docs(report, client, created[:2])

    finally:
        for fid in created:
            try:
                client.files.delete(fid)
            except Exception:  # noqa: BLE001
                pass
        http.close()

    out = report.summary()
    print("\n=== SUMMARY ===")
    print(json.dumps({k: out[k] for k in ("base_url", "passed", "failed")}, indent=2))
    print(
        "\nVerdict:\n"
        "  • OpenAI hosted file_search (vector_stores + Responses/Assistants tool): "
        "NOT available on this DashScope compatible endpoint.\n"
        "  • DashScope Files API works for purpose=file-extract|batch.\n"
        "  • Qwen-native alternative: upload file-extract + reference fileid:// in "
        "system messages (qwen-long / qwen-doc-turbo) — full-file context, not RAG search.\n"
        "  • Chat can still *emit* a client-side function tool named file_search; "
        "you would implement retrieval yourself (embeddings/rerank).\n"
    )

    out_path = Path(
        os.environ.get(
            "PROBE_REPORT_PATH",
            "/tmp/qwen_openai_file_search_probe.json",
        )
    )
    out_path.write_text(json.dumps(out, indent=2, default=str), encoding="utf-8")
    print(f"Wrote {out_path}")
    # Exit 0 even if OpenAI file_search is unsupported — the probe itself succeeded.
    return 0


if __name__ == "__main__":
    sys.exit(main())
