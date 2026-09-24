#!/usr/bin/env python3
"""Generates the Aged Ticket Advisor deployment guide as a Word document.

Kept in the repo so the guide can be regenerated whenever configuration keys
or steps change, rather than drifting away from the code as a hand-edited
binary.

    python docs/generate_deployment_guide.py

Requires python-docx (pip install python-docx).
"""

import datetime
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _docx_builder import (ACCENT, CRITICAL, GOOD, INK, INK2, MUTED, Guide)
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.shared import Pt

OUT = Path(__file__).resolve().parent / 'Aged_Ticket_Advisor_Deployment_Guide.docx'


# ===========================================================================
# document content
# ===========================================================================

TODAY = datetime.date.today().strftime('%d %B %Y')
g = Guide(footer_label='Aged Ticket Advisor - Deployment Guide',
          header_label='Internal - Service Desk Engineering')
d = g.doc

# ---------------------------------------------------------------- cover ----
title = d.add_paragraph()
title.alignment = WD_ALIGN_PARAGRAPH.LEFT
run = title.add_run('Aged Ticket Advisor')
run.font.size = Pt(32)
run.font.bold = True
run.font.color.rgb = INK

sub = d.add_paragraph()
run = sub.add_run('Deployment & Configuration Guide')
run.font.size = Pt(17)
run.font.color.rgb = ACCENT

sub2 = d.add_paragraph()
run = sub2.add_run('Automated aged pending ticket review for the Service Desk\n'
                   'Email digest + web review board  ·  read-only against ServiceNow')
run.font.size = Pt(11)
run.font.color.rgb = INK2

d.add_paragraph()

g.table(
    ['Field', 'Value'],
    [
        ['Document', 'Aged Ticket Advisor — Deployment & Configuration Guide'],
        ['Version', '1.0'],
        ['Date', TODAY],
        ['Applies to', 'aged_ticket_advisor (new deployment)'],
        ['Target host', 'The existing GenAI application server (same host as itsm_analytics)'],
        ['Target path', '/genai/etc/scripts/aged_ticket_advisor'],
        ['Database', 'sd_advisor_db (new, MySQL)'],
        ['Service port', '8444/tcp (HTTPS via reverse proxy)'],
        ['Existing systems touched', 'None — see Section 1.2'],
        ['Audience', 'Platform engineer performing the install'],
        ['Estimated effort', '2–3 hours, plus a 1–4 hour unattended backfill'],
    ],
    widths=[4.6, 11.8])

d.add_paragraph()
g.callout('crit', 'Read before you start.',
          'Section 1.2 states exactly what this deployment does and does not touch. The '
          'existing itsm_analytics Ticket Audit tool must keep running throughout. Nothing '
          'in this guide modifies its files, its database, its Vault token, its cron entries '
          'or its port. If any step appears to require changing the existing tool, stop and '
          'escalate — it does not.')

# ------------------------------------------------------------- contents ----
d.add_page_break()
g.doc.add_heading('Contents', 1)
g.p('Right-click anywhere in the field below and choose "Update Field" in Word to populate '
    'the table of contents with page numbers.', italic=True, size=9.5, color=MUTED)

toc_para = d.add_paragraph()
fld_begin = OxmlElement('w:fldChar')
fld_begin.set(qn('w:fldCharType'), 'begin')
instr = OxmlElement('w:instrText')
instr.set(qn('xml:space'), 'preserve')
instr.text = 'TOC \\o "1-3" \\h \\z \\u'
fld_sep = OxmlElement('w:fldChar')
fld_sep.set(qn('w:fldCharType'), 'separate')
placeholder = OxmlElement('w:t')
placeholder.text = 'Table of contents — press F9 in Word to populate.'
fld_end = OxmlElement('w:fldChar')
fld_end.set(qn('w:fldCharType'), 'end')
r = toc_para.add_run()
for element in (fld_begin, instr, fld_sep, placeholder, fld_end):
    r._r.append(element)

d.add_paragraph()
g.table(['Section', 'Title', 'Typical duration'], [
    ['1', 'Overview and isolation guarantee', '10 min read'],
    ['2', 'Prerequisites and access required', '—'],
    ['3', 'Step 1 — Discover your environment values', '20 min'],
    ['4', 'Step 2 — Deploy the code', '10 min'],
    ['5', 'Step 3 — Python dependencies', '10 min'],
    ['6', 'Step 4 — MySQL database configuration', '20 min'],
    ['7', 'Step 5 — HashiCorp Vault configuration', '30 min'],
    ['8', 'Step 6 — ServiceNow configuration', '30 min'],
    ['9', 'Step 7 — Azure OpenAI configuration', '20 min'],
    ['10', 'Step 8 — SMTP configuration', '15 min'],
    ['11', 'Step 9 — conf.json, field by field', '30 min'],
    ['12', 'Step 10 — Verification with run.py doctor', '10 min'],
    ['13', 'Step 11 — Create UI accounts', '5 min'],
    ['14', 'Step 12 — Build the evidence index (backfill)', '1–4 h unattended'],
    ['15', 'Step 13 — First manual pipeline run', '20 min'],
    ['16', 'Step 14 — Install the systemd service', '15 min'],
    ['17', 'Step 15 — Network, firewall and TLS', '20 min'],
    ['18', 'Step 16 — Grafana dashboard (optional)', '20 min'],
    ['19', 'Step 17 — Shadow mode operation', '2 weeks'],
    ['20', 'Step 18 — Go live', '10 min'],
    ['21', 'Operations runbook', 'reference'],
    ['22', 'Troubleshooting', 'reference'],
    ['23', 'Rollback and uninstall', 'reference'],
    ['24', 'Appendices A–F', 'reference'],
], widths=[1.8, 11.0, 3.6])


# =========================================================== SECTION 1 =====
g.h1('1. Overview and isolation guarantee')

g.h2('1.1 What this deploys')
g.p('The Aged Ticket Advisor replaces the manual daily review in which leads read through '
    'every aged pending ticket for each agent. It runs continuously, and produces:')
g.bullet('a ranked review board of open tickets older than a configurable threshold '
         '(5 days by default), scored 0–100 by an explainable "attention score";')
g.bullet('a "who is actually blocking this" classification per ticket — us, the caller, '
         'a vendor, a change, a problem or an approval;')
g.bullet('a recommended next action per ticket, grounded in comparable resolved incidents '
         'and knowledge articles, with a ready-to-paste work note and caller message;')
g.bullet('a daily email digest to leads, and optionally one per agent covering only their '
         'own queue;')
g.bullet('immediate email alerts when a ticket newly breaches SLA, becomes critically '
         'stale, has a caller waiting on a reply, or is held for a dependency that has '
         'already closed;')
g.bullet('an accuracy dashboard driven by the verdicts leads record, so "is this good '
         'enough to trust" is answered with evidence.')

g.h2('1.2 What it does NOT touch')
g.p('This is a parallel deployment. Every resource it uses is new and separately named:')

g.table(['Resource', 'Existing Ticket Audit tool', 'Aged Ticket Advisor (new)'], [
    ['Install directory', '/genai/etc/scripts/itsm_analytics', '/genai/etc/scripts/aged_ticket_advisor'],
    ['MySQL database', 'itsm_analytics_db', 'sd_advisor_db'],
    ['MySQL user', '(existing)', 'sd_advisor (new)'],
    ['Vault credentials', 'itsm_analytics/config/.vault_token (static)',
     'aged_ticket_advisor/config/.vault_role_id + .vault_secret_id (AppRole)'],
    ['Vault secret paths', 'itsm_analytics_db, <itsm_tag>', 'sd_advisor_* (new paths)'],
    ['TCP port', '8443 (Flask front end)', '8444 (FastAPI)'],
    ['Scheduling', 'cron 04:00 daily', 'systemd long-running service'],
    ['Log directory', 'itsm_analytics/daily_run_logs', 'aged_ticket_advisor/logs'],
    ['ServiceNow writes', 'none', 'none'],
], widths=[3.6, 6.4, 6.4], code_cols=(1, 2))

g.callout('info', 'The only shared resources',
          'the MySQL server process, the Vault server, the Azure OpenAI subscription, the '
          'ServiceNow instance and the Python virtual environment. All are used read-only or '
          'additively. If you prefer complete separation, Section 5.2 covers creating a '
          'dedicated virtual environment instead of reusing genai_venv_1.')

g.h2('1.3 Optional read-only link to the audit tool')
g.p('The per-agent view can show each agent\'s 30-day average compliance score from the '
    'existing itsm_analytics_db, which turns "this agent has 9 aged tickets" into "this '
    'agent has 9 aged tickets and a 52% compliance average". This is:')
g.bullet('disabled by default (audit_database.enabled = false);')
g.bullet('a read-only SELECT on a separate connection using a separate, read-only MySQL grant;')
g.bullet('fail-soft — if the query errors the column is simply omitted and the service '
         'carries on.')
g.p('Section 6.5 covers enabling it. If your change process would rather not have any link '
    'at all, leave it disabled; nothing else depends on it.')

g.h2('1.4 Architecture at a glance')
g.code("""ServiceNow (READ ONLY)
  incident · sys_history_line · task_sla · kb_knowledge
  m2m_kb_task · change_request · problem · sys_user
        |
        v
  TIER 1 - every 10 minutes  (no LLM, pure arithmetic)
    delta sync -> signal extraction -> attention score -> risk alert email
        |
        v
  TIER 2 - hourly and before each digest
    vector retrieval (similar resolved incidents + KB)
      -> ONE structured Azure OpenAI call
      -> validated recommendation -> MySQL
    (input-hash cached: an unchanged ticket costs nothing)
        |
        v
  TIER 3 - on demand
    web board :8444 · ticket drill-down · JSON API · optional SNOW webhook
        |
        v
  DELIVERY
    daily lead digest · per-agent digest · risk alerts · Grafana""")


# =========================================================== SECTION 2 =====
g.h1('2. Prerequisites and access required')

g.h2('2.1 Server prerequisites')
g.table(['Requirement', 'Minimum', 'How to check'], [
    ['OS', 'RHEL/CentOS 8+ or equivalent Linux', 'cat /etc/os-release'],
    ['Python', '3.9 or newer', '/genai/etc/scripts/genai_venv_1/bin/python --version'],
    ['MySQL / MariaDB', '5.7+ / 10.3+ (JSON column support)', 'mysql --version'],
    ['HashiCorp Vault', 'running and unsealed, KV v2 enabled', 'vault status'],
    ['Disk free', '5 GB (vectors, logs, signal history)', 'df -h /genai'],
    ['RAM', '2 GB free for this service', 'free -g'],
    ['systemd', 'present', 'systemctl --version'],
    ['Outbound HTTPS', 'to ServiceNow and Azure OpenAI', 'Section 17.1'],
], widths=[3.6, 6.0, 6.8], code_cols=(2,))

g.h2('2.2 Access you will need')
g.table(['System', 'Access level', 'Used for'], [
    ['Linux host', 'sudo, or write access to /genai/etc/scripts plus systemctl rights',
     'Deploy code, install the service unit'],
    ['MySQL', 'An account that can CREATE DATABASE and GRANT',
     'Create sd_advisor_db and its user (once)'],
    ['Vault', 'A token that can write to secret/sd_advisor_*, create policies, and enable '
              'and configure the approle auth method',
     'Store credentials and set up AppRole authentication'],
    ['ServiceNow', 'Admin or an account able to create an integration user and assign roles',
     'Create the read-only integration account'],
    ['Azure OpenAI', 'Access to the resource, or the endpoint/key from whoever owns it',
     'Chat and embedding deployments'],
    ['SMTP / Exchange', 'Relay permission for the sending address (no credentials needed — '
                        'mailhost.kohler.com:25 is anonymous)',
     'Send digests'],
    ['Firewall / network', 'Ability to open 8444 to the lead user population',
     'Reach the web board'],
], widths=[3.2, 6.6, 6.6])

g.h2('2.3 Decisions to confirm with the Service Desk leads before you start')
g.callout('warn', 'Do not skip this.',
          'These four answers change configuration values. Getting the system-account list '
          'wrong in particular makes the idle clock report nonsense, because an automated '
          'update will look like an agent working the ticket.')
g.table(['#', 'Question', 'Config key it sets', 'Suggested default'], [
    ['1', 'After how many days is a ticket "aged"?', 'thresholds.aged_after_days', '5'],
    ['2', 'Which assignment groups are in scope?', 'servicenow.assignment_groups', '["Service Desk"]'],
    ['3', 'Which ServiceNow accounts are automated, not people?', 'servicenow.system_accounts',
     'See Section 3.4 — your data shows cac.rest and similar'],
    ['4', 'What time should each region\'s digest land, and who receives it?',
     'scheduler.digests, advisor_user table', '07:30 local, weekdays'],
], widths=[0.9, 6.0, 5.2, 4.3], code_cols=(2,))

g.h2('2.4 Files you should have')
g.p('The deployment package is the aged_ticket_advisor directory. Confirm it contains:')
g.code("""aged_ticket_advisor/
├── run.py                      single entry point for every mode
├── requirements.txt
├── README.md
├── config/conf.sample.json     template you will copy to conf.json
├── db/schema.sql               11 tables + the v_current_board view
├── core/                       config, logging, vault, db, redaction, context
│   ├── snow/                   read-only ServiceNow clients
│   └── llm/                    Azure OpenAI client, schemas, prompts
├── pipeline/                   sync, signals, scoring, retrieval, advisor, orchestrator
├── delivery/                   mailer, digest builder, dispatcher, email templates
├── web/                        FastAPI app, auth, Jinja templates, CSS
├── scheduler/jobs.py           APScheduler job definitions
├── ops/                        index builder, systemd unit, install.sh
├── docs/                       this guide
└── tests/                      74 unit tests""")


# =========================================================== SECTION 3 =====
g.h1('3. Step 1 — Discover your environment values')
g.p('Most values you need are already present in the existing deployment. Collect them '
    'first and write them into the worksheet in Appendix A; every later step refers back '
    'to it.')

g.h2('3.1 Values from the existing configuration file')
g.code("""cat /genai/etc/scripts/itsm_analytics/config/qa_conf.json""",
       caption='Run on the application server')
g.p('This yields the Vault URL and the MySQL host. A typical result:')
g.code("""{
    "db_host": "localhost",
    "vault_url": "https://127.0.0.1:8200",
    "llm_url": "https://10.20.53.10:8000"
}""")
g.rich([('Record ', {}), ('db_host', {'code': True}), ('. Ignore ', {}),
        ('llm_url', {'code': True}),
        (' — it is a legacy value the audit tool no longer uses. Treat ', {}),
        ('vault_url', {'code': True}), (' with care; see below.', {})])

g.callout('crit', 'Do not copy the audit tool\'s vault_url as-is.',
          'It reads https://127.0.0.1:8200, which works there only because that tool passes '
          'verify=False and skips certificate checking altogether. The advisor verifies TLS, '
          'so the URL must use a name the Vault certificate is actually issued for. An IP '
          'that is not listed in the certificate\'s SANs fails at startup with '
          '"certificate verify failed: IP address mismatch".')

g.p('Confirm which names the certificate covers:')
g.code("""# What Vault actually serves
echo | openssl s_client -connect 127.0.0.1:8200 2>/dev/null \\
  | openssl x509 -noout -subject -ext subjectAltName

# Or inspect the certificate file directly
openssl x509 -in /opt/vault/tls/tls.crt -noout -subject -ext subjectAltName""")
g.p('Use one of the names listed in the output as vault.url. On this estate that is '
    'kohlerco.com, which is also what the existing startup_services.sh sets VAULT_ADDR to.')

