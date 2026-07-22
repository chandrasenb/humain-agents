# humain-agents

Mono-repo for agents built on the HUMAIN marketplace platform. Each agent
lives in its own folder under `agents/`, deployable independently to the
platform (`agent.yaml` manifest + container image).

## Layout

```
agents/
└── <agent-name>/
    ├── agent.yaml       Platform manifest — runtime, connectors, tool nodes
    ├── Dockerfile
    ├── requirements.txt
    └── src/             Agent implementation
```

## Agents

| Agent | Description |
|-------|-------------|
| [`meeting-manager`](agents/meeting-manager/) | Reads and schedules Google Calendar events, with conflict detection. |
