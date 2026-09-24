# Aged Ticket Advisor

Automates the daily aged pending ticket review that service desk leads currently
do by hand. It pulls open incidents older than N days, works out **who is
actually blocking each one**, ranks them by an explainable attention score,
grounds a recommendation in comparable resolved incidents and KB articles, and
delivers the result as a **daily email digest** plus a **web review board**.

It is read-only with respect to ServiceNow. It never resolves, reassigns or
comments on a ticket.

> This project is entirely separate from `itsm_analytics/`. Different folder,
> different database (`sd_advisor_db`), different Vault paths, different port
> (8444 vs 8443), different service unit. Nothing in the existing audit tool is
> read, imported or modified. The one optional touchpoint is a read-only
> `SELECT` against `itsm_analytics_db` for the agent coaching column, and that
> is off by default.

---

## Why this is not just "the audit tool again"

| | Ticket Audit (existing) | Aged Ticket Advisor (this) |
|---|---|---|
| Looks at | Closed tickets | Open tickets |
| Direction | Retrospective — what happened | Prospective — what next |
| Output | Compliance score | Next-best-action + drafted text |
| Cadence | Nightly batch | Continuous, 10-minute stream tier |
| LLM calls | ~15–30 per ticket, ungrounded | 1 per ticket, grounded in retrieved evidence |
| Parsing | Substring match on `Boolean 1` | JSON schema, enum-validated |
| Runtime | ~2.4 h sequential | < 3 min, async + change-hash cache |

---

## How it works

```
                      ┌──────────────── ServiceNow (READ ONLY) ────────────────┐
                      │ incident · sys_history_line · task_sla · kb_knowledge  │
                      │ m2m_kb_task · change_request · problem · sys_user      │
                      └────────────────────────┬───────────────────────────────┘
                                               │
   TIER 1 · every 10 min ───────────────────── ▼ ──────────────────────────────
   delta sync  →  signal extraction  →  attention score  →  risk alerts
   (no LLM — pure arithmetic, this is what makes alerting near-real-time)
                                               │
   TIER 2 · hourly + pre-digest ─────────────  ▼  ─────────────────────────────
   retrieval (vector: similar resolved incidents + KB)
        → 1 structured LLM call → validated recommendation → MySQL
   (input-hash cached: an unchanged ticket costs nothing)
                                               │
   TIER 3 · on demand ───────────────────────  ▼  ─────────────────────────────
   web board · ticket drill-down · JSON API · optional SNOW push webhook
                                               │
   DELIVERY ─────────────────────────────────  ▼  ─────────────────────────────
   daily email digest (lead + per agent) · risk alert email · web UI · Grafana
```

### The signals (deterministic, no LLM, fully explainable)

| Signal | Why it matters |
|---|---|
| `idle_days` | Time since the last **meaningful agent action**. Excludes system accounts, `*.rest` integration users, SLA recalculations and the caller's own updates — otherwise the number is noise. |
| **`ball_in_court`** | AGENT / CALLER / VENDOR / CHANGE / PROBLEM / APPROVAL. Turns "40 aged tickets" into "9 that are actually ours". |
| **`caller_replied_unanswered`** | Caller responded, agent hasn't. Silent MTTR killer, and it overrides an "awaiting caller" hold reason. |
| **`dependency_resolved`** | On hold for a CHG/PRB that has **already closed**. Invisible in a manual review; often the oldest tickets on the board. |
| `followup_count` / `auto_close_candidate` | Documented chases with no reply → pre-drafted closure. |
| `sla_*` | Breach state, % consumed, projected breach time. |
| `p90_overrun` | Slow **for its own category**, so long-by-nature request types aren't permanently red. |
| `kb_available` / `kb_attached` | A KB exists and wasn't attached — a provable coaching point. |

### The attention score

Weighted 0–100 composite, weights editable at `/settings/weights` and stored in
`attention_weightage`. If enabled weights don't total 100 the scorer reverts to
defaults rather than producing a distorted ranking. Every point is attributable —
the ticket page shows the per-component breakdown.

| Component | Default |
|---|---|
| SLA jeopardy | 25 |
| Stagnation | 25 |
| Blocked but stale | 15 |
| Age beyond threshold | 10 |
| Priority | 10 |
| p90 overrun | 10 |
| Reassignment churn | 5 |

### The recommendation

