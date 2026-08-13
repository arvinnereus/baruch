"""Baruch MCP server — exposes the meeting library to Claude Code /
Claude Desktop over stdio. Register once:

  claude mcp add localfellow -s user -- \
    "<app>/.venv/bin/python" "<app>/mcp_server.py"

All tools are read-only over local files; nothing leaves this Mac.
"""
from mcp.server.mcpserver import MCPServer

import meeting_tools

server = MCPServer("localfellow")


@server.tool()
def list_meetings(limit: int = 30) -> list[dict]:
    """List recent Baruch meetings (id, title, created_at epoch,
    duration seconds, status). Newest first."""
    return meeting_tools.list_meetings(limit)


@server.tool()
def search_meetings(query: str, limit: int = 12) -> list[dict]:
    """Full-text search across ALL meeting transcripts, AI notes, and titles.
    Returns hits with meeting_id, meeting_title, speaker, MM:SS timestamp,
    and a snippet. Use this first for any 'what was said about X' question."""
    return meeting_tools.search_meetings(query, limit)


@server.tool()
def get_transcript(meeting_id: str, start_ts: str = "", end_ts: str = "") -> str:
    """Speaker-labelled, timestamped transcript of one meeting. Optionally
    limit to a [start_ts, end_ts] MM:SS window (recommended for long
    meetings — full transcripts are truncated at ~9000 chars)."""
    return meeting_tools.get_transcript(meeting_id, start_ts, end_ts)


@server.tool()
def get_note(meeting_id: str) -> str:
    """The AI note (summary, action items, decisions, topics) for a meeting,
    as Markdown."""
    return meeting_tools.get_note(meeting_id)


@server.tool()
def get_agenda(meeting_id: str) -> str:
    """The agenda (talking points, action items, notepad) for a meeting."""
    return meeting_tools.get_agenda(meeting_id)


if __name__ == "__main__":
    server.run()  # stdio transport
