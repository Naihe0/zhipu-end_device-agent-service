<!-- Use this file to provide workspace-specific custom instructions to Copilot. For more details, visit https://code.visualstudio.com/docs/copilot/copilot-customization#_use-a-githubcopilotinstructionsmd-file -->

1. Whenever you write anything about the A2A protocol, reference the latest protocol documentation (`docs/a2a_protocol.md`). Use the official `a2a-sdk` (1.x) for all A2A server and client work — do not implement the protocol from scratch. Agent logic belongs in `src/device_agent/a2a/executor.py` (an `AgentExecutor`); task lifecycle, persistence and streaming are handled by the SDK's `DefaultRequestHandler`.

2. Do not repeat the same information or code snippets that have already been provided in the context or any files in the project folder. Always check the context for existing information before suggesting new content.

3. Configuration is centrally managed in `src/device_agent/config.py` (pydantic-settings, loaded from environment variables / `.env`). Add new settings there and document user-facing ones in `.env.example`. Services receive settings through `src/device_agent/container.py`; do not create module-level singletons.

4. Don't hard code anything. Use configuration or environment variables to manage settings and secrets. Never commit `.env`.

5. Whenever you write a test, check `tests/` for an existing test that already covers the same functionality and extend it instead of creating a new one. Offline tests use the fakes in `tests/conftest.py` (scripted LLM, in-process MCP device, in-process A2A agent); tests that call the real Gemini API must be marked `@pytest.mark.live`.

6. If you write any code that interacts with MCP, use the official SDK (`mcp` 2.x; `MCPServer` for servers, `mcp.Client` for clients) and consult https://github.com/modelcontextprotocol/python-sdk and https://modelcontextprotocol.io/docs/learn/client-concepts for best practices.

7. LLM access goes through the `LLMClient` protocol in `src/device_agent/services/llm.py` (Gemini via `google-genai`). Prefer native function calling and structured output (Pydantic schemas) over prompting the model to emit parseable text.

8. Run `uv run ruff check src tests`, `uv run ruff format src tests` and `uv run pytest` before finishing a change.
