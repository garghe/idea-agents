# Agent runner image: runs one role per ECS task (see src/idea_agents/aws/task_entry.py).
# The Claude Agent SDK wheel bundles the Claude Code CLI it drives.
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    IDEA_CATALOGUE=/app/catalogue

WORKDIR /app
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-cache-dir ".[aws]"
COPY catalogue ./catalogue

RUN useradd --create-home --uid 10001 agent
USER agent
WORKDIR /home/agent

ENTRYPOINT ["python", "-m", "idea_agents.aws.task_entry"]