g.h2('3.2 ServiceNow URL and Vault secret path')
g.p('The audit tool stores these in the database rather than a file. Query them:')
g.code("""mysql -u <admin> -p itsm_analytics_db -e "
  SELECT c.customer_name,
         i.itsm_url,
         i.itsm_tag       AS vault_path_for_snow_creds,
         i.assignment_groups
    FROM itsm_configuration_table i
    JOIN customer_master_table c ON c.customer_id = i.customer_id;"
""", caption='Read-only query against the existing database')
g.callout('info', 'What you get.',
          'itsm_url is your ServiceNow instance URL. itsm_tag is the Vault path where the '
          'existing ServiceNow credentials live — useful to know even though you will create '
          'a separate secret for the advisor. assignment_groups confirms the in-scope queues.')

g.h3('3.2.1 Confirm the assignment group names before you go further')
g.p('This single setting decides what the whole service can see, and getting it wrong costs '
    'more time than anything else in this guide. The names must match sys_user_group.name '
    'character for character — "Service Desk" will not find "IT Service Desk", and the '
    'failure is silent.')
g.code("""python ops/find_groups.py "Service Desk" """,
       caption='Lists matching groups with their open and 30-day-resolved volumes')
g.p('Paste the names it prints into servicenow.assignment_groups exactly as shown, then '
    'verify with:')
g.code("""python run.py doctor | grep "snow groups" """)
g.callout('warn', 'Do not shorten or tidy the names.',
          'The advisor scopes queries with assignment_group.nameIN<names>. Because '
          'assignment_group stores a sys_id rather than text, the dot-walk to .name is what '
          'makes a human-readable name work at all — and it is an exact comparison. A name '
          'that does not exist contributes nothing and produces no warning from ServiceNow.')

g.h2('3.3 Azure OpenAI endpoint and API version')
g.code("""sudo cat /genai/etc/scripts/itsm_analytics/config/.oai_config.json""",
       caption='Contains the existing Azure OpenAI settings')
g.p('Record azure_endpoint, azure_api_version and azure_deployment. You will normally reuse '
    'the same Azure resource but must confirm an embedding deployment exists as well — see '
    'Section 9.2.')
g.callout('warn', 'Handle this file carefully.',
          'It contains a live API key. Do not paste its contents into tickets, chat or this '
          'document. You only need the endpoint, the API version and the deployment names; '
          'the key itself goes straight into Vault.')

g.h2('3.4 Identify the automated (non-human) ServiceNow accounts')
g.p('This is the single most important discovery step. Any account that updates tickets '
    'automatically must be classified as a system account, otherwise its updates reset the '
    '"time since an agent last touched this" clock and the whole board becomes misleading.')
g.code("""mysql -u <admin> -p itsm_analytics_db -e "
  SELECT created_by, COUNT(*) AS tickets
    FROM incident_master_table
   WHERE created_on >= DATE_SUB(NOW(), INTERVAL 60 DAY)
   GROUP BY created_by
   ORDER BY tickets DESC
   LIMIT 30;"
""", caption='Most frequent ticket-creating accounts over the last 60 days')
g.p('Review the list with the Service Desk leads and classify each one. In the sample data '
    'reviewed for this build, the following appeared as high-volume creators and are almost '
    'certainly integrations rather than people:')
g.table(['Account', 'Appears to be', 'Action'], [
    ['cac.rest', 'REST integration', 'Add to system_accounts'],
    ['koce718', 'Confirm with leads — may be a person', 'Ask before adding'],
    ['koceqc6', 'Confirm with leads — may be a person', 'Ask before adding'],
    ['system / guest / admin', 'Platform accounts', 'Already handled automatically'],
    ['anything ending .rest', 'Integration user', 'Already handled automatically by pattern'],
], widths=[3.4, 6.4, 6.6], code_cols=(0,))
g.callout('good', 'Built-in safety net.',
          'Accounts named system, guest or admin, and any username ending in ".rest", are '
          'treated as automated regardless of configuration. The system_accounts list is for '
          'everything else — service accounts with ordinary-looking names.')

g.h2('3.5 Map your SLA definitions')

g.h3('The two tables, and how they join')
g.p('ServiceNow separates the SLA rule from its application to a ticket, and the distinction '
    'matters here:')
g.table(['Table', 'What it holds', 'Rows'], [
    ['contract_sla', 'The DEFINITION — the rule. "Priority 3 (Medium) Resolution", 2 days, '
                     'applies to incidents. Configured by administrators',
     'One per SLA definition — a dozen or so'],
    ['task_sla', 'The INSTANCE — that rule attached to one ticket, with the live timing: '
                 'stage, percentage, has_breached, business_time_left, planned_end_time',
     'One per ticket per applicable SLA'],
], widths=[3.0, 9.6, 3.8], code_cols=(0,))

g.rich([('They join on ', {}), ('task_sla.sla', {'code': True}),
        (', a reference field pointing at ', {}), ('contract_sla.sys_id', {'code': True}),
        ('. Because the advisor queries task_sla with ', {}),
        ('sysparm_display_value=all', {'code': True}),
        (', that field arrives as ', {}),
        ('{"display_value": <definition name>, "value": <sys_id>}', {'code': True}),
        (' — so both halves of the join are present in a single call and no second lookup '
         'is needed at scoring time.', {})])

g.h3('Why the mapping has to be explicit')
g.p('The advisor needs to know which definitions represent the customer-facing resolution '
    'commitment, because that is what the SLA component of the attention score is built on — '
    'and at weight 25 it is the joint-largest ingredient. There is no reliable out-of-box '
    'field on contract_sla that distinguishes a response target from a resolution target; '
    'most instances encode it in the name.')
g.p('Matching words in the name therefore works, but it fails silently when a definition is '
    'named unconventionally. So the mapping is declared in configuration, keyed on sys_id, '
    'with name-matching retained only as a fallback for definitions added later.')

g.h3('List your definitions')
g.code("""SNOW='https://<instance>.service-now.com'; CRED='<user>:<pass>'

curl -s -u "$CRED" "$SNOW/api/now/table/contract_sla\\
?sysparm_query=collection=incident^active=true\\
&sysparm_fields=sys_id,name,type,duration\\
&sysparm_display_value=true" | python3 -m json.tool""")

g.table(['Kind', 'Meaning', 'Effect on scoring'], [
    ['RESOLUTION', 'The customer-facing commitment', 'Drives sla_breached and the SLA '
                                                     'component of the attention score'],
    ['RESPONSE', 'Time to first meaningful contact', 'Consulted only if no resolution SLA '
                                                     'exists on the ticket'],
    ['VENDOR', 'A third-party target', 'Reported separately as a VENDOR_SLA_BREACHED risk '
                                       'flag; never counted as our breach'],
    ['IGNORE', 'Excluded entirely', 'Dropped before any calculation'],
    ['OTHER', 'Unrecognised', 'Used only as a last resort when nothing better is attached'],
], widths=[2.6, 5.4, 8.4], code_cols=(0,))

g.h3('The type filter')
g.rich([('contract_sla.type', {'code': True}),
        (' distinguishes SLA from OLA and Underpinning Contract. Only the types listed in ', {}),
        ('sla.include_types', {'code': True}),
        (' reach the scorer, so an internal OLA overrun is never reported as a '
         'customer-facing breach. This is enforced in the query itself, by dot-walking '
         'to the referenced definition:', {})])
g.code("""task.number=INC2838464^sla.typeINSLA""")
g.callout('info', 'On the Kohler instance all 13 incident SLA definitions are type SLA.',
          'The filter changes nothing today. It is configured as a guard so that adding an '
          'OLA later cannot quietly distort the board.')

g.h3('Verify the mapping')
g.p('After conf.json is in place (Section 11), confirm every definition resolves as intended:')
g.code("""python run.py sla-map""")
g.code("""KIND        NAME                                        TYPE   DURATION     SOURCE
---------------------------------------------------------------------------------
IGNORE      ITSM IAR SLA                                SLA    1 Day        config:sys_id
RESOLUTION  P1 (Major Incident) Resolution              SLA    4 Hours      config:sys_id
RESOLUTION  Priority 1 (Critical) Resolution            SLA    4 Hours      config:sys_id
RESOLUTION  Priority 2 (High) Resolution                SLA    6 Hours      config:sys_id
RESOLUTION  Priority 3 (Medium) Resolution              SLA    2 Days       config:sys_id
RESOLUTION  Priority 4 (Low) Resolution                 SLA    3 Days       config:sys_id
RESPONSE    Priority 1(Critical) Incident Response      SLA    15 Minutes   config:sys_id
RESPONSE    Priority 1(Critical) Response               SLA    15 Minutes   config:sys_id
RESPONSE    Priority 2 (Critical) Incident Response     SLA    30 Minutes   config:sys_id
RESPONSE    Priority 2 (High) Response                  SLA    30 Minutes   config:sys_id
RESPONSE    Priority 3 (Medium) Response                SLA    4 Hours      config:sys_id
RESPONSE    Priority 4 (Low) Response                   SLA    1 Day        config:sys_id
VENDOR      Vendor Resolution                           SLA    10 Days      config:sys_id

Totals: IGNORE=1, RESOLUTION=5, RESPONSE=6, VENDOR=1
Types counted: SLA

All definitions are explicitly configured.""")

g.p('The SOURCE column is the thing to read. Any row showing name-token or unmatched is '
    'relying on the fallback and should be added to the map — the command prints a ready-to-'
    'paste config line for each, and exits with status 2 so it can gate a deployment pipeline.')

g.callout('warn', 'Re-run sla-map whenever SLA definitions change.',
          'A new definition added in ServiceNow will fall through to name matching. That is '
          'usually correct, but it is exactly the case worth checking rather than assuming.')


# =========================================================== SECTION 4 =====
g.h1('4. Step 2 — Deploy the code')

g.h2('4.1 Copy the project to the server')
g.code("""# From your workstation (adjust the hostname)
scp -r aged_ticket_advisor <you>@<appserver>:/tmp/

# On the application server
sudo mv /tmp/aged_ticket_advisor /genai/etc/scripts/
cd /genai/etc/scripts/aged_ticket_advisor""")

g.h2('4.2 Ownership and permissions')
g.p('Run the service as the same unprivileged account that owns the existing GenAI scripts. '
    'Confirm which that is, then apply ownership:')
g.code("""# Confirm the owning account
stat -c '%U:%G' /genai/etc/scripts/itsm_analytics

# Apply the same ownership (substitute if different)
sudo chown -R genai:genai /genai/etc/scripts/aged_ticket_advisor

# Directory permissions
sudo chmod 750 /genai/etc/scripts/aged_ticket_advisor
sudo chmod 700 /genai/etc/scripts/aged_ticket_advisor/config
sudo chmod 700 /genai/etc/scripts/aged_ticket_advisor/logs
sudo chmod +x  /genai/etc/scripts/aged_ticket_advisor/ops/install.sh""")

g.callout('crit', 'Never run this service as root.',
          'It holds credentials for ServiceNow, MySQL, Azure OpenAI and SMTP in memory. The '
          'systemd unit in Section 16 sets User=genai and several hardening directives; do '
          'not remove them.')

g.h2('4.3 Guided install script (optional)')
g.rich([('The repository includes ', {}), ('ops/install.sh', {'code': True}),
        (', which performs Sections 5, 6.2 and 11.1 interactively and prints the Vault '
         'commands you need. It is idempotent. If you prefer to follow each step manually — '
         'recommended for a first production install so you understand each change — skip it '
         'and continue with Section 5.', {})])
g.code("""./ops/install.sh""", caption='Optional guided path')


# =========================================================== SECTION 5 =====
g.h1('5. Step 3 — Python dependencies')

g.h2('5.1 Option A — reuse the existing virtual environment (default)')
g.p('Simplest, and matches how the existing tool is deployed. The advisor adds FastAPI, '
    'uvicorn, APScheduler and a few small libraries; it pins no version that conflicts with '
    'what the audit tool already uses.')
g.code("""source /genai/etc/scripts/genai_venv_1/bin/activate
cd /genai/etc/scripts/aged_ticket_advisor

pip install --upgrade pip
pip install -r requirements.txt

python -c "import fastapi, uvicorn, apscheduler, openai, pymysql, hvac, numpy, jinja2; \\
           print('all imports OK')"
deactivate""")

g.callout('warn', 'Change-control note.',
          'Installing into the shared venv technically alters an environment the existing '
          'tool also uses. The package set was chosen to be additive, but if your change '
          'process requires zero risk to the running audit tool, use Option B instead.')

g.h2('5.2 Option B — dedicated virtual environment (maximum isolation)')
g.code("""python3 -m venv /genai/etc/scripts/ata_venv
source /genai/etc/scripts/ata_venv/bin/activate

cd /genai/etc/scripts/aged_ticket_advisor
pip install --upgrade pip
pip install -r requirements.txt
deactivate""")
g.rich([('If you choose Option B, change the ', {}), ('ExecStart', {'code': True}),
        (' and ', {}), ('ExecStartPre', {'code': True}),
        (' lines in the systemd unit (Section 16) to point at ', {}),
        ('/genai/etc/scripts/ata_venv/bin/python', {'code': True}), ('.', {})])

g.h2('5.3 Run the unit tests')
g.p('The signal and scoring layers are pure functions with no external dependencies, so the '
    'test suite runs before any credential is configured. A failure here means the code did '
    'not copy correctly.')
g.code("""source /genai/etc/scripts/genai_venv_1/bin/activate
cd /genai/etc/scripts/aged_ticket_advisor
python -m pytest tests -q

# Expected:
# ........................................................................ [ 97%]
# ..                                                                       [100%]
# 74 passed""")


# =========================================================== SECTION 6 =====
g.h1('6. Step 4 — MySQL database configuration')

g.h2('6.1 Create the database user')
g.p('Generate a strong password and keep it to hand — it goes straight into Vault in '
    'Section 7 and is never written to a configuration file.')
g.code("""# Generate a password
openssl rand -base64 32""")

g.code("""-- Connect as an administrative MySQL account
CREATE USER IF NOT EXISTS 'sd_advisor'@'localhost'
  IDENTIFIED BY '<PASTE_GENERATED_PASSWORD>';

-- Full rights on its own database only
GRANT SELECT, INSERT, UPDATE, DELETE, CREATE, ALTER, INDEX, DROP, REFERENCES
  ON sd_advisor_db.* TO 'sd_advisor'@'localhost';

FLUSH PRIVILEGES;""", caption='Run as a MySQL administrator')

g.h2('6.2 Apply the schema')
g.code("""cd /genai/etc/scripts/aged_ticket_advisor
mysql -u <admin> -p < db/schema.sql""")
g.p('This creates the database if absent, eleven tables, the default attention weights and '
    'the v_current_board view. It is written to be safely re-runnable.')

g.h3('Objects created')
g.table(['Object', 'Purpose'], [
    ['watched_ticket', 'Current snapshot of every tracked open incident'],
    ['ticket_signal', 'Append-only computed signals and attention score — the fastest-growing table'],
    ['recommendation', 'LLM output, keyed on an input hash so unchanged tickets are not re-analysed'],
    ['recommendation_feedback', 'Lead verdicts — the basis of every accuracy metric'],
    ['suppression', 'Snoozes, so a handled ticket stops appearing'],
    ['attention_weightage', 'Tunable score weights, editable from the UI'],
    ['digest_run', 'Delivery log — proves whether a digest was sent'],
    ['alert_log', 'Alert de-duplication, one alert per ticket per type per day'],
    ['embedding_store', 'Vector index for similar incidents and KB articles'],
    ['resolution_stat', 'p50/p90 resolution baselines per category'],
    ['advisor_user', 'Web UI accounts'],
    ['v_current_board', 'View joining the latest signal and recommendation per ticket'],
], widths=[5.0, 11.4], code_cols=(0,))

g.h2('6.3 Verify')
g.code("""mysql -u sd_advisor -p sd_advisor_db -e "
  SHOW TABLES;
  SELECT component, weightage FROM attention_weightage ORDER BY weightage DESC;"
""")
g.p('You should see eleven tables plus the view, and seven weight rows totalling 100.')

