---
sidebar_position: 17
title: "Session Heartbeats"
description: "A recurring prompt that re-enters your current session whenever it's idle — /heartbeat every 10m Check the deployment."
---

# Session Heartbeats (`/heartbeat`)

`/heartbeat` gives the **current session** one recurring instruction. Whenever the session is idle and the interval has elapsed, the prompt fires as a normal user turn — same conversation, same context, same prompt cache.

```
/heartbeat every 10m Check the deployment and report meaningful changes
```

Inspired by Prime-Agent's `/heartbeat`. The Hermes adaptation keeps the strict message-flow invariants: the heartbeat is injected only between turns (never mid-run), as a plain user-role message.

## Heartbeat vs cron: which one do I want?

They look similar but serve different jobs:

| | `/heartbeat` | [`hermes cron`](./cron) |
|---|---|---|
| Runs in | **This conversation** — full context, memory of the discussion | A fresh isolated session per tick |
| Survives process restart | State survives (SessionDB); gateway watches resume automatically after restart | Yes — fully durable scheduler |
| How many | One per session | Unlimited jobs |
| Best for | "Keep an eye on X *in this thread* while we work" | Standing jobs, reports, watchdogs, deliveries |

Rule of thumb: if the recurring prompt needs the conversation's context, use `/heartbeat`. If it's a self-contained job, use cron.

## Commands

| Command | What it does |
|---|---|
| `/heartbeat every <interval> <prompt>` | Set (or replace) the session's heartbeat. Intervals: `90s`, `10m`, `2h`, `1d` (minimum 60s). |
| `/heartbeat` or `/heartbeat status` | Show the heartbeat, its interval, and time to next fire. |
| `/heartbeat pause` | Stop firing without clearing. |
| `/heartbeat resume` | Resume (re-anchors the timer — no instant stale fire). |
| `/heartbeat clear` | Remove the heartbeat. |
| `/heartbeat channel every <interval> [prompt]` | Gateway-only opt-in: one persistent contextual schedule for the exact channel/topic, following the current conversation. |
| `/heartbeat channel status\|pause\|resume\|clear` | Inspect or durably control the channel schedule. |

`/hb` is an alias. Works on the CLI, the TUI / Desktop app, and gateway platforms (on Slack, use `/hermes heartbeat …`).

## Behavior details

### Persistent channel scope (gateway opt-in)

Use `/heartbeat channel every 10m` in a messaging gateway chat to give that exact
channel/topic one indefinitely persistent contextual heartbeat. An optional prompt
replaces the default instruction to review the current conversation's commitments
and goals, take only authorized useful steps, respect policy/quiet hours, avoid
duplicating cron jobs, and remain silent with `NO_REPLY` when nothing warrants a message.

`/heartbeat channel status`, `/heartbeat channel pause`, `/heartbeat channel resume`,
and `/heartbeat channel clear` address this schedule explicitly. Pause and clear
are durable; resume re-anchors the interval. There is no lifetime or tick cap.
The default `/heartbeat every 10m <prompt>` remains session-scoped and unchanged.
Channel scope is gateway-only, not a standalone CLI/TUI schedule.

The channel schedule follows the routing index's current conversation across
reset, switch, compression, and gateway restart, without copying prompts into
system context or creating a cron job. It retains the exact routing key, platform,
chat/topic/workspace, runtime profile and receiving-bot identity. Unknown ownership
or a missing adapter fails closed; it never borrows another profile's bot. A
suspended conversation waits until normal user activity establishes a current lane.
This also holds when suspension happens after a tick was admitted: the tick only
observes the lane and cannot consume suspension by creating a replacement session.

It uses the same idle poller and admission checks as session heartbeats. Active
or paused channel schedules suppress any legacy session heartbeat on that route;
setting or clearing the channel schedule also clears the current session's legacy
heartbeat. Setting a session heartbeat while a channel schedule exists is refused.
Atomic claims prevent duplicate polls from reserving the same tick. A reset/switch
before turn resolution uses the new conversation; a change during preparation or
prompt-pin persistence rejects and refunds the stale attempt for a later idle poll. A concurrent pause,
clear or replacement wins over admission and refund. Already-running turns are
not canceled by pause/clear. As with legacy heartbeats, abrupt process death after
reservation can consume that tick; missed intervals coalesce after restart.

### Shared behavior

- **Idle-only.** A heartbeat never interrupts a running turn. If the agent is busy when the tick comes due, it fires at the next idle poll. In the gateway, an idle watched session wakes proactively; no new inbound message is needed.
- **Missed ticks coalesce.** If the session was busy (or the process wasn't running) through several intervals, you get **one** heartbeat turn, not a backlog. The timer re-anchors on every fire.
- **User messages win.** A queued user message always takes priority; the heartbeat waits for the input queue to drain.
- **Owned by the surface that set it.** A heartbeat set from a messaging chat fires from the gateway and replies into that chat even while the same session is open in the TUI / Desktop app; the viewer never claims the tick.
- **Cache-safe.** The injected prompt is an ordinary user message. No system-prompt mutation, no toolset change.
- **Gateway recovery.** Startup restores active heartbeats using the current persisted conversation and thread routing, in the owning profile. Each poll retries recovery after temporary storage failures or adapter downtime; paused and cleared heartbeats and suspended conversations do not restart. No new chat message is required.
- **Persistence and conversation boundaries (default session scope).** State lives in `SessionDB.state_meta` keyed by `heartbeat:<session_id>` and follows context-compression session rotations. In the messaging gateway, leaving a conversation through reset, switch, or suspension clears its heartbeat; resuming that archived conversation does not resurrect it. Firing requires the owning process (CLI session or gateway) to be running. An already-admitted gateway tick is checked again after session resolution and before agent execution: it may follow a compression child, but cannot carry its old instruction into a reset or switched conversation.
- **Execution accounting.** The gateway reserves a due tick at adapter admission. If that exact attempt ends before entering the agent runner (including cancellation or a routing, authorization, emergency-stop, or preparation rejection), it refunds the tick unless the schedule has since changed. Once the agent runner is entered, the fire remains counted even if execution fails or is interrupted. This count is **not** proof of a successful model response or outbound delivery; abrupt process death can prevent the refund callback.
- **Quiet when there is nothing to say (gateway).** A scheduled heartbeat turn may end with a bare silence marker (`NO_REPLY` / `[SILENT]`): the gateway sends nothing, unlike a human message that returns only a marker (that still gets the visible "try again" notice). While the heartbeat works, no typing indicator, tool-progress, streamed draft or "still working" bubble is posted, and a result it does deliver is routed to the chat/topic without quoting the message that set the heartbeat. Approval prompts and failures stay visible.
- **Don't-invent-work guard.** The injected prompt tells the agent to reply briefly and stop when nothing meaningful changed, so an idle heartbeat doesn't generate busywork.

## Example

```
You: /heartbeat every 15m Check whether the CI run for PR #1234 finished; summarize the result when it does

  ♥ Heartbeat set (every 15m): Check whether the CI run for PR #1234 finished; ...

[15 minutes of you working on other things in the same session]

Hermes: [Heartbeat — recurring instruction, fires every 15m]
  💻 gh pr checks 1234   (1.2s)
  CI is still running (14/37 checks complete). Nothing to report yet.
```

When the answer stops changing, `/heartbeat clear` it — or let it keep watch.
