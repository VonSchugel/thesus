# Research and selection

Reviewed November 5, 2026. The project started as a local prototype with durable chat, an event timeline, checkpoints, and a fake adapter. The live workspace lacked shared pixels and a human input path. The original browser-use approval callback inspected the first proposed action while the upstream step could dispatch a batch. One decision from a model now produces one application-owned command, and the same dispatcher handles manual input. [Agent framework and orchestration dossier](research-agent-frameworks.md)

## Ranked options

Scores range from 1, poor fit, to 5, strong fit. Columns follow the brief's order. The weights are functionality 2, architecture 1, performance 1, reliability 2, human control 3, integration 2, maintainability 1, security 2, licensing 1, and engineering effort 1. Higher effort scores mean less work. Totals are out of 80. These are integration judgments for this Python application. Performance ratings reflect available evidence, with local timings measured separately.

| Rank | Candidate | Scores in criterion order | Weighted total | Decision |
| --- | --- | --- | ---: | --- |
| 1 | Playwright with demand-driven JPEG, local command gate | 5 / 5 / 4 / 4 / 5 / 5 / 5 / 4 / 5 / 4 | 74 | Adopt. Direct control of the same context, process-specific revision checks, and one operating path. |
| 2 | Playwright with CDP or Playwright screencast callbacks | 5 / 4 / 5 / 3 / 5 / 4 / 4 / 4 / 5 / 3 | 68 | Revisit for faster motion. Requires frame backpressure, tab rebinding and password masking before it can replace the live JPEG path. |
| 3 | Browserbase with Stagehand | 5 / 4 / 4 / 3 / 3 / 3 / 4 / 3 / 3 / 3 | 55 | Useful managed option when credentials and a hosted deployment are approved. An interactive vendor viewer would need to participate in the application's ownership protocol. |
| 4 | Self-hosted Steel | 4 / 4 / 4 / 3 / 3 / 3 / 3 / 4 / 4 / 2 | 54 | Reference for session casting and remote infrastructure. It adds a browser service and independent control surface. |
| 5 | Extend browser-use hooks | 4 / 3 / 3 / 2 / 2 / 5 / 4 / 2 / 5 / 3 | 50 | Keep as reference. Source inspection found a batch-level callback and lifecycle behavior that would require an exact-action integration redesign. |
| 6 | Browserless live URL | 4 / 3 / 4 / 3 / 2 / 3 / 4 / 3 / 2 / 3 | 48 | Its interactive link is a separate input path. Review deployment licensing before reuse. |
| 7 | noVNC or WebRTC desktop | 4 / 3 / 4 / 3 / 2 / 2 / 4 / 3 / 4 / 2 | 47 | Consider when native dialog fidelity, audio, or high frame rate justifies a display server, transport service, and input bridge. |
| 8 | DOM mirror | 2 / 3 / 5 / 2 / 1 / 3 / 3 / 3 / 5 / 3 | 42 | Use DOM/AX as the agent observation channel and real browser pixels for manual work. A mirror would need independent support for canvas, cross-origin frames, and visual state. |

SQLite plus an in-process scheduler fits one deployment worker. LangGraph checkpointing, LlamaIndex workflows and Temporal become relevant for workflow graphs, queues, or distributed recovery. Browser effects still need their own idempotency rules when workflow nodes or activities can replay. Existing branching and checkpoint behavior can support the current task without a second persistence system. [Orchestration comparison and source analysis](research-agent-frameworks.md)

## Measurement and next decision

The early comparison used a 1280 × 800 animated fixture to contrast sampled JPEG with CDP capture. The integration benchmark then used a real Chromium workflow over loopback to measure same-session takeover, input and resumption. A small fixture gives reproducible latency and safety checks. It cannot establish general website task success or a remote video service's throughput. Compare equal frame rates and a constrained network before adding CDP, WebRTC or a hosted provider. [Optimization report](optimization.md)

The next model evaluation should hold the browser and task set constant. Compare full versus bounded accessibility context and screenshot-on-demand. Track task success, wrong-target clicks, confirmation false positives, recovery, wall time, and provider tokens. Establish an acceptance bar before moving from the guided baseline to sensitive workflows. WebArena and BrowserGym provide task environments for this phase. Run browser-side prompt-injection cases separately from ordinary navigation and preserve negative results. [WebArena paper](https://arxiv.org/abs/2307.13854), [BrowserGym](https://github.com/ServiceNow/BrowserGym), [Mind the Web](https://arxiv.org/abs/2506.07153).

The delivery includes verified source and issue review, practitioner accounts labeled as anecdotes, and local capture and application benchmarks. Hosted Browserbase, Steel and Browserless were compared through docs and accessible source. Their private implementation, service costs under load and comparative reliability were outside this evaluation. The current workspace is a source snapshot without Git metadata. [Browser infrastructure dossier](research-browser-infrastructure.md)
