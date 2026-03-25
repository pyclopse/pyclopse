"""pyclawops self-knowledge system.

Provides the `self` MCP server — a FastMCP server that exposes pyclawops's own
documentation and source code to agents, enabling self-improvement workflows.

Tools exposed:
  self_topics()          — list all available knowledge topics
  self_read(topic)       — read documentation for a topic
  self_source(module)    — read pyclawops source with line numbers
"""