g.h2('6.4 Character set check')
g.p('Your ticket data includes non-Latin text (the sample data contains Chinese-language '
    'incidents). The schema declares utf8mb4 explicitly; confirm the server did not override it:')
g.code("""mysql -u sd_advisor -p -e "
  SELECT default_character_set_name, default_collation_name
    FROM information_schema.SCHEMATA
   WHERE schema_name = 'sd_advisor_db';"

-- Expect: utf8mb4 / utf8mb4_unicode_ci""")

g.h2('6.5 Optional — read-only grant for the coaching column')
g.p('Only if you intend to enable the agent compliance column described in Section 1.3:')
g.code("""-- Read-only, and only the two tables the feature actually reads
GRANT SELECT ON itsm_analytics_db.incident_master_table     TO 'sd_advisor'@'localhost';
GRANT SELECT ON itsm_analytics_db.incident_compliance_table TO 'sd_advisor'@'localhost';
FLUSH PRIVILEGES;""")
g.callout('info', 'Then set in conf.json:',
          'audit_database.enabled = true, and audit_database.vault_path to the same Vault '
          'path as the advisor\'s own database credentials (the same MySQL user is used, now '
          'carrying the two extra SELECT grants).')


# =========================================================== SECTION 7 =====
g.h1('7. Step 5 — HashiCorp Vault configuration')

g.h2('7.1 Confirm Vault is reachable and unsealed')
g.code("""export VAULT_ADDR='https://kohlerco.com:8200'
export VAULT_CACERT='/opt/vault/tls/tls.crt'

vault status
# Sealed must read "false"

# If it is sealed, the existing startup script handles unsealing:
sudo /genai/etc/scripts/itsm_analytics/startup_services.sh""")

g.h2('7.2 Create a dedicated policy')
g.p('The advisor must not be able to read the audit tool\'s secrets, and vice versa. Create '
    'a policy scoped to its own paths only:')
g.code("""cat > /tmp/sd_advisor_policy.hcl <<'EOF'
# Aged Ticket Advisor - read-only access to its own secrets

path "secret/data/sd_advisor_db"   { capabilities = ["read"] }
path "secret/data/sd_advisor_llm"  { capabilities = ["read"] }
path "secret/data/sd_advisor_smtp" { capabilities = ["read"] }
path "secret/data/sd_advisor_web"  { capabilities = ["read"] }
path "secret/data/snow_advisor"    { capabilities = ["read"] }

path "secret/metadata/sd_advisor_*" { capabilities = ["read", "list"] }
EOF

vault policy write sd-advisor /tmp/sd_advisor_policy.hcl
shred -u /tmp/sd_advisor_policy.hcl""")

g.callout('warn', 'KV mount path.',
          'The policy above assumes the KV v2 engine is mounted at secret/. Confirm with '
          '"vault secrets list". If it is mounted elsewhere, adjust both the policy paths and '
          'the path arguments in the commands below.')

g.h2('7.3 Write the secrets')
g.p('Note the format difference: the database and ServiceNow secrets are stored as a single '
    'username=password pair (matching how the existing estate stores them), while the others '
    'use explicit named keys.')

g.code("""# 1. MySQL credentials  --  single username=password pair
vault kv put secret/sd_advisor_db \\
    sd_advisor='<MYSQL_PASSWORD_FROM_SECTION_6.1>'

# 2. ServiceNow credentials  --  single username=password pair
vault kv put secret/snow_advisor \\
    ata.integration='<SERVICENOW_PASSWORD_FROM_SECTION_8>'

# 3. Azure OpenAI  --  explicit named keys
vault kv put secret/sd_advisor_llm \\
    azure_endpoint='https://<your-resource>.openai.azure.com/' \\
    azure_api_key='<AZURE_API_KEY>' \\
    azure_api_version='2024-10-21'

# 4. SMTP  --  NOT REQUIRED on this estate.
#    mailhost.kohler.com:25 is an anonymous internal relay, so no credential
#    exists to store. The mailer treats a missing secret as an open relay.
#    Only create this if authentication is introduced later:
#      vault kv put secret/sd_advisor_smtp username='...' password='...'

# 5. Web session signing key and optional webhook token
vault kv put secret/sd_advisor_web \\
    secret_key="$(openssl rand -base64 48)" \\
    webhook_token="$(openssl rand -hex 32)" """)

g.callout('crit', 'Credential-pair format matters.',
          'For secret/sd_advisor_db and secret/snow_advisor the KEY is the username and the '
          'VALUE is the password. Writing username=x password=y also works — the client '
          'accepts both — but writing any other combination of keys will fail with '
          '"expected a single username:password pair".')

g.h2('7.4 Authentication: AppRole (recommended)')

g.p('A static token has to be renewed by something, and when that something is forgotten the '
    'service fails at 03:00 on a Sunday. AppRole removes the problem: the service exchanges '
    'a role_id and secret_id for a short-lived token at startup, renews it on a background '
    'thread, and logs in again automatically when the token reaches its maximum lifetime or '
    'is revoked.')

g.table(['', 'Static token', 'AppRole'], [
    ['On disk', 'A live token', 'role_id + secret_id (credentials, not a session)'],
    ['Lifetime', 'Fixed; expires', 'Token TTL 1h, renewed automatically, unlimited re-login'],
    ['Renewal', 'Your problem — cron or manual', 'Built in, no external moving parts'],
    ['If leaked', 'Immediate access until expiry or revocation', 'Can be CIDR-bound to the '
                                                                 'app server; secret_id revocable'],
    ['Revocation', 'Revoke the token', 'Revoke the secret_id; the role survives'],
], widths=[2.8, 6.4, 7.2])

g.h3('Step 1 — enable the auth method')
g.code("""vault auth list | grep -q '^approle/' || vault auth enable approle""")

g.h3('Step 2 — create the role')
g.code("""LAN_IP=$(hostname -I | awk '{print $1}')

vault write auth/approle/role/sd-advisor \\
    token_policies="sd-advisor" \\
    token_ttl=1h \\
    token_max_ttl=24h \\
    secret_id_ttl=0 \\
    secret_id_num_uses=0 \\
    token_num_uses=0 \\
    bind_secret_id=true \\
    secret_id_bound_cidrs="127.0.0.1/32,${LAN_IP}/32" \\
    token_bound_cidrs="127.0.0.1/32,${LAN_IP}/32" """)

g.callout('crit', 'Include loopback, or login will fail.',
          'CIDR restrictions match the source address AS VAULT OBSERVES IT, not the address '
          'you dialled. Vault runs on the same host as the advisor here, so the connection '
          'arrives over loopback and Vault sees 127.0.0.1 — even when VAULT_ADDR is '
          'https://kohlerco.com:8200. Binding only to the LAN address produces: '
          '\'source address "127.0.0.1" unauthorized by CIDR restrictions on the role\'. '
          'Listing both addresses also keeps the role working if DNS or routing changes.')

g.table(['Parameter', 'Value', 'Why'], [
    ['token_ttl', '1h', 'Short-lived. Renewed automatically at the halfway point'],
    ['token_max_ttl', '24h', 'After this the token cannot be renewed further, so the client '
                             'logs in again. This is normal and handled silently'],
    ['secret_id_ttl', '0', 'The secret_id itself does not expire. Set a TTL only if you have '
                           'a rotation process — see Section 7.8'],
    ['secret_id_num_uses', '0', 'Unlimited logins. The service re-logs-in on every restart '
                                'and at every max_ttl boundary'],
    ['token_num_uses', '0', 'Unlimited API calls per token'],
    ['secret_id_bound_cidrs', 'loopback + LAN /32', 'The secret_id is unusable from any '
                                                    'other host. See the warning above on '
                                                    'which addresses to list'],
    ['token_bound_cidrs', 'loopback + LAN /32', 'Same protection for the issued token'],
], widths=[3.8, 2.8, 9.8], code_cols=(0,))

g.callout('warn', 'What the CIDR binding does and does not buy you.',
          'The secret_id is long-lived and sits on disk, so it is the credential worth '
          'protecting, and binding it means a copy taken off the box — in a backup, a '
          'support bundle, or onto a laptop — cannot be used. That is the realistic leak, '
          'and this stops it. But because Vault is on the same host, 127.0.0.1/32 admits '
          'any local process, so it is not protection against local compromise. There, the '
          'control is filesystem permissions: mode 600, owned by the service account.')

g.callout('warn', 'vault write REPLACES the role.',
          'It is not a patch. Any parameter you omit reverts to its default — so when '
          'amending an existing role, re-supply every line above, not just the one you are '
          'changing. Confirm the result with "vault read auth/approle/role/sd-advisor".')

g.h3('Step 3 — fetch the role_id and generate a secret_id')
g.code("""# role_id is stable and not itself a secret, but treat it as one
vault read -field=role_id auth/approle/role/sd-advisor/role-id

# secret_id IS a secret. It is shown once and cannot be retrieved again.
vault write -f -field=secret_id auth/approle/role/sd-advisor/secret-id""")

g.callout('crit', 'Always generate a fresh secret_id after changing the role.',
          'A secret_id carries the CIDR list it was issued under. Amending '
          'secret_id_bound_cidrs on the role does not retrofit credentials that already '
          'exist, so an existing secret_id keeps failing with the same error. This catches '
          'people out every time.')

g.h3('Step 4 — install both on the application server')
g.code("""BASE=/genai/etc/scripts/aged_ticket_advisor

sudo -u genai install -m 600 /dev/stdin $BASE/config/.vault_role_id   <<< '<ROLE_ID>'
sudo -u genai install -m 600 /dev/stdin $BASE/config/.vault_secret_id <<< '<SECRET_ID>'

ls -l $BASE/config/.vault_role_id $BASE/config/.vault_secret_id
# Both must show -rw------- and be owned by genai""")

g.callout('info', 'No .vault_token file is needed.',
          'In AppRole mode the service never reads one. If existing operational tooling '
          'expects a token on disk, set vault.approle.write_token_file to true and the live '
          'token is mirrored to config/.vault_token atomically at mode 600 on every login '
          'and renewal. Leave it false otherwise — a token on disk is precisely what AppRole '
          'exists to avoid.')

g.h3('Step 5 — point conf.json at AppRole')
g.code(""""vault": {
    "url": "https://kohlerco.com:8200",
    "auth_method": "approle",
    "verify_tls": true,
    "ca_bundle": "/opt/vault/tls/tls.crt",

    "token_file": "config/.vault_token",

    "approle": {
        "role_id_file": "config/.vault_role_id",
        "secret_id_file": "config/.vault_secret_id",
        "write_token_file": false,
        "renew_ratio": 0.5,
        "min_renew_seconds": 60
    },

    "paths": { "...": "unchanged" }
}""")
g.table(['Key', 'Default', 'Meaning'], [
    ['auth_method', 'token', 'Set to approle to enable everything on this page'],
    ['approle.role_id_file', 'config/.vault_role_id', 'Path to the role_id'],
    ['approle.secret_id_file', 'config/.vault_secret_id', 'Path to the secret_id'],
    ['approle.write_token_file', 'false', 'Mirror the live token to token_file for external '
                                          'tooling. The app never reads it back'],
    ['approle.renew_ratio', '0.5', 'Renew when this fraction of the lease has elapsed. '
                                   '0.5 of a 1h TTL is every 30 minutes'],
    ['approle.min_renew_seconds', '60', 'Floor on the renewal interval, so a very short '
                                        'lease cannot cause a busy loop'],
], widths=[4.2, 3.4, 8.8], code_cols=(0,))

g.h2('7.5 What the service does at runtime')
g.code("""startup
  read role_id + secret_id  ->  POST auth/approle/login  ->  token (ttl 1h)
  start the 'vault-token-renewer' background thread
  [if write_token_file] mirror the token to config/.vault_token

every 30 minutes (ttl x renew_ratio)
  POST auth/token/renew-self
     success  ->  new lease, carry on
     declined ->  max_ttl reached or token revoked  ->  log in again

on failure (Vault unreachable)
  log an error, retry in 60s
  cached secrets keep working, so the service degrades rather than stopping

on shutdown
  the renewer thread is stopped cleanly""")

g.callout('good', 'A Vault outage does not stop the service.',
          'Every secret is cached in process after first read, so an unreachable Vault '
          'affects renewal only. The pipeline keeps running on cached credentials and '
          'recovers automatically when Vault returns.')

g.h2('7.6 Verify')
g.p('First, confirm the policy grants exactly what it should and nothing more:')
g.code("""# Log in the same way the service will, and test the boundaries
ROLE_ID=$(sudo cat /genai/etc/scripts/aged_ticket_advisor/config/.vault_role_id)
SECRET_ID=$(sudo cat /genai/etc/scripts/aged_ticket_advisor/config/.vault_secret_id)

export VAULT_TOKEN=$(vault write -field=token auth/approle/login \\
    role_id="$ROLE_ID" secret_id="$SECRET_ID")

vault kv get secret/sd_advisor_db      # should succeed
vault kv get secret/sd_advisor_llm     # should succeed
vault kv get secret/snow_advisor       # should succeed
vault kv get secret/itsm_analytics_db  # MUST fail with permission denied

vault token lookup | grep -E 'ttl|renewable|policies'
unset VAULT_TOKEN""")
g.p('The fourth command failing is the point of the exercise: it proves the advisor cannot '
    'reach the audit tool\'s credentials.')

g.p('Then confirm the application agrees:')
g.code("""cd /genai/etc/scripts/aged_ticket_advisor
python run.py doctor | grep vault

# PASS  vault   auth=approle, token expires in 1.0h, auto-renewal active""")

g.p('Finally, watch a renewal actually happen. With a 1h TTL the first one occurs about '
    'thirty minutes after start-up:')
g.code("""sudo journalctl -u aged-ticket-advisor -f | grep -i vault

# Vault session established (https://kohlerco.com:8200, auth=approle)
# AppRole login succeeded (ttl=3600s, renewable=True, policies=default,sd-advisor)
# Vault token renewer started (first refresh in ~1800s)
# ... 30 minutes later ...
# Vault token renewed (ttl=3600s)""")

g.callout('info', 'Want to see it sooner?',
          'Temporarily set token_ttl=5m on the role and restart the service. The renewer '
          'floor is 60s, so you will see a renewal within about two and a half minutes, and '
          'a full re-login once max_ttl is reached. Put the TTL back afterwards.')

g.h2('7.7 Rotating the secret_id')
g.p('With secret_id_ttl=0 the credential does not expire, so rotation is a scheduled hygiene '
    'task rather than an operational necessity. To rotate without downtime:')
g.code("""# 1. Generate a replacement (the old one stays valid for now)
NEW_SECRET_ID=$(vault write -f -field=secret_id auth/approle/role/sd-advisor/secret-id)

# 2. Install it
sudo -u genai install -m 600 /dev/stdin \\
    /genai/etc/scripts/aged_ticket_advisor/config/.vault_secret_id <<< "$NEW_SECRET_ID"

# 3. Restart so the next login uses it
sudo systemctl restart aged-ticket-advisor
python run.py doctor | grep vault

# 4. Only once that is healthy, destroy the old secret_id
vault list auth/approle/role/sd-advisor/secret-id
vault write auth/approle/role/sd-advisor/secret-id-accessor/destroy \\
    secret_id_accessor="<OLD_ACCESSOR>" """)
g.callout('warn', 'Restart is required.',
          'The secret_id is read at login, not on every renewal, so replacing the file alone '
          'changes nothing until the service restarts or reaches max_ttl. Steps 3 and 4 are '
          'in that order deliberately — do not destroy the old credential until the new one '
          'is proven.')

g.h3('If you prefer an expiring secret_id')
g.p('Set secret_id_ttl (say 90d) and schedule the rotation above. The trade-off is that a '
    'missed rotation becomes an outage, which is the failure mode AppRole was adopted to '
    'avoid — so only do this if the rotation is automated and monitored.')

g.h2('7.8 Fallback: static token')
g.p('For a quick trial, or if AppRole cannot be enabled on your Vault, the original method '
    'still works. Set auth_method to token and provide config/.vault_token:')
