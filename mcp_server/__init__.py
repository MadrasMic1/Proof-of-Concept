# mcp_server/__init__.py
"""The RIC MCP server: a separate process, the agents' only way to reach data.

Never import app.main or app.api from here (tests/agentic/test_mcp_policy_tokens.py
checks it): the server reuses the services, not the HTTP API.
"""
