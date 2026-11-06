# Workspace validation

## Reproduce the automated checks

Install the application dependencies and Chromium as described in [the README](../README.md). Run the tests from the project directory. The browser tests start their own loopback website and workspace, use temporary profiles and SQLite databases, and require no model credentials. `BROWSER_EXECUTABLE_PATH` can point to an existing compatible Chromium for both the shared browser and the test viewer.

```powershell
$workspacePython = '.\.workspace-venv\Scripts\python.exe'
& $workspacePython -m pytest -q
& $workspacePython -m pytest -q tests/test_browser_workspace.py
```

The original regression suite passed 39 tests in 12.37 seconds against a copy of the starting source without project credentials or data. The task-group milestone passed 62 tests in 73.01 seconds. The subsequent adversarial audit passed all 92 tests in 135.57 seconds. Validation used Windows 11 x64, Python 3.12.14, Playwright 1.64.1, and Chromium 145.0.7632.6 for both the shared browser and the viewer. [Adversarial audit](adversarial-audit.md), [Browser and model measurements](optimization.md)

| Scenario | Automated check |
| --- | --- |
| Human changes the page during a task | Starts the agent, pauses a pending decision, acquires the browser, clicks a rendered coordinate, types a note, and returns control. The next observation contains the new page text. The obsolete plan never reaches the fixture. |
| Conflicting input and stale state | A second client cannot seize or use the lease. The same command ID performs one click. Stale revisions and released leases cannot mutate the page. |
| Takeover during Chromium navigation | A real HTTP navigation waits on a fixture barrier. Manual ownership is granted after that operation drains and the first manual command affects the same browser. |
| Sensitive operations and untrusted content | A page claiming authorization still triggers approval. Rejecting one form makes zero submissions. Approving the second exact target submits that form once. |
| Stale target recovery | A live form changes its submission destination after the approval request. The old authorization reaches neither endpoint. Resuming lets the agent observe the changed form. |
| Editing an approval | A same-target text change receives a new correlation ID. A change of target and approval of the old proposal both fail. The browser receives the revised text and the durable event output omits both values. |
| Cancel, replan, and stop | Cancel removes one proposed action. Replan creates a new proposal without submitting the form. Stop invalidates that proposal and prevents resuming the stopped run. A new instruction can start a different task. |
| Tabs, files, and profile isolation | Tabs in one browser share its cookie. A different session cannot read that cookie or download the first session's artifact. An explicitly staged file reaches the page file input. A restart preserves the cookie and download in their original branch. |
| Network policy | An allowlisted loopback fixture redirects to an unlisted loopback port. The destination receives zero requests. Workspace tests separately check authenticated Basic and Bearer access, invalid Host, and cross-site request rejection. |
| Restart and reconnect | A workspace stream reconnects over a real Uvicorn socket and reconstructs committed history. A task interrupted while waiting for a decision can resume in its restored profile. A branch ID from another session returns an error. |
| Chromium renderer crash | Injects an actual Chromium renderer crash through DevTools. The same manual controller reconnects with its existing lease, a new page revision, the saved URL, and the profile cookie. |
| Visible workspace | A second Chromium instance loads the actual UI, decodes the shared browser JPEG, and forwards scaled mouse and keyboard input. The test checks the updated pixels, confirms the agent saw the changes, and reloads the workspace. |
| Guided demonstration | A second Chromium instance completes the shipped guided demo through the UI against the shipped reservation server. The built-in planner navigates, requests help, and verifies the resulting confirmation. |

The integration suite uses deterministic decision fixtures to isolate browser and control failures. The guided demo uses the shipped planner and real browser without an external model. A separate Qwen smoke test used actual inference and Chromium. It answered a direct invoice lookup and reached the right page on a follow-link task before proposing an unnecessary action that the approval gate held. Hosted inference, credential workflows, cross-region streaming, and standardized benchmarks such as WebArena remain unverified. [Model results and token counts](optimization.md#local-model-smoke-test)

## Manual walkthrough

Start the [credential-free demo](../README.md#demonstrate-without-model-credentials) on two loopback ports. Open the workspace in a desktop browser. Use sample data in the demo.

1. Create a session and choose **Run guided demo**. Watch the URL and viewport progress from the demo landing page to **Reservation details**. Verify that **Activity** shows the agent's page actions and **Conversation** asks for manual input.
2. Choose **Take control**. Open the same workspace session in another browser tab. The second tab can observe the viewport, and the first tab retains input control. On the first tab, click the reservation input in the shared viewport, type a sample name, and select **Confirm reservation**.
3. Wait for **Reservation received** in the shared viewport. Choose **Return control**. Verify that the agent reports the confirmation and the task becomes **Completed**. Reload the workspace tab and confirm that the conversation, activity, and branch are restored.
4. Take control again. Select **Download sample receipt** in the browser and open **Files**. Download the artifact and verify that the file contains `Demo workshop reservation`. Save a checkpoint from **History**, create a branch, and verify the branch selector identifies the new branch. The restored branch uses a separate profile and a logical URL checkpoint.
5. To check recovery, pause a task before restarting the workspace process. Start it again with the same database and profile directory. Verify that the conversation and checkpoints persist. Resume from the saved branch and inspect the page before approving a fresh action. Forms or transient page state can require manual re-entry.

For a configured model, give it a task that proposes entering a sample value on the isolated reservation site. Verify that the proposal shows the action and target before the browser changes. Try **Edit action** with a different sample value, then **Reject** the updated proposal and confirm the field remains untouched. Request a new proposal, verify its details, and approve it. **Stop task** should stop further browser actions. These manual steps depend on the configured model producing a suitable proposal.

## Task groups and local models

Run the group integration tests from the workspace root.

```powershell
& $workspacePython -m pytest -q tests/test_task_groups.py
```

The tests cover simultaneous workers, independent Chromium profiles and model selection, a synchronous join, an attention join during approval, a timed-out wait that leaves work running, and replay of a submission with the same request ID. They also cover cancellation while a task is waiting for model capacity, safe failure if a pinned profile changes, and automatic scheduling of a browser-queued task after a restart. The application with test model endpoints also passed a 390 × 840 Chromium walkthrough covering the group composer, saved-group reload, live updates, and opening and approving exactly one member's form submission.

The real-model run used distinct Qwen3 0.6B and 1.7B weights through a loopback llama.cpp router, with two application inference slots. The group reached a stable result in 35.33 seconds. The 1.7B model visited its fixture page once and accurately reported that the cookie was absent. The 0.6B model made three decisions without visiting the requested invoice page. The application withheld its unsupported answer and returned **Awaiting input**. Both profiles recorded zero application queue time in this small trial. This validates routing and the blank-page safeguard. A model can still misread evidence on a page it visited. [Model and slot measurements](multitask-research.md)

## Visual latency probe

The GUI integration test records `viewer_click_to_visible_pixel_ms` in JUnit XML. The clock starts at the viewer's pointer event. After the click command returns, an animation-frame check samples the displayed JPEG until it reads the changed color. The probe includes command dispatch, capture and frame delivery, decoding, and the viewer's polling cadence. It measures display data in Chromium on loopback.

```powershell
& $workspacePython -m pytest -q tests/test_browser_workspace.py -k workspace_ui `
  --junitxml="$env:TEMP/thesus-viewer.xml" -o junit_family=xunit1
```