g.code("""vault token create -policy=sd-advisor -period=768h \\
    -display-name=aged-ticket-advisor -field=token \\
  | sudo -u genai install -m 600 /dev/stdin \\
      /genai/etc/scripts/aged_ticket_advisor/config/.vault_token""")
g.code(""""vault": { "auth_method": "token", "token_file": "config/.vault_token" }""")
g.callout('warn', 'You now own the renewal.',
          'A 768h period means the token must be renewed within 32 days or the service stops '
          'reading secrets. doctor warns when a static token has under 7 days left, and '
          'Section 21.3 lists it as a monthly task — but AppRole removes the task entirely.')


# =========================================================== SECTION 8 =====
g.h1('8. Step 6 — ServiceNow configuration')

g.h2('8.1 Create a dedicated read-only integration account')
g.p('You could reuse the existing integration account, but a dedicated one is strongly '
    'preferred: it makes the advisor\'s API usage separately auditable and rate-limitable, '
    'and it lets you revoke access without affecting the audit tool.')
g.table(['Field', 'Value'], [
    ['User ID', 'ata.integration'],
    ['First / Last name', 'Aged Ticket / Advisor'],
    ['Email', 'A monitored mailbox, or none'],
    ['Web service access only', 'TRUE — blocks interactive login'],
    ['Internal Integration User', 'TRUE (if your instance version offers it)'],
    ['Password', 'Generated, stored only in Vault'],
    ['Time zone', 'Set to UTC if possible — see the note in Section 8.4'],
], widths=[5.0, 11.4])

g.h2('8.2 Required table read access')
g.p('The account needs READ on the following tables, and write on none of them.')
g.table(['Table', 'Used for', 'Required?'], [
    ['incident', 'The tickets themselves', 'Yes'],
    ['sys_history_line', 'Field-level audit trail — the basis of every idle and activity signal', 'Yes'],
    ['task_sla', 'Breach state, percentage consumed, planned end time', 'Yes'],
    ['sys_user', 'Timezone offset calibration', 'Yes'],
    ['kb_knowledge', 'Knowledge article retrieval', 'Yes'],
    ['m2m_kb_task', 'Whether a KB article is already attached', 'Yes'],
    ['change_request', 'Detecting a change dependency that has already closed', 'Yes'],
    ['problem', 'Detecting a problem dependency that has already closed', 'Yes'],
    ['contract_sla', 'Confirming SLA definition naming (Section 3.5)', 'Optional'],
], widths=[3.8, 9.8, 2.8], code_cols=(0,))

g.h3('Suggested roles')
g.p('Exact role names vary by instance. In most deployments the following combination is '
    'sufficient, and your ServiceNow administrator should confirm against local ACLs:')
g.code("""itil                  # read access to incident, task_sla, change_request, problem
knowledge              # read access to kb_knowledge
snc_read_only          # belt-and-braces: blocks writes platform-wide for this user
rest_api_explorer      # optional, only if you want the account to use the REST explorer""")

g.callout('good', 'Defence in depth.',
          'Assigning snc_read_only is the strongest guarantee available. The advisor codebase '
          'contains no write client at all, but a platform-level read-only role means even a '
          'future code change could not modify a ticket with this account.')

g.h2('8.3 Verify access with curl')
g.p('Run each of these from the application server. Every one must return HTTP 200 and a '
    'JSON result array.')
g.code("""SNOW='https://<instance>.service-now.com'
CRED='ata.integration:<password>'

# 1. Incidents - open and in scope
curl -s -o /dev/null -w 'incident          %{http_code}\\n' -u "$CRED" \\
  "$SNOW/api/now/table/incident?sysparm_limit=1&sysparm_query=active=true"

# 2. History - the most commonly missed permission
curl -s -o /dev/null -w 'sys_history_line  %{http_code}\\n' -u "$CRED" \\
  "$SNOW/api/now/table/sys_history_line?sysparm_limit=1"

# 3. SLA
curl -s -o /dev/null -w 'task_sla          %{http_code}\\n' -u "$CRED" \\
  "$SNOW/api/now/table/task_sla?sysparm_limit=1"

# 4. Knowledge
curl -s -o /dev/null -w 'kb_knowledge      %{http_code}\\n' -u "$CRED" \\
  "$SNOW/api/now/table/kb_knowledge?sysparm_limit=1&sysparm_query=workflow_state=published"

# 5. KB attachment join table
curl -s -o /dev/null -w 'm2m_kb_task       %{http_code}\\n' -u "$CRED" \\
  "$SNOW/api/now/table/m2m_kb_task?sysparm_limit=1"

# 6. Change and problem
curl -s -o /dev/null -w 'change_request    %{http_code}\\n' -u "$CRED" \\
  "$SNOW/api/now/table/change_request?sysparm_limit=1"
curl -s -o /dev/null -w 'problem           %{http_code}\\n' -u "$CRED" \\
  "$SNOW/api/now/table/problem?sysparm_limit=1"

# 7. Users
curl -s -o /dev/null -w 'sys_user          %{http_code}\\n' -u "$CRED" \\
  "$SNOW/api/now/table/sys_user?sysparm_limit=1" """)

g.table(['Response', 'Meaning', 'Fix'], [
    ['200', 'Access confirmed', '—'],
    ['401', 'Wrong username or password', 'Re-check the credential, and that the account is active'],
    ['403', 'Authenticated but no read ACL on that table', 'Ask the SNOW admin to grant read on that table'],
    ['200 with empty result', 'Access is fine, the query simply matched nothing', 'Acceptable'],
], widths=[2.4, 6.6, 7.4], code_cols=(0,))

g.h2('8.4 Timezone')
g.p('ServiceNow returns history timestamps in UTC but attachment timestamps in the '
    'integration user\'s display timezone. The advisor detects the offset automatically on '
    'first use by comparing the same field with and without display values, and caches it. '
    'Setting the integration user to UTC makes the offset zero and removes a class of '
    'subtle date errors entirely — recommended but not required.')

g.h2('8.5 Rate limiting')
g.p('Confirm with your ServiceNow administrator whether an inbound REST rate limit applies '
    'to integration users. Steady-state load is modest, but the one-off backfill in Section '
    '14 is the heaviest the advisor will ever be.')
g.table(['Phase', 'Approximate API calls', 'When'], [
    ['Stream tick', '2 + 3 per aged ticket', 'Every 10 minutes'],
    ['Daily digest', 'Same, plus a full backlog fetch', 'Once per configured digest'],
    ['Backfill (one-off)', 'One page per 200 resolved incidents, plus KB', 'Section 14 only'],
], widths=[3.6, 7.0, 5.8])
g.rich([('If you hit a rate limit, reduce ', {}), ('servicenow.page_size', {'code': True}),
        (' and raise ', {}), ('scheduler.stream_interval_minutes', {'code': True}),
        ('. The client already retries with exponential backoff and honours '
         'Retry-After headers on 429 responses.', {})])


# =========================================================== SECTION 9 =====
g.h1('9. Step 7 — Azure OpenAI configuration')

g.h2('9.1 What is required')
g.table(['Item', 'Requirement', 'Notes'], [
    ['Chat deployment', 'GPT-4o or newer', 'Must support structured outputs (JSON schema)'],
    ['Embedding deployment', 'text-embedding-3-small or -large', 'Needed for similar-incident and KB retrieval'],
    ['API version', '2024-08-01-preview or later', 'Earlier versions lack json_schema response format'],
    ['Quota', 'Modest — see Section 9.4', 'Backfill is the peak'],
], widths=[3.8, 5.6, 7.0], code_cols=(1,))

g.callout('crit', 'The API version is not optional.',
          'The advisor constrains the model to a strict JSON schema rather than parsing prose. '
          'That requires response_format of type json_schema, which is unavailable before API '
          'version 2024-08-01-preview. The existing audit tool uses plain chat completions and '
          'may be pinned to an older version — check, and if so use a newer version here. The '
          'two tools can use different API versions against the same resource.')

g.h2('9.2 Confirm both deployments exist')
g.p('The existing tool only needs a chat deployment, so an embedding deployment may not yet '
    'exist. Check in the Azure portal under your OpenAI resource → Deployments, or via CLI:')
g.code("""az cognitiveservices account deployment list \\
    --name <resource-name> \\
    --resource-group <resource-group> \\
    -o table""")
g.p('If no embedding deployment exists, create one:')
g.code("""az cognitiveservices account deployment create \\
    --name <resource-name> \\
    --resource-group <resource-group> \\
    --deployment-name text-embedding-3-small \\
    --model-name text-embedding-3-small \\
    --model-version "1" \\
    --model-format OpenAI \\
    --sku-capacity 120 \\
    --sku-name Standard""")

g.h2('9.3 Verify from the application server')
g.code("""source /genai/etc/scripts/genai_venv_1/bin/activate
python3 - <<'PY'
from openai import AzureOpenAI
client = AzureOpenAI(
    api_key='<AZURE_API_KEY>',
    api_version='2024-10-21',
    azure_endpoint='https://<resource>.openai.azure.com/')

# Structured output support - this is the capability that matters
r = client.chat.completions.create(
    model='<chat-deployment>',
    messages=[{'role': 'user', 'content': 'Return a=1 and b=two.'}],
    response_format={'type': 'json_schema', 'json_schema': {
        'name': 'probe', 'strict': True,
        'schema': {'type': 'object',
                   'properties': {'a': {'type': 'integer'}, 'b': {'type': 'string'}},
                   'required': ['a', 'b'], 'additionalProperties': False}}})
print('chat      OK:', r.choices[0].message.content)

e = client.embeddings.create(model='<embedding-deployment>', input=['hello'])
print('embedding OK: dimensions =', len(e.data[0].embedding))
PY""")
g.rich([('Record the reported dimension count — it must match ', {}),
        ('llm.embedding_dims', {'code': True}),
        (' in conf.json (1536 for text-embedding-3-small, 3072 for -large).', {})])

g.h2('9.4 Cost and quota expectations')
g.p('These are planning estimates for a backlog in the range observed in the existing logs '
    '(roughly 40–220 tickets per day). Confirm against your own tariff.')
g.table(['Phase', 'Volume', 'Driver'], [
    ['One-off backfill', 'One embedding per resolved incident with usable closure notes, '
     'plus one per KB article', 'Section 14 — the single largest cost'],
    ['Nightly index refresh', 'Only new or changed records', 'Negligible after the backfill'],
    ['Recommendation pass', 'One chat call per ticket whose state actually changed',
     'Input-hash cache suppresses the rest'],
    ['Steady state', 'Typically well under a hundred chat calls per day',
     'Aged tickets by definition change little'],
], widths=[3.8, 6.8, 5.8])
g.callout('info', 'Cost control is built in.',
          'A recommendation is cached against a hash of the ticket state, signals, risk flags '
          'and retrieved evidence. Values that drift continuously — age and idle days — are '
          'deliberately excluded from that hash, otherwise every poll would look like a change '
          'and the cache would never hit.')


# ========================================================== SECTION 10 =====
g.h1('10. Step 8 — SMTP configuration')

g.h2('10.1 Relay details for this estate')
g.table(['Setting', 'conf.json key', 'Value'], [
    ['Relay host', 'mail.host', 'mailhost.kohler.com'],
    ['Port', 'mail.port', '25'],
    ['STARTTLS', 'mail.use_starttls', 'false — but see Section 10.2'],
    ['Authentication', '(none)', 'Anonymous relay. No Vault secret is created'],
    ['From address', 'mail.from_address', 'sd-advisor@kohlerco.com — CONFIRM, see below'],
    ['Reply-To', 'mail.reply_to', 'A monitored lead mailbox'],
    ['Subject prefix', 'mail.subject_prefix', '[SD Advisor]'],
], widths=[3.4, 5.2, 7.8], code_cols=(1,))

g.callout('crit', 'Confirm the sending domain before go-live.',
          'The instance is kohler.service-now.com and the relay is mailhost.kohler.com, but '
          'the existing estate uses kohlerco.com (see VAULT_ADDR in the audit tool\'s '
          'startup_services.sh). Both domains plausibly exist. A From address on the wrong '
          'one is silently quarantined rather than rejected, which looks identical to the '
          'digest never being generated. Check with the messaging team and set '
          'mail.from_address accordingly.')

g.p('Two other things worth asking the messaging team:')
g.bullet('that the application server\'s IP is permitted to relay — anonymous relays normally '
         'restrict by source address;')
g.bullet('whether the sending address needs an SPF or DKIM entry.')

g.h2('10.2 Check whether STARTTLS is available anyway')
g.p('Many internal relays advertise STARTTLS opportunistically on port 25. If yours does, '
    'enabling it costs one configuration value and encrypts the hop — worth doing, because '
    'the digest carries caller names, agent names and ticket descriptions.')
g.code("""python3 -c "
import smtplib
s = smtplib.SMTP('mailhost.kohler.com', 25, timeout=15)
s.ehlo()
print('STARTTLS supported:', s.has_extn('starttls'))
print('Extensions:', sorted(s.esmtp_features))
s.quit()" """)
g.table(['Result', 'Set mail.use_starttls to'], [
    ['STARTTLS supported: True', 'true — free encryption, no other change needed'],
    ['STARTTLS supported: False', 'false'],
], widths=[5.6, 10.8], code_cols=(0,))
g.callout('warn', 'Do not enable it speculatively.',
          'The mailer calls starttls() unconditionally when the setting is true. If the relay '
          'does not advertise it, the send raises SMTPNotSupportedError and fails rather than '
          'falling back to plaintext.')

g.h2('10.3 Test the relay before configuring the service')
g.code("""python3 - <<'PY'
import smtplib
from email.message import EmailMessage

HOST, PORT = 'mailhost.kohler.com', 25
FROM, TO = 'sd-advisor@kohlerco.com', '<your.name>@kohlerco.com'

msg = EmailMessage()
msg['Subject'] = '[SD Advisor] relay test'
msg['From'], msg['To'] = FROM, TO
msg.set_content('If you are reading this, the relay works.')

with smtplib.SMTP(HOST, PORT, timeout=30) as s:
    # No starttls() and no login() - anonymous internal relay.
    s.send_message(msg)
print('sent - now check the inbox, and the junk folder')
PY""")

g.h2('10.4 Recipients')
g.p('Digest recipients are not configured in conf.json. Leads are whoever holds an active '
    'ADMIN or LEAD account in the advisor_user table with an email address set — created in '
    'Section 13. A lead whose assignment_groups field is blank receives all groups; otherwise '
    'they receive only digests covering their listed groups.')


# ========================================================== SECTION 11 =====
g.h1('11. Step 9 — conf.json, field by field')

g.h2('11.1 Create the file')
g.code("""cd /genai/etc/scripts/aged_ticket_advisor
cp config/conf.sample.json config/conf.json
chmod 600 config/conf.json
vi config/conf.json""")
g.callout('good', 'No secrets here.',
          'conf.json contains no passwords or keys — every credential is fetched from Vault at '
          'runtime. It is still set to mode 600 because it discloses internal hostnames and '
          'topology.')

g.h2('11.2 A completed example')
g.p('The following is a realistic configuration for the environment described in Section 3. '
    'Replace every angle-bracketed placeholder.')
