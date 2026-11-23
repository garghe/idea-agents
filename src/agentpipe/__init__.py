"""agentpipe: a multi-agent pipeline that turns a business request into a reviewed design and a backlog.

Code owns the flow (stages, gates, loop limits); Claude agents own the thinking inside each stage.
Agents hand over through documents in a case file, never through chat history.
"""

__version__ = "0.1.0"
