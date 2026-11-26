# app/agentic/tools/__init__.py
"""The MCP tools as plain async functions: (session, context, args) -> ToolOutput.

The MCP server (mcp_server/) only wires these to FastMCP; the call pipeline
(pipeline.py) adds token, policy, limits, transaction, envelope and audit. Tests
call the functions directly on the test database.
"""