g.code("""{
    "customer_name": "Kohler",

    "vault": {
        "url": "https://kohlerco.com:8200",
        "token_file": "config/.vault_token",
        "verify_tls": true,
        "ca_bundle": "/opt/vault/tls/tls.crt",
        "paths": {
            "database":     "sd_advisor_db",
            "servicenow":   "snow_advisor",
            "smtp":         "sd_advisor_smtp",
            "azure_openai": "sd_advisor_llm",
            "web":          "sd_advisor_web"
        }
    },

    "database": {
        "host": "localhost",
        "name": "sd_advisor_db",
        "port": 3306,
        "connect_timeout": 10
    },

    "audit_database": {
        "enabled": false,
        "host": "localhost",
        "name": "itsm_analytics_db",
        "vault_path": "sd_advisor_db"
    },

    "servicenow": {
        "url": "https://<instance>.service-now.com",
        "verify_tls": true,
        "timeout_seconds": 60,
        "page_size": 200,
        "max_retries": 3,
        "assignment_groups": ["Service Desk"],
        "system_accounts": ["system", "guest", "cac.rest"]
    },

    "sla": {
        "include_types": ["SLA"],
        "definitions": {
            "6a3dab1e1b60e410f8eb0f6e6e4bcba2": { "kind": "RESPONSE",   "name": "Priority 1(Critical) Response" },
            "342465c72b2a4350793af829ce91bf75": { "kind": "RESPONSE",   "name": "Priority 1(Critical) Incident Response" },
            "481eab9a1b60e410f8eb0f6e6e4bcb02": { "kind": "RESPONSE",   "name": "Priority 2 (High) Response" },
            "7d4429c72b2a4350793af829ce91bf34": { "kind": "RESPONSE",   "name": "Priority 2 (Critical) Incident Response" },
            "288ee35e1b60e410f8eb0f6e6e4bcb76": { "kind": "RESPONSE",   "name": "Priority 3 (Medium) Response" },
            "3eeea35e1b60e410f8eb0f6e6e4bcb86": { "kind": "RESPONSE",   "name": "Priority 4 (Low) Response" },

            "c1e472d21beca410f8eb0f6e6e4bcb65": { "kind": "RESOLUTION", "name": "Priority 1 (Critical) Resolution" },
            "0d8ec5072be24350793af829ce91bf83": { "kind": "RESOLUTION", "name": "P1 (Major Incident) Resolution" },
            "d606b61a1beca410f8eb0f6e6e4bcb9b": { "kind": "RESOLUTION", "name": "Priority 2 (High) Resolution" },
            "4edbfeda1beca410f8eb0f6e6e4bcbe2": { "kind": "RESOLUTION", "name": "Priority 3 (Medium) Resolution" },
            "ffbc3e1e1beca410f8eb0f6e6e4bcbae": { "kind": "RESOLUTION", "name": "Priority 4 (Low) Resolution" },

            "10b7e3ab1be47090f8eb0f6e6e4bcb13": { "kind": "VENDOR",     "name": "Vendor Resolution" },
            "4a0a1f0dff03211001b9ffffffffff78": { "kind": "IGNORE",     "name": "ITSM IAR SLA" }
        }
    },

    "llm": {
        "chat_deployment": "<your-gpt-4o-deployment>",
        "embedding_deployment": "text-embedding-3-small",
        "embedding_dims": 1536,
        "api_version": "2024-10-21",
        "max_concurrency": 8,
        "request_timeout_seconds": 90,
        "max_retries": 3,
        "min_confidence_to_surface": 0.55,
        "min_similarity": 0.55
    },

    "thresholds": {
        "aged_after_days": 5,
        "stagnation_days": 2,
        "critical_stagnation_days": 4,
        "sla_jeopardy_pct": 75,
        "auto_close_followups": 3,
        "auto_close_silence_days": 5,
        "digest_max_tickets_per_agent": 25
    },

    "scheduler": {
        "stream_interval_minutes": 10,
        "recommendation_interval_minutes": 60,
        "stats_refresh_cron": "0 2 * * *",
        "digests": [
            {
                "name": "emea-morning",
                "cron": "30 7 * * 1-5",
                "timezone": "Europe/London",
                "assignment_groups": ["Service Desk"]
            }
        ]
    },

    "mail": {
        "enabled": true,
        "host": "mailhost.kohler.com",
        "port": 25,
        "use_starttls": false,
        "from_address": "sd-advisor@kohlerco.com",
        "reply_to": "servicedesk-leads@kohlerco.com",
        "always_bcc": [],
        "subject_prefix": "[SD Advisor]"
    },

    "web": {
        "base_url": "https://<appserver-fqdn>:8444",
        "host": "0.0.0.0",
        "port": 8444,
        "session_cookie": "ata_session",
        "session_max_age_seconds": 43200,
        "secret_file": "config/.session_secret"
    },

    "runtime": {
        "shadow_mode": true,
        "log_level": "INFO",
        "log_dir": "logs",
        "redact_before_llm": true
    }
}""")

g.h2('11.3 Field reference')

g.h3('vault')
g.table(['Key', 'Meaning', 'Guidance'], [
    ['url', 'Vault API address', 'Use the same value as the audit tool\'s qa_conf.json'],
    ['token_file', 'Path to the advisor\'s own token', 'Relative paths resolve from the project root'],
    ['verify_tls', 'Verify the Vault certificate', 'Leave true. The audit tool disables this; do not copy that'],
    ['ca_bundle', 'CA certificate for Vault', '/opt/vault/tls/tls.crt on this estate'],
    ['paths.*', 'Where each credential set lives', 'Must match Section 7.3 exactly'],
], widths=[3.0, 5.6, 7.8], code_cols=(0,))

g.h3('servicenow')
g.table(['Key', 'Meaning', 'Guidance'], [
    ['url', 'Instance base URL', 'No trailing slash needed; one is stripped if present'],
    ['verify_tls', 'Verify the SNOW certificate', 'Keep true'],
    ['timeout_seconds', 'Per-request timeout', '60 is ample; raise if the instance is slow'],
    ['page_size', 'Records per API page', '200. Lower it if you hit rate limits'],
    ['max_retries', 'Retries on 429/5xx', '3, with exponential backoff'],
    ['assignment_groups', 'Queues in scope', 'Empty list means every group — not recommended'],
    ['system_accounts', 'Automated accounts', 'THE critical value. See Section 3.4'],
], widths=[3.4, 5.2, 7.8], code_cols=(0,))

g.h3('sla')
g.table(['Key', 'Meaning', 'Guidance'], [
    ['include_types', 'contract_sla.type values that count',
     '["SLA"] excludes OLAs and underpinning contracts. Empty means no filter'],
    ['definitions', 'sys_id to kind map',
     'Keyed on contract_sla sys_id so a rename cannot change scoring. "name" is '
     'documentation only'],
    ['(kind) RESOLUTION', 'Customer-facing commitment', 'What sla_breached is built from'],
    ['(kind) RESPONSE', 'Time to first contact', 'Used only when no resolution SLA is attached'],
    ['(kind) VENDOR', 'Third-party target', 'Raises VENDOR_SLA_BREACHED instead of sla_breached'],
    ['(kind) IGNORE', 'Excluded entirely', 'Dropped before any calculation'],
], widths=[3.4, 5.2, 7.8], code_cols=(0,))
g.rich([('Anything not listed falls back to matching words in the definition name, so a new '
         'SLA degrades gracefully rather than erroring. Verify with ', {}),
        ('python run.py sla-map', {'code': True}), (' — see Section 3.5.', {})])

g.h3('thresholds')
g.table(['Key', 'Default', 'Effect if raised', 'Effect if lowered'], [
    ['aged_after_days', '5', 'Fewer tickets reviewed, risk of missing some',
     'More tickets, more noise and cost'],
    ['stagnation_days', '2', 'Fewer "stale" flags', 'More stale flags'],
    ['critical_stagnation_days', '4', 'Fewer critical flags; also flattens the stagnation score curve',
     'More critical flags'],
    ['sla_jeopardy_pct', '75', 'Later SLA warnings', 'Earlier warnings, more alerts'],
    ['auto_close_followups', '3', 'Stricter closure policy', 'Tickets proposed for closure sooner'],
    ['auto_close_silence_days', '5', 'Longer wait before proposing closure', 'Faster closure proposals'],
    ['digest_max_tickets_per_agent', '25', 'Longer emails', 'Shorter emails; the remainder is on the board'],
], widths=[4.0, 1.6, 5.4, 5.4], code_cols=(0,))

g.h3('scheduler.digests — one entry per region')
g.p('Your caller base spans the UK, the United States and China, so one 07:30 digest cannot '
    'serve everyone. Add an entry per timezone:')
g.code(""""digests": [
    { "name": "emea",  "cron": "30 7 * * 1-5", "timezone": "Europe/London",
      "assignment_groups": ["Service Desk"] },
    { "name": "amer",  "cron": "30 7 * * 1-5", "timezone": "America/Chicago",
      "assignment_groups": ["Service Desk"] },
    { "name": "apac",  "cron": "30 8 * * 1-5", "timezone": "Asia/Shanghai",
      "assignment_groups": ["Service Desk"] }
]""")
g.callout('warn', 'Each digest entry runs the full pipeline.',
          'A digest job performs a full sync, a signal refresh and a recommendation pass before '
          'sending. Three regional digests therefore mean three full passes per day. That is '
          'usually fine, but stagger the cron times rather than firing them simultaneously.')

g.h3('runtime')
g.table(['Key', 'Default', 'Meaning'], [
    ['shadow_mode', 'true', 'Everything is computed, stored and rendered, but NO mail is sent. '
     'Delivery attempts are logged to digest_run with status SUPPRESSED'],
    ['log_level', 'INFO', 'DEBUG is very verbose; use it only while diagnosing'],
    ['log_dir', 'logs', 'Rotated daily, 30 days retained'],
    ['redact_before_llm', 'true', 'Masks phone numbers, emails, card/SSN patterns, API keys and '
     'password assignments before any text reaches Azure. Leave enabled'],
], widths=[3.2, 1.8, 11.4], code_cols=(0,))

g.h2('11.4 Validate the JSON')
g.code("""python3 -m json.tool /genai/etc/scripts/aged_ticket_advisor/config/conf.json > /dev/null \\
  && echo 'conf.json is valid JSON'""")


# ========================================================== SECTION 12 =====
g.h1('12. Step 10 — Verification with run.py doctor')

g.p('The doctor command checks every external dependency in one pass and exits non-zero on '
    'any failure. Run it now, before attempting a real pipeline run.')
g.code("""source /genai/etc/scripts/genai_venv_1/bin/activate
cd /genai/etc/scripts/aged_ticket_advisor
python run.py doctor""")

g.p('Expected output on a correctly configured system:')
g.code("""PASS  config                loaded /genai/etc/scripts/aged_ticket_advisor/config/conf.json
PASS  vault                 auth=approle, token expires in 1.0h, auto-renewal active
PASS  database              0 tracked tickets
PASS  servicenow            137 aged open
PASS  llm                   reachable (gpt-4o)
PASS  vector index          0 incidents, 0 KB articles
PASS  users                 0 active
INFO  audit database link   disabled (coaching rollup will be omitted)
INFO  shadow mode           ON - no mail will be sent""")

g.callout('good', 'Zero counts are expected here.',
          'Nothing has been synced, indexed or created yet. What matters is that every line '
          'says PASS. The counts become meaningful after Sections 13 to 15.')

g.p('Then verify the SLA mapping, which doctor does not cover because it needs the '
    'definitions read from ServiceNow:')
g.code("""python run.py sla-map""")
g.p('Every row should show a SOURCE of config:sys_id. Anything reporting name-token or '
    'unmatched is relying on the fallback — the command prints the config line to paste, and '
    'exits with status 2 so a pipeline can gate on it. Section 3.5 explains the kinds.')

g.h2('12.1 Interpreting failures')
g.table(['Failing check', 'Most likely cause', 'Resolution'], [
    ['config', 'conf.json missing or malformed', 'Section 11.1 and 11.4'],
    ['vault', 'role_id/secret_id missing or unreadable, CIDR binding rejects this host, '
              'approle not enabled, or Vault sealed',
     'Section 7.4 and 7.6; check "vault status"'],
    ['database', 'Wrong credentials in Vault, schema not applied, or user lacks grants',
     'Section 6.1 and 6.2; confirm the Vault key is the username'],
    ['servicenow', 'Bad credentials, missing table ACL, or TLS trust failure',
     'Re-run the curl checks in Section 8.3'],
    ['llm', 'Wrong endpoint or key, deployment name mismatch, or API version too old',
     'Section 9.3 — the structured-output probe is the decisive test'],
    ['vector index', 'Normally passes with zero before backfill', 'Only a concern after Section 14'],
    ['users', 'No accounts created yet', 'Section 13'],
], widths=[3.0, 6.4, 7.0], code_cols=(0,))


# ========================================================== SECTION 13 =====
g.h1('13. Step 11 — Create UI accounts')

g.h2('13.1 Roles')
g.table(['Role', 'Can do', 'Give to'], [
    ['ADMIN', 'Everything: tune weights, trigger digests manually, all lead actions',
     'The platform owner and one or two senior leads'],
    ['LEAD', 'Review, record verdicts, snooze, request re-analysis; receives the digest',
     'Shift leads'],
    ['VIEWER', 'Read only', 'Managers and reporting users'],
], widths=[2.4, 8.0, 6.0], code_cols=(0,))

g.h2('13.2 About the password')
g.callout('info', 'It is a new password that you choose.',
          'These are local application accounts. The password is not your AD or SSO '
          'password, not the ServiceNow integration password and not the MySQL password - '
          'it is stored only in the advisor\'s own advisor_user table, as a bcrypt hash, '
          'and is used solely to sign in to the board.')
g.table(['Rule', 'Detail'], [
    ['Minimum length', '8 characters'],
    ['Maximum length', '72 bytes — a bcrypt limit. Non-ASCII characters cost more than one '
                       'byte each, so a 40-character password can exceed it'],
    ['Entry', 'Omit --password and it is prompted for twice, without echoing'],
    ['Storage', 'bcrypt, standard $2b$ format, unique salt per account'],
    ['Changing it', 'python run.py passwd --username <name>'],
], widths=[3.4, 13.0])
g.callout('warn', 'Do not pass --password on the command line in production.',
          'It lands in shell history and is visible in ps output to every user on the box. '
          'Let the command prompt for it instead; it asks twice, so a typo cannot silently '
          'create an account nobody can log into.')

g.h2('13.3 Create the accounts')
g.code("""source /genai/etc/scripts/genai_venv_1/bin/activate
cd /genai/etc/scripts/aged_ticket_advisor

# Administrator (you will be prompted for the password)
python run.py adduser \\
    --username pkumar \\
    --full-name "P Kumar" \\
    --email p.kumar@kohlerco.com \\
    --role ADMIN

# A shift lead scoped to one group
python run.py adduser \\
    --username sdlead1 \\
    --full-name "Service Desk Lead" \\
    --email sd.lead1@kohlerco.com \\
    --role LEAD \\
    --groups "Service Desk"

# A read-only manager account
python run.py adduser \\
    --username smanager \\
    --full-name "Service Manager" \\
    --email s.manager@kohlerco.com \\
    --role VIEWER""")

g.callout('info', 'Two things the email address controls.',
          'It is both the digest recipient for ADMIN and LEAD accounts, and — when the '
          'full_name matches the ServiceNow assigned_to display name — the address used for '
          'that agent\'s personal digest. Matching the names exactly is what enables per-agent '
          'emails.')

g.rich([('Leaving ', {}), ('--groups', {'code': True}),
        (' blank means the account sees and is emailed about every in-scope group. Passing a '
         'comma-separated list restricts both the board and the digest.', {})])

g.h2('13.4 Verify')
g.code("""mysql -u sd_advisor -p sd_advisor_db -e "
  SELECT username, full_name, email, role, assignment_groups, active
    FROM advisor_user;"
""")


# ========================================================== SECTION 14 =====
g.h1('14. Step 12 — Build the evidence index (backfill)')

g.p('This one-off job gives the advisor its evidence base: embeddings of resolved incidents '
    'and knowledge articles, plus p50/p90 resolution baselines per category. Without it the '
    'system still works, but recommendations fall back to keyword KB search and lose the '
    '"tickets like this are normally fixed in four hours by doing X" grounding.')

g.callout('warn', 'Plan for a long run.',
          'Depending on volume this takes one to four hours and is the largest single Azure '
          'OpenAI cost in the project. Run it in a screen or tmux session, outside business '
          'hours, and expect meaningful ServiceNow API load throughout.')