One structured call returning an enum-constrained action — `RESOLVE`,
`FOLLOW_UP_CALLER`, `CHASE_VENDOR`, `REASSIGN`, `ESCALATE`, `AWAIT_DEPENDENCY`,
`CLOSE_STALE`, `NO_ACTION_NEEDED` — plus confidence, cited evidence, a drafted
work note and a drafted caller message.

`NO_ACTION_NEEDED` is a first-class answer. A tool that flags everything gets
ignored within a fortnight.

---

## Safety

- **Read-only.** No write client to ServiceNow exists in the codebase.
- **PII redaction before the prompt.** Phones, emails, card/SSN patterns, API
  keys, JWTs, private keys and `password: X` assignments are replaced with
  stable placeholders and restored in the rendered output, so drafted text still
  reads naturally while the model never sees the real value.
- **Prompt-injection defence, three layers.** Ticket text sits in a delimited
  untrusted block; structured output means injected prose can't change the
  schema; and `suggested_target_group` is validated against the real group list —
  an unknown group downgrades the action to `ESCALATE` rather than being
  actioned.
- **TLS verification on** for ServiceNow and Vault.
- **Confidence gating.** Below `llm.min_confidence_to_surface` a recommendation
  is stored but marked "needs a human look" rather than asserted.
- **Shadow mode** (`runtime.shadow_mode`, default **on**) computes, stores and
  renders everything but sends no mail.

---

## Install

```bash
cd aged_ticket_advisor
./ops/install.sh                       # deps, config, schema, prints the Vault secrets you need
```

Then:

```bash
python run.py doctor                                        # check every dependency
python run.py adduser --username you --role ADMIN --email you@corp
python run.py backfill --days 180                           # build the evidence index (once, slow)
python run.py sync --full && python run.py signals          # first pass
python run.py preview -o /tmp/digest.html                   # eyeball the email

sudo cp ops/aged-ticket-advisor.service /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl enable --now aged-ticket-advisor
```

### Vault secrets

```
secret/sd_advisor_db     <dbuser>=<dbpassword>
secret/snow_advisor      <snowuser>=<snowpassword>
secret/sd_advisor_llm    azure_endpoint=... azure_api_key=... azure_api_version=2024-10-21
secret/sd_advisor_web    secret_key=<openssl rand -base64 48>   webhook_token=<optional>
```

`secret/sd_advisor_smtp` is **not needed** on this estate — the internal relay
(`mailhost.kohler.com:25`) is anonymous, and the mailer treats a missing SMTP
secret as an open relay. Create it only if authentication is introduced later.

### Vault authentication

Use **AppRole**, not a static token — the service obtains a short-lived token
at startup, renews it on a background thread, and re-authenticates when it
hits max TTL:

```bash
vault auth enable approle

LAN_IP=$(hostname -I | awk '{print $1}')
vault write auth/approle/role/sd-advisor \
    token_policies="sd-advisor" token_ttl=1h token_max_ttl=24h \
    secret_id_ttl=0 secret_id_num_uses=0 bind_secret_id=true \
    secret_id_bound_cidrs="127.0.0.1/32,${LAN_IP}/32" \
    token_bound_cidrs="127.0.0.1/32,${LAN_IP}/32"

vault read  -field=role_id      auth/approle/role/sd-advisor/role-id
vault write -f -field=secret_id auth/approle/role/sd-advisor/secret-id
```

> **Include `127.0.0.1/32`.** CIDR restrictions match the source address as
> *Vault* sees it. Vault runs on this host, so logins arrive over loopback even
> when `VAULT_ADDR` is `https://kohlerco.com:8200`. Binding only to the LAN
> address fails with `source address "127.0.0.1" unauthorized by CIDR
> restrictions on the role`.
>
> Two further gotchas: `vault write` **replaces** the role (re-supply every
> parameter when amending it), and a `secret_id` keeps the CIDR list it was
> issued under — so **generate a new one** after changing the role.

Write those to `config/.vault_role_id` and `config/.vault_secret_id` at mode
600, then set `vault.auth_method: "approle"`. No `.vault_token` is needed —
set `vault.approle.write_token_file: true` only if external tooling expects
one. Full walkthrough in §7.4 of the deployment guide.

Use a **separate Vault token** from the audit tool, scoped to these paths only.

### ServiceNow permissions

