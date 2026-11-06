# Optimization and remaining work

## Measured workspace performance

Windows 11 build 26200, Intel i7-10810U, Python 3.12.14, Playwright Python 1.64.1, installed headless Chromium 145.0.7632.6, sandbox enabled, and one Uvicorn process on loopback. Each run starts an independent browser profile and SQLite database. The fixture uses a scripted planner driving real Chromium. Five runs per configuration each attempted takeover, click, typing, fresh observation, and completion. Both configurations achieved 5/5 tasks, takeovers and resumptions, with 0/10 failed manual inputs per configuration. The sample is small and uses a controlled site.

| Measurement | Frame cache disabled | 200 ms frame reuse |
| --- | ---: | ---: |
| Cold task to first useful observation, median / max | 1,090 / 1,227 ms | 1,063 / 1,455 ms |
| Takeover, median / max | 54 / 55 ms | 57 / 61 ms |
| Click command, median / max | 38 / 52 ms | 41 / 45 ms |
| Click to changed JPEG received over HTTP, median / max | 84 / 107 ms | 89 / 92 ms |
| Typing command, median / max | 36 / 42 ms | 36 / 41 ms |
| Return control to task completion, median / max | 128 / 196 ms | 137 / 146 ms |
| JPEG HTTP response, 50 requests, median / max | 48.62 / 71.57 ms | 7.04 / 58.55 ms |
| Serial JPEG requests per second, median | 20.22 | 95.63 |
| JPEG payload, median | 16,837 bytes | 16,837 bytes |
| Process tree working set near trial end, median / max | 601 / 615 MiB | 599 / 607 MiB |
| Process tree CPU time over the trial, median / max | 3.81 / 4.11 s | 3.84 / 3.98 s |

Rapid duplicate requests reuse a capture, explaining the faster repeated HTTP responses. The request rate counts reused frames. It does not represent new frames per second. Measurements show no material improvement in click-to-fresh-frame time, memory, or CPU at this sample size. CPU and working set include the Python server, Playwright driver and Chromium children. The uncached case spends longer fetching its samples, so process CPU totals are not a fixed-duration comparison. Task start includes launching Chromium. The 200 ms cache is invalidated on navigation, tab, viewport, and browser command changes.

The outer Chromium UI test recorded pointer-up to changed pixel on screen at **314.5 ms** in an isolated run and **705.5 ms** while other Chromium tests were running. It used a frame-synchronized pixel read in the viewer. These two samples are load-dependent and separate from the HTTP measurement. Browser frame capture throughput under motion, including a matched-rate CDP comparison, is documented in the infrastructure dossier. [Capture experiment](research-browser-infrastructure.md#local-capture-experiment)

Run the same workspace benchmark from the project root. Install `psutil` to include process measurements.

```powershell
& $workspacePython -m pip install psutil
& $workspacePython -m scripts.benchmark_workspace --rounds 5 --frames 10
& $workspacePython -m scripts.benchmark_workspace --uncached --rounds 5 --frames 10
```

## Changes made after measurement and inspection

- Serve JPEGs separately from control and history, capture on demand, and reuse a short-lived frame across viewers. The client holds one frame request at a time and pauses refresh when the workspace is hidden.
- Bound accessibility text, visible target count and decision history. Send screenshots to a vision-enabled model only after a specific request and explicit configuration. The scripted latency benchmark uses zero model tokens. A separate local inference test is reported below.
- Apply ordering, cursors, and result limits in the database. The old history queries materialized whole branches before slicing. The streaming endpoint publishes changed bounded snapshots. High-volume replay and branching still materialize history. No large-database throughput comparison was run.
- Keep model inference outside the branch command lock and browser activity outside write transactions. This lets the human take control while a model decision is pending. The measured takeover time above covers this case. The in-flight navigation test separately verifies that a dispatched command drains before manual input is admitted.
- Add a Chromium egress proxy after reproducing the redirect gap in browser routing. Add a bounded shutdown wait for reset connections on Windows. The redirect fixture verifies that a denied origin receives zero requests.

## Local model smoke test

Ran Qwen3 1.7B Q4_K_M on CPU through llama.cpp `b12138` with an 8,192-token context, six threads, and reasoning disabled. The model had to visit the local invoice fixture and read a heading with an amount absent from the prompt. The final direct-navigation run visited the page once, reported the correct amount of 26, and completed in 16.69 seconds during concurrent tests. It used 1,225 input and 61 output tokens across two decisions, as reported by the model endpoint. An earlier direct-navigation run completed in 9.38 seconds using 1,152 input and 61 output tokens. A separate hosted check returned HTTP 401. The Ollama service was unavailable on this host. [llama.cpp server API](https://github.com/ggml-org/llama.cpp/blob/b12138/tools/server/README.md)

The first local run used a generic command schema and failed application validation. After replacing it with per-command schemas and shortening the system prompt, input to the first decision fell from 942 to 521 tokens, and the agent reached the correct page. It then tried to act on a heading and paused. An explicit instruction to answer from visible accessibility text, bounded recovery for missing targets, and omitting target commands when no targets are observed addressed the failure in direct navigation. The final successful run's first decision used 579 input tokens. On the separate worksheet-to-Details workflow, the model reached the correct page, then proposed an unnecessary action. The workspace held it for approval after 23.02 seconds, 2,218 input tokens, and 126 output tokens. The run did not produce an answer. These small sequential smoke tests changed several variables and establish limited model reliability.

## Next experiments and boundaries

1. Run approved local or hosted models against a fixed WebArena or BrowserGym task subset and the workspace's supervision fixtures. Compare full versus bounded observations, then bounded plus on-demand vision. Record task success, action failure and approval rates, input/output tokens, and recovery after intervention. Include an explicit security suite for prompt injection and policy bypass.
2. Benchmark sampled JPEG versus rate-limited Playwright screencast across static forms, animation and high-motion pages with controlled network loss and multiple viewers. Keep the same command protocol and privacy treatment. Reconsider the transport if representative input-to-visible latency exceeds the 500 ms local p95 target or browser encode CPU dominates.
3. Test browser process failures and sustained workloads with more branches and long histories. Measure renderer working set, artifact storage, and SQLite contention. Decide whether browser workers need separate processes or remote containers before changing the lease model. A distributed workflow engine would still require an external-effect journal and recovery policy.
4. Track Playwright Chromium compatibility and the open redirect-routing and proxy-auth issues. The workspace uses the published Python API and keeps no upstream fork. If a deterministic upstream reproduction remains, contribute it without application secrets. [Playwright redirect issue](https://github.com/microsoft/playwright/issues/34994), [proxy-auth issue](https://github.com/microsoft/playwright/issues/40194).

Actual model quality on arbitrary websites, remote TLS deployment, large workloads, and multi-tenant isolation remain unmeasured. The real Chromium workflow, ownership, snapshots, and guided planner can be reproduced without model credentials. [Validation procedures](validation.md)