g.h2('14.1 Run it')
g.code("""screen -S ata-backfill        # or: tmux new -s ata-backfill

source /genai/etc/scripts/genai_venv_1/bin/activate
cd /genai/etc/scripts/aged_ticket_advisor

time python run.py backfill --days 180 2>&1 | tee logs/backfill.log

# Detach with Ctrl-A D (screen) or Ctrl-B D (tmux)""")

g.h2('14.2 Start smaller if you prefer')
g.p('A 30-day backfill completes in minutes and is enough to prove the mechanism end to end. '
    'Re-running with a longer window later is safe — records whose text has not changed are '
    'skipped by hash, so nothing is embedded twice.')
g.code("""python run.py backfill --days 30""")

g.h2('14.3 Expected output')
g.code("""{
  "fetched": 14203,
  "incidents_indexed": 9187,
  "kb_indexed": 412,
  "baselines": 63
}""")
g.p('The gap between "fetched" and "incidents_indexed" is expected and healthy: tickets '
    'closed with an empty or near-empty closure note are deliberately excluded, because they '
    'are not evidence of anything. The log reports the exact count.')

g.h2('14.4 Verify')
g.code("""mysql -u sd_advisor -p sd_advisor_db -e "
  SELECT entity_type, COUNT(*) AS n, MAX(updated_at) AS newest
    FROM embedding_store GROUP BY entity_type;
  SELECT COUNT(*) AS baselines, MIN(sample_size) AS smallest_sample
    FROM resolution_stat;"
""")
g.p('Then confirm the service can load the index into memory:')
g.code("""python run.py doctor | grep 'vector index'
# PASS  vector index   9187 incidents, 412 KB articles""")

g.callout('info', 'After this, refreshes are automatic.',
          'The nightly maintenance job re-runs the indexer at 02:00 UTC by default and only '
          'embeds new or changed records, so the ongoing cost is negligible.')


# ========================================================== SECTION 15 =====
g.h1('15. Step 13 — First manual pipeline run')

g.p('Run each stage by hand once. This surfaces problems one layer at a time, rather than as '
    'a single opaque failure inside the scheduler.')

g.h2('15.1 Sync tickets')
g.code("""python run.py sync --full

# Expected shape:
# { "mode": "full", "fetched": 412, "changed": 412, "retired": 0,
#   "watermark": "2026-09-23 06:14:22" }""")
g.code("""mysql -u sd_advisor -p sd_advisor_db -e "
  SELECT COUNT(*) AS tracked,
         SUM(active) AS active,
         MIN(opened_at) AS oldest
    FROM watched_ticket;"
""")

g.h2('15.2 Compute signals')
g.code("""python run.py signals

# Expected:
# { "scored": 137, "failed": 0, "duration_s": 42.7 }""")

g.p('Now inspect the result — this is the first point at which you can sanity-check the '
    'output against what the leads believe is true:')
g.code("""mysql -u sd_advisor -p sd_advisor_db -e "
  SELECT incident_number, attention_score, ball_in_court,
         ROUND(age_days) AS age, ROUND(idle_days,1) AS idle, risk_flags
    FROM v_current_board
   ORDER BY attention_score DESC
   LIMIT 15;"
""")

g.callout('crit', 'Stop here and review with a lead.',
          'Show them the top fifteen. Ask two questions: does the ball_in_court column match '
          'reality, and are the idle_days plausible? If idle times look far too low, an '
          'automated account is missing from servicenow.system_accounts — return to Section 3.4. '
          'This is the single most common configuration error and everything downstream depends '
          'on getting it right.')

g.h2('15.3 Generate recommendations')
g.p('Start with a small batch to confirm quality and cost before running the full backlog:')
g.code("""python run.py recommend -n 5

# Expected:
# { "analysed": 5, "cached": 0, "failed": 0, "fallback": 0, "duration_s": 18.3 }""")
g.code("""mysql -u sd_advisor -p sd_advisor_db -e "
  SELECT incident_number, recommended_action, confidence,
         LEFT(rationale, 90) AS rationale
    FROM recommendation WHERE superseded = 0
   ORDER BY id DESC LIMIT 5\\G"
""")
g.p('If those five read sensibly, run the rest:')
g.code("""python run.py recommend""")

g.h2('15.4 Preview the digest')
g.code("""python run.py preview -o /tmp/digest_preview.html

# Copy it somewhere you can open it in a browser
scp <appserver>:/tmp/digest_preview.html .""")
g.p('Check the preview for: correct ticket counts, sensible ranking, no placeholder or '
    'template artefacts, and — importantly — that no personal data looks wrong or exposed.')

g.h2('15.5 Send a real digest to yourself')
g.p('Shadow mode suppresses sending. To test delivery end to end without emailing the team, '
    'temporarily make yourself the only recipient:')
g.code("""-- Temporarily deactivate everyone except your own account
UPDATE advisor_user SET active = 0 WHERE username <> 'pkumar';""")
g.code("""# Temporarily disable shadow mode
vi config/conf.json          # set "shadow_mode": false
python run.py digest --no-agents

# Check the delivery log
mysql -u sd_advisor -p sd_advisor_db -e "
  SELECT run_type, recipient, ticket_count, status, sent_at, error
    FROM digest_run ORDER BY id DESC LIMIT 5;" """)
g.callout('crit', 'Restore both settings immediately afterwards.',
          'Set shadow_mode back to true and reactivate the other accounts '
          '(UPDATE advisor_user SET active = 1;) before continuing. The two-week shadow period '
          'in Section 19 depends on this.')


# ========================================================== SECTION 16 =====
g.h1('16. Step 14 — Install the systemd service')

g.h2('16.1 Review the unit file')
g.rich([('The unit is at ', {}), ('ops/aged-ticket-advisor.service', {'code': True}),
        ('. Confirm the paths and the user account match your environment before installing '
         'it — particularly if you chose the dedicated virtual environment in Section 5.2.', {})])
g.code("""[Unit]
Description=Aged Ticket Advisor (service desk aged pending ticket review)
After=network-online.target vault.service mysqld.service
Wants=network-online.target

[Service]
Type=simple
User=genai
Group=genai
WorkingDirectory=/genai/etc/scripts/aged_ticket_advisor

Environment=PYTHONUNBUFFERED=1
Environment=ATA_CONFIG=/genai/etc/scripts/aged_ticket_advisor/config/conf.json

ExecStartPre=/genai/etc/scripts/genai_venv_1/bin/python run.py doctor
ExecStart=/genai/etc/scripts/genai_venv_1/bin/python run.py serve

Restart=on-failure
RestartSec=15
TimeoutStopSec=30
KillSignal=SIGTERM

NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=full
ProtectHome=true
ReadWritePaths=/genai/etc/scripts/aged_ticket_advisor/logs
ReadWritePaths=/genai/etc/scripts/aged_ticket_advisor/config

[Install]
WantedBy=multi-user.target""")

g.callout('good', 'ExecStartPre is deliberate.',
          'The service refuses to start if doctor fails. A misconfigured advisor that starts '
          'and silently produces nothing is worse than one that fails loudly at boot.')

g.h2('16.2 Install and start')
g.code("""sudo cp ops/aged-ticket-advisor.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable aged-ticket-advisor
sudo systemctl start aged-ticket-advisor

sudo systemctl status aged-ticket-advisor
sudo journalctl -u aged-ticket-advisor -f""")

g.h2('16.3 Confirm the scheduler started')
g.p('Within about thirty seconds of start-up the first stream tick fires. Look for:')
g.code("""Scheduler started with jobs: stream, recommend, digest-emea-morning, maintenance
Digest 'emea-morning' scheduled (30 7 * * 1-5 Europe/London)
Sync delta complete: 3 fetched, 1 changed, 0 retired (2.1s)
Signals refreshed: 137 scored, 0 failed (41.3s)""")

g.h2('16.4 Confirm the web service responds')
g.code("""curl -s http://localhost:8444/healthz | python3 -m json.tool

# {
#     "status": "ok",
#     "database": "ok",
#     "last_sync": "2026-09-23T06:24:11",
#     "llm": "ok",
#     "shadow_mode": true
# }""")

g.h2('16.5 Scheduled jobs installed')
g.table(['Job ID', 'Trigger', 'What it does'], [
    ['stream', 'Every 10 minutes', 'Delta sync, signal refresh, risk alert emails. No LLM'],
    ['recommend', 'Every 60 minutes', 'LLM recommendation pass over changed tickets'],
    ['digest-<name>', 'Per configured cron', 'Full pass, then lead and agent digests'],
    ['maintenance', '02:00 UTC daily', 'Full sync, index refresh, baseline rebuild, prune'],
], widths=[3.2, 3.6, 9.6], code_cols=(0,))
g.p('Jobs are configured with coalesce and max_instances=1, so an overrunning run causes the '
    'next to be skipped rather than stacking up.')


# ========================================================== SECTION 17 =====
g.h1('17. Step 15 — Network, firewall and TLS')

g.h2('17.1 Required outbound access')
g.table(['Destination', 'Port', 'Purpose'], [
    ['<instance>.service-now.com', '443/tcp', 'Ticket, history, SLA and KB reads'],
    ['<resource>.openai.azure.com', '443/tcp', 'Chat and embedding calls'],
    ['Vault (kohlerco.com - must match the TLS certificate)', '8200/tcp', 'Credential retrieval'],
    ['MySQL', '3306/tcp (localhost)', 'Application database'],
    ['mailhost.kohler.com', '25/tcp', 'Digest delivery (anonymous relay)'],
], widths=[6.4, 3.0, 7.0], code_cols=(0, 1))

g.h2('17.2 Inbound')
g.code("""# Open 8444 to the lead user population only - not to the world
sudo firewall-cmd --permanent --add-rich-rule='
  rule family="ipv4"
  source address="<CORPORATE_SUBNET>/16"
  port protocol="tcp" port="8444" accept'
sudo firewall-cmd --reload
sudo firewall-cmd --list-all""")

g.h2('17.3 TLS — put a reverse proxy in front')
g.callout('crit', 'Do not expose port 8444 directly over plain HTTP.',
          'Users authenticate with a password, and the session cookie authorises every '
          'subsequent request. The application marks that cookie Secure automatically when '
          'web.base_url begins with https, which only helps if TLS is actually terminated in '
          'front of it.')
g.code("""# /etc/nginx/conf.d/ata.conf
server {
    listen 8444 ssl;
    server_name <appserver-fqdn>;

    ssl_certificate     /etc/pki/tls/certs/appserver.crt;
    ssl_certificate_key /etc/pki/tls/private/appserver.key;
    ssl_protocols       TLSv1.2 TLSv1.3;

    location / {
        proxy_pass         http://127.0.0.1:8445;
        proxy_set_header   Host              $host;
        proxy_set_header   X-Real-IP         $remote_addr;
        proxy_set_header   X-Forwarded-For   $proxy_add_x_forwarded_for;
        proxy_set_header   X-Forwarded-Proto $scheme;
        proxy_read_timeout 120s;
    }
}""")
g.p('With that proxy in place, bind the application to localhost on a different port:')
g.code(""""web": {
    "base_url": "https://<appserver-fqdn>:8444",
    "host": "127.0.0.1",
    "port": 8445
}""")

g.h2('17.4 Port allocation on this host')
g.table(['Port', 'Service', 'Status'], [
    ['8443', 'Existing itsm_analytics Flask front end', 'Unchanged — do not disturb'],
    ['8444', 'Aged Ticket Advisor (public, TLS)', 'New'],
    ['8445', 'Aged Ticket Advisor (loopback, behind nginx)', 'New, only if using a proxy'],
    ['8200', 'Vault', 'Existing'],
    ['3000', 'Grafana', 'Existing'],
], widths=[2.2, 8.0, 6.2], code_cols=(0,))


# ========================================================== SECTION 18 =====
g.h1('18. Step 16 — Grafana dashboard (optional)')

g.h2('18.1 Add the data source')
g.p('In Grafana: Configuration → Data sources → Add → MySQL.')
g.table(['Field', 'Value'], [
    ['Name', 'sd_advisor_db'],
    ['Host', 'localhost:3306'],
    ['Database', 'sd_advisor_db'],
    ['User', 'sd_advisor'],
    ['Password', 'The password from Section 6.1'],
], widths=[4.0, 12.4])
g.callout('info', 'Prefer a separate read-only user for Grafana.',
          'Grafana stores the password in its own database. Creating grafana_ata with SELECT '
          'only on sd_advisor_db keeps the application credential out of a second system.')
g.code("""CREATE USER 'grafana_ata'@'localhost' IDENTIFIED BY '<GRAFANA_PASSWORD>';
GRANT SELECT ON sd_advisor_db.* TO 'grafana_ata'@'localhost';
FLUSH PRIVILEGES;""")

g.h2('18.2 Starter panel queries')
g.code("""-- Aged backlog, split by who is actually blocking it
SELECT ball_in_court AS metric, COUNT(*) AS value
  FROM v_current_board WHERE snoozed = 0
 GROUP BY ball_in_court;

-- Attention score distribution
SELECT CASE WHEN attention_score >= 70 THEN 'Critical'
            WHEN attention_score >= 45 THEN 'High'
            WHEN attention_score >= 25 THEN 'Medium'
            ELSE 'Low' END AS metric,
       COUNT(*) AS value
  FROM v_current_board WHERE snoozed = 0
 GROUP BY metric;

-- Backlog trend over time
SELECT DATE(computed_at) AS time, COUNT(DISTINCT incident_number) AS aged_tickets
  FROM ticket_signal
 WHERE computed_at >= NOW() - INTERVAL 30 DAY
 GROUP BY DATE(computed_at) ORDER BY 1;

-- Recommendation accuracy (needs lead verdicts)
SELECT DATE(f.created_at) AS time,
       100.0 * SUM(f.decision IN ('ACCEPTED','MODIFIED')) / COUNT(*) AS agreement_pct
  FROM recommendation_feedback f
 WHERE f.created_at >= NOW() - INTERVAL 30 DAY
 GROUP BY DATE(f.created_at) ORDER BY 1;

-- Digest delivery health
SELECT sent_at AS time, run_type, recipient, ticket_count, status
  FROM digest_run ORDER BY sent_at DESC LIMIT 50;""")

g.h2('18.3 Colour guidance')
g.p('If you build severity panels, reuse the application\'s status colours so the dashboard '
    'and the board agree, and always pair the colour with a written label rather than relying '
    'on hue alone:')
g.table(['Severity', 'Hex', 'Score range'], [
    ['Critical', '#d03b3b', '70–100'],
    ['High', '#ec835a', '45–69'],
    ['Medium', '#fab219', '25–44'],
    ['Low', '#0ca30c', '0–24'],
], widths=[3.2, 3.2, 10.0], code_cols=(1,))


# ========================================================== SECTION 19 =====
g.h1('19. Step 17 — Shadow mode operation')

g.callout('crit', 'Do not skip the shadow period.',
          'Two weeks in shadow mode is what converts "the vendor says it is accurate" into '
          '"we measured it on our own tickets". It also gives leads time to correct the '
          'system-account list and thresholds before anyone is emailed.')

g.h2('19.1 What happens during shadow mode')
g.table(['Behaviour', 'In shadow mode'], [
    ['Sync, signals, scoring', 'Running normally'],
    ['LLM recommendations', 'Generated and stored normally'],
    ['Web board', 'Fully available — leads use it'],
    ['Digest emails', 'Rendered and logged to digest_run with status SUPPRESSED, not sent'],
    ['Risk alert emails', 'Same — suppressed'],
    ['Lead verdicts', 'Recorded normally. This is the point of the exercise'],
], widths=[5.0, 11.4])

g.h2('19.2 What the leads need to do')
g.p('Ask two or three leads to open the board each morning, work the top ten tickets as they '
    'normally would, and record a verdict on each one. Five to ten verdicts per lead per day '
    'reaches a statistically meaningful sample inside two weeks.')