Read on `incident`, `sys_history_line`, `task_sla`, `kb_knowledge`,
`m2m_kb_task`, `change_request`, `problem`, `sys_user`. **No write anywhere.**

---

## Commands

| Command | What it does |
|---|---|
| `run.py serve` | Web UI + scheduler (the systemd daemon) |
| `run.py web` | Web UI only |
| `run.py sync [--full]` | One sync pass |
| `run.py signals` | Recompute signals and scores |
| `run.py recommend [-n N] [--force]` | LLM pass |
| `run.py digest [--no-agents]` | Build and send digests |
| `run.py backfill [--days 180]` | Build the vector index and baselines |
| `run.py preview -o out.html` | Render the digest without sending |
| `run.py adduser ...` | Create a UI account |
| `run.py doctor` | Check every dependency and exit non-zero on failure |

## Web UI

| Route | |
|---|---|
| `/board` | Ranked review board, filterable by group, agent, blocker, risk flag, action |
| `/ticket/{n}` | Signals, score breakdown, recommendation, evidence, activity, drafts, verdict form |
| `/agents` | Per-agent rollup, with the optional audit-compliance column |
| `/accuracy` | Agreement, precision, coverage, silent breaches |
| `/settings/weights` | Tune the attention score (admin) |
| `/api/board`, `/api/ticket/{n}`, `/api/accuracy` | JSON, for Grafana and automation |
| `/healthz` | Liveness, last sync watermark, LLM reachability |
| `/webhook/snow` | Optional push trigger (shared-secret header) |

---

## Measuring accuracy

Every recommendation and every lead verdict is stored, so "is it accurate
enough" is answerable with evidence rather than opinion. `/accuracy` reports:

| Metric | Definition | Target |
|---|---|---|
| **Action agreement** | Verdicts of *agreed* or *agreed with edits*, over all verdicts | ≥ 80% |
| **Actionable precision** | Same numerator, excluding *already handled* — was flagging it justified? | ≥ 85% |
| **Review coverage** | Share of recommendations that got any verdict | high enough that the above two mean something |
| **Silent breaches** | Breached with no prior warning — recall, the metric a precision-only dashboard hides | 0 |

**Run in shadow mode for two weeks**, hit the agreement target, then set
`runtime.shadow_mode: false`.

---

## Layout

```
core/         config · logging · vault · db · redaction · context
  snow/       read-only ServiceNow clients (base, incidents, sla, knowledge)
  llm/        Azure OpenAI client · pydantic schemas · prompts
pipeline/     sync · signals · scoring · retrieval · advisor · orchestrator
delivery/     mailer · digest builder · dispatcher · HTML templates
web/          FastAPI app · auth · Jinja templates · CSS
scheduler/    APScheduler job definitions
ops/          index builder · systemd unit · install script
db/schema.sql 11 tables + the v_current_board view
tests/        74 tests over the signal, scoring and redaction layers
```

## Tests

```bash
python -m pytest tests -q
```

The signal layer is pure functions over plain dicts — no DB, no network, no
model — specifically so the numbers a lead is asked to trust are unit-testable.

---

## Before go-live — agree these with the SD leads

1. **What counts as "pending"** and what counts as "no action taken". Everything
   downstream depends on it. The current definition of a meaningful agent action
   is in `pipeline/signals.py` (`AGENT_ACTION_FIELDS`, `SYSTEM_NOISE_FIELDS`).
2. **The system-account list** in `servicenow.system_accounts` — your logs show
   `cac.rest`, `koce718`, `koceqc6`; anything that auto-updates tickets must be
   listed or the idle clock lies.
3. **Aged threshold and digest times** per group. Kohler spans UK, US and China;
   `scheduler.digests` takes one cron entry per timezone.
4. **Who gets the lead digest**, and whether agents get their own.

## Known limitations

- Similar-incident quality depends on close notes. Tickets closed with an empty
  or one-word note are excluded from the index — they aren't evidence.
- The first `backfill` over 180 days of resolved incidents is slow and costs
  embedding tokens. It is a one-off; nightly runs only embed the delta.
- Non-English tickets (your volume includes Chinese) are embedded and reasoned
  over in their original language. Retrieval quality there is untested and
  should be spot-checked during shadow mode.
- Interactive approve/snooze happens in the web UI, not in the email — the email
  links through. That was the chosen trade-off for not depending on Teams.
