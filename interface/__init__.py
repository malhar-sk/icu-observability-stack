"""A read-only way for programs and AI assistants to ask the stack questions.

    questions   interface/features/*.py   one small function per question (add a file to add one, delete it to remove it)
    registry    interface/registry.py     keeps the list, checks every answer before it leaves
    adapter     interface/fleetlink.py    the only code that talks to ./fleet and the stack
    front-ends  interface/mcp_server.py   the same questions as MCP tools (for AI assistants)
                interface/http_api.py     the same questions as GET /v1/<name> (for scripts and curl)

Nothing here can change the stack. See docs/INTERFACE.md.
"""