g.table(['Verdict', 'Use when'], [
    ['Agree', 'The suggested action is what you would do'],
    ['Mostly right — I adjusted it', 'Right direction, wrong detail'],
    ['Wrong', 'The suggested action is not appropriate'],
    ['Not applicable', 'This ticket should not have been flagged at all'],
    ['Already handled', 'Actioned before you saw the suggestion'],
], widths=[5.0, 11.4])

g.h2('19.3 Weekly review')
g.p('Open /accuracy each Friday and review against the go-live gates:')
g.table(['Metric', 'Meaning', 'Gate'], [
    ['Action agreement', 'Agree or agree-with-edits, over all verdicts', '≥ 80%'],
    ['Actionable precision', 'Same numerator, excluding "already handled" — was flagging it justified?',
     '≥ 85%'],
    ['Review coverage', 'Share of recommendations that received any verdict',
     'High enough that the first two are credible'],
    ['Silent breaches', 'Breached with no prior warning — the recall measure',
     '0'],
], widths=[3.6, 8.4, 4.4])

g.h2('19.4 Tuning during the shadow period')
g.table(['Symptom', 'Likely cause', 'Adjustment'], [
    ['Idle days look far too low', 'An automated account is missing',
     'Add it to servicenow.system_accounts and restart'],
    ['Too many tickets flagged', 'Threshold too low', 'Raise thresholds.aged_after_days'],
    ['Rankings do not match lead intuition', 'Weights do not reflect local priorities',
     'Tune at /settings/weights'],
    ['Many "not applicable" verdicts', 'Scope too wide', 'Narrow servicenow.assignment_groups'],
    ['Many low-confidence recommendations', 'Thin evidence base',
     'Extend the backfill window; check the KB index populated'],
    ['ball_in_court frequently wrong', 'Hold reasons differ from the built-in mapping',
     'Edit HOLD_REASON_BLOCKER in pipeline/signals.py'],
], widths=[4.4, 5.0, 7.0])


# ========================================================== SECTION 20 =====
g.h1('20. Step 18 — Go live')

g.h2('20.1 Pre-flight checklist')
g.checklist([
    ('Action agreement is at or above 80% over at least 20 verdicts', 'The /accuracy page'),
    ('Actionable precision is at or above 85%', 'The /accuracy page'),
    ('Silent breaches is zero', 'The /accuracy page'),
    ('Leads have confirmed the system-account list is complete', 'Review with the SD leads'),
    ('Digest recipients are correct and complete', 'SELECT on advisor_user'),
    ('SLA definitions all resolve from config, none from the name fallback', 'run.py sla-map'),
    ('The From address domain has been confirmed with the messaging team', 'Section 10.1'),
    ('A test digest was received and read correctly', 'Section 15.5'),
    ('The service survives a reboot', 'systemctl reboot, then check /healthz'),
    ('Monitoring is watching /healthz', 'Your monitoring platform'),
    ('Log rotation is working', 'ls -l logs/'),
    ('The Vault token renewal plan is in place', 'Section 7.4'),
])

g.h2('20.2 Flip the switch')
g.code("""cd /genai/etc/scripts/aged_ticket_advisor
cp config/conf.json config/conf.json.shadow-backup

vi config/conf.json
#   "runtime": { "shadow_mode": false, ... }

python3 -m json.tool config/conf.json > /dev/null && echo 'valid'
sudo systemctl restart aged-ticket-advisor

curl -s http://localhost:8444/healthz | python3 -m json.tool
# "shadow_mode": false""")

g.h2('20.3 Confirm the first live digest')
g.code("""# After the first scheduled digest time
mysql -u sd_advisor -p sd_advisor_db -e "
  SELECT run_type, audience, recipient, ticket_count, status, sent_at
    FROM digest_run
   WHERE sent_at >= CURDATE()
   ORDER BY id DESC;"

# status must be SENT, not SUPPRESSED or FAILED""")

g.h2('20.4 Announce it')
g.p('Tell the team three things, in this order: suggestions are advisory and nothing has been '
    'changed in ServiceNow; recording a verdict is what keeps the accuracy honest; and the '
    'snooze button exists so the tool stops nagging about tickets they have already dealt '
    'with. The third point is what determines whether the tool is still in use in three '
    'months.')


# ========================================================== SECTION 21 =====
g.h1('21. Operations runbook')

g.h2('21.1 Daily checks')
g.table(['Check', 'Command'], [
    ['Service is running', 'systemctl status aged-ticket-advisor'],
    ['Health endpoint is green', 'curl -s localhost:8444/healthz'],
    ['Sync watermark is recent', "mysql -e \"SELECT * FROM sync_state\\G\""],
    ['Digest went out', 'SELECT * FROM digest_run WHERE sent_at >= CURDATE();'],
    ['No error spike', "grep -c ERROR logs/advisor.log"],
], widths=[5.0, 11.4], code_cols=(1,))

g.h2('21.2 Weekly checks')
g.table(['Check', 'Where'], [
    ['Accuracy metrics still above the gates', '/accuracy'],
    ['Review coverage has not collapsed', '/accuracy — low coverage invalidates the other numbers'],
    ['Backlog trend is moving the right way', 'Grafana, or the board totals'],
    ['Database growth is reasonable', 'Section 21.4'],
    ['No agent is consistently at the top of /agents', 'Coaching conversation, not a tool problem'],
], widths=[6.0, 10.4])

g.h2('21.3 Monthly checks')
g.table(['Task', 'Why'], [
    ['(AppRole) nothing — renewal is automatic', 'Confirm with: run.py doctor | grep vault'],
    ['(Static token only) renew it', 'A 768h period means expiry within about 32 days'],
    ['Rotate the ServiceNow integration password', 'Standard credential hygiene'],
    ['Review the system-account list', 'New integrations appear over time and silently skew the idle clock'],
    ['Review the attention weights with the leads', 'Priorities drift'],
    ['Check Azure OpenAI spend against the estimate', 'Catch a runaway loop or a cache that stopped hitting'],
], widths=[6.0, 10.4])

g.h2('21.4 Database growth')
g.p('ticket_signal is append-only and written on every stream tick, making it by far the '
    'fastest-growing table. The nightly maintenance job prunes rows older than 180 days.')
g.code("""SELECT table_name,
       ROUND(((data_length + index_length) / 1024 / 1024), 1) AS size_mb,
       table_rows
  FROM information_schema.TABLES
 WHERE table_schema = 'sd_advisor_db'
 ORDER BY (data_length + index_length) DESC;""")
g.rich([('To retain less, adjust the ', {}), ('retain_days', {'code': True}),
        (' default in ', {}), ('JobRunner.prune()', {'code': True}),
        (' in ', {}), ('scheduler/jobs.py', {'code': True}), ('.', {})])

g.h2('21.5 Useful commands')
g.table(['Task', 'Command'], [
    ['Force a full sync now', 'python run.py sync --full'],
    ['Recompute all signals', 'python run.py signals'],
    ['Re-analyse ignoring the cache', 'python run.py recommend --force'],
    ['Send the digest now', 'python run.py digest'],
    ['Render the digest without sending', 'python run.py preview -o /tmp/d.html'],
    ['Rebuild the evidence index', 'python run.py backfill --days 180'],
    ['Check every dependency', 'python run.py doctor'],
    ['Verify the SLA definition map', 'python run.py sla-map'],
    ['Add a user', 'python run.py adduser --username x --role LEAD'],
    ['Change a password', 'python run.py passwd --username x'],
    ['Follow the logs', 'journalctl -u aged-ticket-advisor -f'],
], widths=[5.6, 10.8], code_cols=(1,))

g.h2('21.6 Log locations')
g.table(['Log', 'Path'], [
    ['Application log (rotated daily, 30 days)', 'logs/advisor.log'],
    ['systemd journal', 'journalctl -u aged-ticket-advisor'],
    ['Backfill log (if you used tee)', 'logs/backfill.log'],
    ['Delivery history', 'digest_run table'],
    ['Alert history', 'alert_log table'],
], widths=[6.4, 10.0], code_cols=(1,))


# ========================================================== SECTION 22 =====
g.h1('22. Troubleshooting')

g.h2('22.1 Service will not start')
g.table(['Symptom', 'Cause', 'Fix'], [
    ['Exits immediately, ExecStartPre failed', 'doctor is failing',
     'Run it manually as the genai user and read the output'],
    ['Permission denied on a Vault credential file', 'Wrong file owner or mode',
     'chown genai and chmod 600 on .vault_role_id and .vault_secret_id'],
    ['certificate verify failed: IP address mismatch',
     'vault.url uses an IP (or a name) that the Vault TLS certificate does not cover. '
     'The trust chain is fine — only the name check failed',
     'Set vault.url to a name from the certificate\'s SANs (Section 3.1). Do NOT reach for '
     'verify_tls: false — that discards the check entirely rather than fixing it'],
    ['source address "127.0.0.1" unauthorized by CIDR restrictions',
     'The role is bound to the LAN address, but Vault is on this host so the connection '
     'arrives over loopback',
     'Add 127.0.0.1/32 to secret_id_bound_cidrs AND token_bound_cidrs, re-supplying every '
     'other role parameter, then generate a NEW secret_id — existing ones keep the old '
     'CIDR list'],
    ['AppRole login failed: invalid role or secret ID', 'secret_id destroyed or expired',
     'Generate a new secret_id (Section 7.7)'],
    ['Repeated "logging in again" in the log', 'token_max_ttl is very short relative to '
     'token_ttl', 'Expected at each max_ttl boundary; frequent enough to be noisy means '
                  'the role TTLs need widening'],
    ['Address already in use', 'Port 8444 taken', 'ss -tlnp | grep 8444'],
    ['ModuleNotFoundError', 'Wrong virtual environment in the unit file',
     'Check the ExecStart path'],
], widths=[4.4, 4.4, 7.6])

g.h2('22.2 No tickets appear on the board')
g.code("""# 1. Did the sync run and store anything?
mysql -u sd_advisor -p sd_advisor_db -e "
  SELECT COUNT(*) AS total, SUM(active) AS active FROM watched_ticket;
  SELECT * FROM sync_state\\G"

# 2. Does ServiceNow return anything for the configured scope?
python run.py sync --full

# 3. Are any tickets actually old enough?
mysql -u sd_advisor -p sd_advisor_db -e "
  SELECT COUNT(*) FROM watched_ticket
   WHERE active = 1 AND opened_at <= NOW() - INTERVAL 5 DAY;"

# 4. Is every configured group name a real one? (fails loudly if not)
python run.py doctor | grep "snow groups"

# 5. What are the real names, and how much traffic does each carry?
python ops/find_groups.py "Service Desk"

# 6. Still nothing? Take the incident query apart clause by clause.
python ops/probe_resolved.py --days 30""")
g.callout('warn', 'Most common cause.',
          'servicenow.assignment_groups does not match sys_user_group.name exactly — a '
          'trailing space, different capitalisation, or an abbreviation such as "Service Desk" '
          'where the group is really "IT Service Desk". The comparison is exact, and a '
          'near-miss returns HTTP 200 with an empty result rather than an error, so it '
          'presents as a quiet day rather than as a fault. The "snow groups" check in '
          'run.py doctor exists specifically to turn this into a visible failure.')

g.h3('22.2.1 Why a wrong group name is silent')
g.p('Two ServiceNow behaviours combine to hide this, and they fail in opposite directions. '
    'Knowing which one you are looking at saves a lot of time.')
g.table(
    ['Situation', 'What ServiceNow does', 'What you see'],
    [
        ['Valid query, no matching records',
         'Returns an empty result set, HTTP 200',
         'Zero tickets — looks like a quiet queue'],
        ['Unparseable condition',
         'Drops that condition and runs the rest',
         'Too many tickets — a filter that appears to be off'],
    ],
    widths=[1.7, 2.1, 2.2])
g.p('A misspelled group name is the first row. A malformed operator — the wrong relative-date '
    'unit, for instance — is the second. Because neither raises, always compare a suspect '
    'query against an unfiltered baseline: if adding a clause does not change the count, that '
    'clause is not being applied.')

g.h2('22.3 Idle days look wrong')
g.p('Almost always a missing system account. Inspect who is actually touching the ticket:')
g.code("""python3 - <<'PY'
import sys; sys.path.insert(0, '.')
from core.context import get_context
from pipeline.signals import build_timeline

ctx = get_context()
t = ctx.db.query_one(
    "SELECT * FROM watched_ticket WHERE incident_number = %s", ('INC1234567',))
history = ctx.incidents.get_history(t['sys_id'])
for e in build_timeline(history, t['caller_name'] or '', ctx.settings.system_accounts)[-15:]:
    print(f"{e['when']}  {e['role']:<7} {e['actor']:<22} {e['kind']}")
PY""")
g.p('Any account showing as AGENT that is really an integration must be added to '
    'servicenow.system_accounts, then restart the service and re-run signals.')

g.h2('22.4 LLM problems')
g.table(['Message', 'Cause', 'Fix'], [
    ['Model did not return valid JSON', 'API version too old for structured outputs',
     'Use 2024-08-01-preview or later'],
    ['Model output truncated', 'Token limit hit', 'Reduce the timeline length in prompts.py'],
    ['Blocked by the Azure content filter', 'Ticket content tripped a filter',
     'Expected occasionally; the ticket is skipped and logged'],
    ['rate limit / 429', 'Quota exceeded', 'Lower llm.max_concurrency, or raise the Azure quota'],
    ['All recommendations say rule-fallback', 'The LLM client failed to construct',
     'Run doctor; check the Vault secret keys'],
], widths=[4.6, 4.4, 7.4])

g.h2('22.5 Digests not arriving')
g.code("""-- 1. Was anything even attempted?
SELECT * FROM digest_run ORDER BY id DESC LIMIT 10;""")
g.table(['digest_run status', 'Meaning', 'Next step'], [
    ['SUPPRESSED', 'Shadow mode is on, or mail.enabled is false', 'Expected during the pilot'],
    ['FAILED', 'SMTP rejected it — the error column has the detail', 'Section 10.2'],
    ['SENT', 'Handed to the relay successfully', 'Check the recipient\'s junk folder, then the relay logs'],
    ['No rows at all', 'The digest job did not run, or found nothing to send',
     'Check the scheduler started; check the board is not empty'],
], widths=[3.2, 5.6, 7.6], code_cols=(0,))

g.h2('22.6 Web UI problems')
g.table(['Symptom', 'Cause', 'Fix'], [
    ['Redirected to login repeatedly', 'Session cookie rejected',
     'If base_url says https but you browse over http, the Secure cookie is dropped'],
    ['403 on an action', 'VIEWER role', 'Needs LEAD or ADMIN'],
    ['Weights page will not save', 'Not an ADMIN', 'Use an admin account'],
    ['Activity timeline is empty', 'sys_history_line read denied', 'Section 8.3, check 2'],
], widths=[4.4, 4.0, 8.0])

g.h2('22.7 Escalation data to collect')
g.code("""cd /genai/etc/scripts/aged_ticket_advisor

python run.py doctor                              > /tmp/ata_diag.txt 2>&1
systemctl status aged-ticket-advisor --no-pager  >> /tmp/ata_diag.txt 2>&1
journalctl -u aged-ticket-advisor -n 300 --no-pager >> /tmp/ata_diag.txt 2>&1
tail -300 logs/advisor.log                       >> /tmp/ata_diag.txt 2>&1
python3 -c "import json;print(json.dumps(json.load(open('config/conf.json')),indent=2))" \\
                                                 >> /tmp/ata_diag.txt 2>&1

echo 'conf.json contains no secrets, but review /tmp/ata_diag.txt before sharing it.'""")


# ========================================================== SECTION 23 =====
g.h1('23. Rollback and uninstall')

g.h2('23.1 Pause without uninstalling')
g.code("""sudo systemctl stop aged-ticket-advisor
sudo systemctl disable aged-ticket-advisor""")
g.p('Nothing else is affected. The existing audit tool, its cron entry and its front end '
    'continue exactly as before.')

g.h2('23.2 Stop emails but keep the board')
g.p('Usually the better first response to a complaint. Set shadow_mode back to true and '
    'restart — leads keep the board, nobody gets mail.')
g.code("""vi config/conf.json     # "shadow_mode": true
sudo systemctl restart aged-ticket-advisor""")

g.h2('23.3 Full uninstall')
g.code("""# 1. Stop and remove the service
sudo systemctl stop aged-ticket-advisor
sudo systemctl disable aged-ticket-advisor
sudo rm /etc/systemd/system/aged-ticket-advisor.service
sudo systemctl daemon-reload

# 2. Back the data up first - the verdict history is the record of the trial
mysqldump -u <admin> -p sd_advisor_db > /backup/sd_advisor_db_$(date +%F).sql

# 3. Drop the database and user
mysql -u <admin> -p -e "
  DROP DATABASE sd_advisor_db;
  DROP USER 'sd_advisor'@'localhost';"

# 4. Revoke the Vault credentials
vault delete auth/approle/role/sd-advisor
vault kv metadata delete secret/sd_advisor_db
vault kv metadata delete secret/sd_advisor_llm
vault kv metadata delete secret/sd_advisor_web
vault kv metadata delete secret/snow_advisor
vault policy delete sd-advisor

# 5. Disable the ServiceNow integration account (in ServiceNow)

# 6. Remove the code
sudo rm -rf /genai/etc/scripts/aged_ticket_advisor

# 7. Close the firewall port
sudo firewall-cmd --permanent --remove-port=8444/tcp
sudo firewall-cmd --reload""")
g.callout('good', 'The existing audit tool is untouched by all of the above.',
          'No step in this uninstall references itsm_analytics, itsm_analytics_db, port 8443 '
          'or the existing cron entries.')


# ========================================================== APPENDICES =====
g.h1('Appendix A — Configuration worksheet')
g.p('Complete this before starting Section 4. Keep it with the change record.')
g.table(['#', 'Value', 'Where it comes from', 'Your value'], [
    ['1', 'Application server hostname', 'Your estate', ''],
    ['2', 'Service account (Linux)', 'stat on the existing install', ''],
    ['3', 'Python interpreter path', 'Section 5.1 or 5.2', ''],
    ['4', 'MySQL host', 'qa_conf.json → db_host', ''],
    ['5', 'MySQL password for sd_advisor', 'Generated in Section 6.1', 'in Vault'],
    ['6', 'Vault URL', 'qa_conf.json → vault_url', ''],
    ['7', 'Vault CA bundle path', 'startup_services.sh', ''],
    ['7b', 'AppRole CIDR list', '127.0.0.1/32 plus $(hostname -I) — see §7.4', ''],
    ['8', 'ServiceNow instance URL', 'itsm_configuration_table.itsm_url', ''],
    ['9', 'ServiceNow integration user', 'Created in Section 8.1', ''],
    ['10', 'ServiceNow password', 'Generated in Section 8.1', 'in Vault'],
    ['11', 'Azure OpenAI endpoint', '.oai_config.json', ''],
    ['12', 'Azure OpenAI API key', '.oai_config.json', 'in Vault'],
    ['13', 'Azure API version', 'Must be ≥ 2024-08-01-preview', ''],
    ['14', 'Chat deployment name', 'Azure portal', ''],
    ['15', 'Embedding deployment name', 'Azure portal — may need creating', ''],
    ['16', 'Embedding dimensions', 'From the Section 9.3 probe', ''],
    ['17', 'SMTP host and port', 'Known', 'mailhost.kohler.com:25, anonymous'],
    ['18', 'From address', 'Messaging team — CONFIRM the domain', ''],
    ['19', 'In-scope assignment groups', 'Confirmed with the leads', ''],
    ['20', 'System accounts', 'Section 3.4, confirmed with the leads', ''],
    ['21', 'Aged threshold (days)', 'Confirmed with the leads', ''],
    ['22', 'Digest times and timezones', 'Confirmed with the leads', ''],
    ['23', 'Lead usernames and emails', 'Confirmed with the leads', ''],
    ['24', 'Web base URL', 'Section 17.3', ''],
    ['25', 'SLA definition map verified', 'python run.py sla-map (Section 3.5)', ''],
], widths=[0.9, 4.6, 6.1, 4.8])

g.h1('Appendix B — Complete configuration key reference')
g.table(['Key', 'Type', 'Default', 'Required'], [
    ['customer_name', 'string', '—', 'Yes'],
    ['vault.url', 'string', '—', 'Yes'],
    ['vault.auth_method', 'token | approle', 'token', 'Recommended: approle'],
    ['vault.token_file', 'path', 'config/.vault_token', 'Only if auth_method is token'],
    ['vault.approle.role_id_file', 'path', 'config/.vault_role_id', 'If auth_method is approle'],
    ['vault.approle.secret_id_file', 'path', 'config/.vault_secret_id', 'If auth_method is approle'],
    ['vault.approle.write_token_file', 'bool', 'false', 'No'],
    ['vault.approle.renew_ratio', 'float', '0.5', 'No'],
    ['vault.approle.min_renew_seconds', 'int', '60', 'No'],
    ['vault.verify_tls', 'bool', 'true', 'No'],
    ['vault.ca_bundle', 'path', '—', 'No'],
    ['vault.paths.database', 'string', 'sd_advisor_db', 'Yes'],
    ['vault.paths.servicenow', 'string', '—', 'Yes'],
    ['vault.paths.smtp', 'string', 'sd_advisor_smtp', 'No — anonymous relay on this estate'],
    ['vault.paths.azure_openai', 'string', 'sd_advisor_llm', 'Yes'],
    ['vault.paths.web', 'string', 'sd_advisor_web', 'No — falls back to a generated file'],
    ['database.host / .name / .port', 'string / int', 'localhost / sd_advisor_db / 3306', 'Yes'],
    ['database.connect_timeout', 'int', '10', 'No'],
    ['audit_database.enabled', 'bool', 'false', 'No'],
    ['audit_database.host / .name / .vault_path', 'string', '—', 'If enabled'],
    ['servicenow.url', 'string', '—', 'Yes'],
    ['servicenow.verify_tls', 'bool', 'true', 'No'],
    ['servicenow.timeout_seconds', 'int', '60', 'No'],
    ['servicenow.page_size', 'int', '200', 'No'],
    ['servicenow.max_retries', 'int', '3', 'No'],
    ['servicenow.assignment_groups', 'list', '[]', 'Strongly recommended'],
    ['servicenow.system_accounts', 'list', '[]', 'Strongly recommended'],
    ['sla.include_types', 'list', '[] (no filter)', 'Recommended — ["SLA"]'],
    ['sla.definitions', 'object keyed by sys_id', '{}', 'Strongly recommended — see §3.5'],
    ['llm.chat_deployment', 'string', '—', 'Yes'],
    ['llm.embedding_deployment', 'string', '—', 'Yes'],
    ['llm.embedding_dims', 'int', '1536', 'Yes — must match the model'],
    ['llm.api_version', 'string', '2024-10-21', 'Yes'],
    ['llm.max_concurrency', 'int', '8', 'No'],
    ['llm.request_timeout_seconds', 'int', '90', 'No'],
    ['llm.max_retries', 'int', '3', 'No'],
    ['llm.min_confidence_to_surface', 'float', '0.55', 'No'],
    ['llm.min_similarity', 'float', '0.55', 'No'],
    ['thresholds.aged_after_days', 'int', '5', 'Yes'],
    ['thresholds.stagnation_days', 'int', '2', 'No'],
    ['thresholds.critical_stagnation_days', 'int', '4', 'No'],
    ['thresholds.sla_jeopardy_pct', 'int', '75', 'No'],
    ['thresholds.auto_close_followups', 'int', '3', 'No'],
    ['thresholds.auto_close_silence_days', 'int', '5', 'No'],
    ['thresholds.digest_max_tickets_per_agent', 'int', '25', 'No'],
    ['scheduler.stream_interval_minutes', 'int', '10', 'No'],
    ['scheduler.recommendation_interval_minutes', 'int', '60', 'No'],
    ['scheduler.stats_refresh_cron', 'cron', '0 2 * * *', 'No'],
    ['scheduler.digests', 'list of objects', '[]', 'Yes, for email delivery'],
    ['mail.enabled', 'bool', 'true', 'No'],
    ['mail.host / .port', 'string / int', 'mailhost.kohler.com / 25', 'If mail is enabled'],
    ['mail.use_starttls', 'bool', 'false on this estate', 'No — see §10.2'],
    ['mail.from_address', 'string', '—', 'If mail is enabled'],
    ['mail.reply_to', 'string', '—', 'No'],
    ['mail.always_bcc', 'list', '[]', 'No'],
    ['mail.subject_prefix', 'string', '[SD Advisor]', 'No'],
    ['web.base_url', 'string', '—', 'Yes — used in email links'],
    ['web.host / .port', 'string / int', '0.0.0.0 / 8444', 'No'],
    ['web.session_cookie', 'string', 'ata_session', 'No'],
    ['web.session_max_age_seconds', 'int', '43200', 'No'],
    ['web.secret_file', 'path', 'config/.session_secret', 'No'],
    ['runtime.shadow_mode', 'bool', 'true', 'No'],
    ['runtime.log_level', 'string', 'INFO', 'No'],
    ['runtime.log_dir', 'path', 'logs', 'No'],
    ['runtime.redact_before_llm', 'bool', 'true', 'No'],
], widths=[6.2, 3.0, 4.0, 3.2], font_size=8.6, code_cols=(0,))

g.h1('Appendix C — Vault secret reference')
g.table(['Path', 'Key format', 'Consumed by'], [
    ['secret/sd_advisor_db', '<username> = <password>  (single pair)', 'core/db.py'],
    ['secret/snow_advisor', '<username> = <password>  (single pair)', 'core/snow/base.py'],
    ['secret/sd_advisor_llm', 'azure_endpoint, azure_api_key, azure_api_version', 'core/llm/client.py'],
    ['secret/sd_advisor_smtp', 'NOT USED — mailhost.kohler.com:25 is anonymous', 'delivery/mailer.py'],
    ['secret/sd_advisor_web', 'secret_key, webhook_token (optional)', 'web/app.py'],
], widths=[4.4, 7.0, 5.0], code_cols=(0, 2))
g.callout('info', 'Both credential formats are accepted.',
          'For the database and ServiceNow secrets, either a single <username>=<password> pair '
          '(matching existing estate convention) or explicit username and password keys will '
          'work. Anything else is rejected with a clear error rather than failing later as an '
          'authentication problem.')

g.h1('Appendix D — Security checklist')
g.checklist([
    ('Service runs as an unprivileged account, not root', 'systemctl show -p User aged-ticket-advisor'),
    ('config directory is mode 700', 'ls -ld config'),
    ('AppRole is in use rather than a static token', 'run.py doctor | grep vault'),
    ('.vault_role_id and .vault_secret_id are mode 600, owned by the service account',
     'ls -l config/.vault_*'),
    ('The secret_id is CIDR-bound to this server (loopback + LAN)',
     'vault read auth/approle/role/sd-advisor'),
    ('Automatic renewal is running', 'journalctl -u aged-ticket-advisor | grep "token renew"'),
    ('conf.json is mode 600', 'ls -l config/conf.json'),
    ('Vault policy grants the advisor no access to audit-tool secrets', 'Section 7.5'),
    ('ServiceNow account has snc_read_only or equivalent', 'ServiceNow user record'),
    ('vault.verify_tls and servicenow.verify_tls are both true', 'conf.json'),
    ('runtime.redact_before_llm is true', 'conf.json'),
    ('Port 8444 is restricted to the corporate network', 'firewall-cmd --list-all'),
    ('TLS terminates in front of the application', 'Section 17.3'),
    ('No credential appears in conf.json', 'grep -iE "password|api_key|secret" config/conf.json'),
    ('Session secret is in Vault, or the generated file is mode 600', 'ls -l config/.session_secret'),
], title='Complete before go-live')

g.h1('Appendix E — Sign-off')
g.p('Record completion of each phase.')
g.table(['Phase', 'Sections', 'Completed by', 'Date', 'Notes'], [
    ['Environment discovery', '3', '', '', ''],
    ['Code deployed', '4–5', '', '', ''],
    ['Database configured', '6', '', '', ''],
    ['Vault secrets written', '7.1-7.3', '', '', ''],
    ['AppRole configured and renewing', '7.4-7.6', '', '', ''],
    ['ServiceNow configured', '8', '', '', ''],
    ['Azure OpenAI configured', '9', '', '', ''],
    ['SMTP configured', '10', '', '', ''],
    ['conf.json completed', '11', '', '', ''],
    ['doctor passes', '12', '', '', ''],
    ['Accounts created', '13', '', '', ''],
    ['Backfill complete', '14', '', '', ''],
    ['Manual pipeline verified', '15', '', '', ''],
    ['Service installed', '16', '', '', ''],
    ['Network and TLS complete', '17', '', '', ''],
    ['Shadow mode started', '19', '', '', ''],
    ['Go-live gates met', '20.1', '', '', ''],
    ['Live', '20.2', '', '', ''],
], widths=[4.2, 2.0, 3.4, 2.4, 4.4])

g.h1('Appendix F — Quick reference card')
g.p('Print this page and keep it near the console.', italic=True, color=MUTED)
g.code("""PATHS
  Project        /genai/etc/scripts/aged_ticket_advisor
  Config         config/conf.json
  Vault AppRole  config/.vault_role_id  +  config/.vault_secret_id
  Logs           logs/advisor.log
  Schema         db/schema.sql

VAULT (AppRole - renewal is automatic, nothing to schedule)
  python run.py doctor | grep vault
  journalctl -u aged-ticket-advisor | grep -i vault
  Rotate secret_id:
    NEW=$(vault write -f -field=secret_id auth/approle/role/sd-advisor/secret-id)
    install -m 600 /dev/stdin config/.vault_secret_id <<< "$NEW"
    systemctl restart aged-ticket-advisor      # then destroy the old accessor

SERVICE
  systemctl {start|stop|restart|status} aged-ticket-advisor
  journalctl -u aged-ticket-advisor -f

HEALTH
  curl -s localhost:8444/healthz | python3 -m json.tool
  python run.py doctor

PIPELINE (run from the project directory, venv activated)
  python run.py sync --full
  python run.py signals
  python run.py recommend [--force] [-n N]
  python run.py digest [--no-agents]
  python run.py preview -o /tmp/d.html
  python run.py backfill --days 180
  python run.py sla-map
  python run.py adduser --username X --role LEAD
  python run.py passwd  --username X

WEB
  /board              ranked review board
  /ticket/<INC>       drill-down, verdict, snooze, re-analyse
  /agents             per-agent rollup
  /accuracy           agreement, precision, coverage, silent breaches
  /settings/weights   tune the score (ADMIN)
  /healthz            monitoring endpoint

KEY SQL
  SELECT * FROM sync_state\\G
  SELECT * FROM digest_run ORDER BY id DESC LIMIT 10;
  SELECT incident_number, attention_score, ball_in_court
    FROM v_current_board ORDER BY attention_score DESC LIMIT 20;

EMERGENCY
  Stop emails, keep the board:   shadow_mode = true, then restart
  Stop everything:               systemctl stop aged-ticket-advisor
  (The existing itsm_analytics audit tool is unaffected either way)""")


# ---------------------------------------------------------------------------
if __name__ == '__main__':
    OUT.parent.mkdir(parents=True, exist_ok=True)
    path = g.save(OUT)
    size_kb = path.stat().st_size / 1024
    print(f'Written: {path}')
    print(f'Size:    {size_kb:.0f} KB')
    sys.exit(0)
