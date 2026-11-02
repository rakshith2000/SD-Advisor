"""What an administrator may change from the settings page.

An allow-list, not a deny-list. The difference matters: conf.json holds the
Vault address, the database host and the OIDC client registration alongside
the thresholds, and a page that rendered "everything except the bits we
remembered to hide" would expose the next key somebody adds to the file. A
key absent from SPECS below cannot be set, cannot be read back through the
settings page, and cannot be reached by posting a crafted form - the route
looks the key up here before it does anything else.

Each spec carries the type, so a value arriving from an HTML form - where
everything is a string - is parsed and bounded before it reaches the
database. "8" becomes 8; "eight" is refused with a message; 10_000 for a
percentage is refused by the bound rather than silently reordering the whole
board.

Each spec also carries `applies`, which is the honest part. Some settings are
read every time they are used and take effect on save. Others were read once
while the process was starting - the scheduler's intervals, the assignment
group list the ServiceNow reader was constructed with - and will not change
until it starts again. A settings page that pretends otherwise produces the
worst outcome available: somebody changes a value, sees it saved, and spends
an afternoon on why nothing happened.
"""

import json
from typing import Any, Dict, Iterable, List, Optional, Sequence

# applies
LIVE = 'live'
RESTART = 'restart'

# kinds
BOOL = 'bool'
INT = 'int'
NUMBER = 'number'
TEXT = 'text'
LIST = 'list'
CHOICE = 'choice'


class SettingError(ValueError):
    """A value the registry will not accept. Message is for the screen."""


class Spec:
    def __init__(self, key: str, label: str, kind: str, section: str,
                 help: str = '', applies: str = LIVE,
                 minimum: Optional[float] = None, maximum: Optional[float] = None,
                 choices: Optional[Sequence[str]] = None,
                 placeholder: str = '', warning: str = ''):
        self.key = key
        self.label = label
        self.kind = kind
        self.section = section
        self.help = help
        self.applies = applies
        self.minimum = minimum
        self.maximum = maximum
        self.choices = list(choices) if choices else None
        self.placeholder = placeholder
        # Shown in red next to the control. Reserved for settings whose
        # wrong value is not merely wrong but actively harmful.
        self.warning = warning

    # -- parsing -----------------------------------------------------------

    def parse(self, raw: Any) -> Any:
        """Turn a form value into the stored type, or raise SettingError."""
        if self.kind == BOOL:
            # An unchecked checkbox is absent from the form, not false, so
            # the route passes an explicit present/absent flag.
            return bool(raw)

        text = '' if raw is None else str(raw).strip()

        if self.kind == LIST:
            items = [part.strip() for part in text.replace('\n', ',').split(',')]
            return [part for part in items if part]

        if self.kind == CHOICE:
            if text not in (self.choices or []):
                raise SettingError(
                    f'{self.label}: {text or "(blank)"} is not one of '
                    f'{", ".join(self.choices or [])}.')
            return text

        if self.kind in (INT, NUMBER):
            if text == '':
                raise SettingError(f'{self.label} cannot be blank.')
            try:
                value = int(text) if self.kind == INT else float(text)
            except ValueError:
                raise SettingError(
                    f'{self.label}: {text!r} is not a '
                    f'{"whole number" if self.kind == INT else "number"}.')
            if self.minimum is not None and value < self.minimum:
                raise SettingError(
                    f'{self.label} must be at least {self._fmt(self.minimum)}.')
            if self.maximum is not None and value > self.maximum:
                raise SettingError(
                    f'{self.label} must be at most {self._fmt(self.maximum)}.')
            return value

        return text

    @staticmethod
    def _fmt(value: float) -> str:
        return str(int(value)) if float(value).is_integer() else str(value)

    # -- rendering ---------------------------------------------------------

    def display(self, value: Any) -> str:
        if value is None:
            return ''
        if self.kind == LIST:
            return ', '.join(str(v) for v in value) if isinstance(value, list) else str(value)
        if self.kind == BOOL:
            return 'On' if value else 'Off'
        return str(value)


# ---------------------------------------------------------------------------
# the allow-list
# ---------------------------------------------------------------------------

# (key, label, blurb, icon). The icon is a plain character rather than an
# icon font or an SVG sprite: the page has seven of them, they are decorative
# beside a written label, and neither a webfont request nor a build step is
# worth paying for that.
SECTIONS = [
    ('review', 'Review policy',
     'What counts as aged, idle or at risk. These drive the attention score, '
     'so a change here reorders the board for everybody.', '◔'),
    ('queues', 'Assignment groups',
     'Which queues this service covers. Everything else is scoped to these.',
     '☷'),
    ('mail', 'Email delivery',
     'Who the service appears to be, and whether it is actually sending.',
     '✉'),
    ('access', 'Access requests',
     'The self-service workflow on the profile page.', '⚿'),
    ('display', 'Presentation',
     'Wording and timestamps. No effect on what is stored or compared.',
     '◳'),
    ('schedule', 'Schedule',
     'How often each background pass runs.', '⟳'),
    ('model', 'Recommendations',
     'Thresholds on the language model output.', '◇'),
]

SECTION_KEYS = [key for key, _label, _blurb, _icon in SECTIONS]

SPECS: List[Spec] = [
    # -- review policy ----------------------------------------------------
    Spec('thresholds.aged_after_days', 'Aged after (days)', INT, 'review',
         'A ticket older than this is on the board. The single number that '
         'decides how much work the board shows.',
         minimum=1, maximum=365),
    Spec('thresholds.inactivity_days', 'Idle after (days)', INT, 'review',
         'No action from the assigned team for this long counts as idle.',
         minimum=1, maximum=90),
    Spec('thresholds.prolonged_inactivity_days', 'Prolonged inactivity (days)',
         INT, 'review',
         'Raises the Prolonged inactivity flag and a risk alert. Must be '
         'larger than the idle threshold or the two flags fire together.',
         minimum=1, maximum=180),
    Spec('thresholds.sla_risk_pct', 'SLA at risk (% consumed)', INT, 'review',
         'How much of the resolution target must be used up before a ticket '
         'is flagged as at risk.',
         minimum=1, maximum=100),
    Spec('thresholds.closure_followup_count', 'Follow-ups before closure candidate',
         INT, 'review',
         'Documented chases with no caller reply before a ticket is offered '
         'as a closure candidate.',
         minimum=1, maximum=20),
    Spec('thresholds.closure_silence_days', 'Caller silence before closure (days)',
         INT, 'review', minimum=1, maximum=120),
    Spec('thresholds.digest_max_tickets_per_agent', 'Tickets per digest', INT,
         'review',
         'Cap on the ranked list in each email. The rest are counted, not listed.',
         minimum=1, maximum=200),

    # -- queues -----------------------------------------------------------
    Spec('servicenow.assignment_groups', 'Assignment groups covered', LIST,
         'queues',
         'One per line, spelled exactly as in ServiceNow. A near-miss matches '
         'no tickets and shows as an empty board rather than an error - run '
         'doctor after changing this.',
         applies=RESTART,
         warning='Removing a queue stops its tickets being synced, scored and '
                 'mailed. The rows already on the board stay until the next '
                 'full sync retires them.'),

    # -- mail --------------------------------------------------------------
    Spec('runtime.shadow_mode', 'Shadow mode', BOOL, 'mail',
         'On: everything is computed, rendered and recorded, and nothing '
         'leaves the box. Off: mail goes to real people.',
         warning='Turning this OFF starts live delivery to every lead and '
                 'viewer in scope, immediately, on the next scheduled pass.'),
    Spec('mail.enabled', 'Mail enabled', BOOL, 'mail',
         'Off stops this host talking to the relay at all - absolute, and not '
         'escaped by the shadow-mode exemptions.'),
    Spec('mail.from_name', 'From name', TEXT, 'mail',
         'The display name on every outbound message. Keep it queue-neutral.',
         placeholder='Aged Ticket Advisor'),
    Spec('mail.subject_prefix', 'Subject prefix', TEXT, 'mail',
         'Prepended to every subject, so a mailbox rule can find them.',
         placeholder='[SD Advisor]'),
    Spec('mail.reply_to', 'Reply-to address', TEXT, 'mail',
         'Where a reply goes. Blank means replies come back to the From '
         'address, which is usually unmonitored.'),
    Spec('mail.always_bcc', 'Always blind-copy', LIST, 'mail',
         'Added to the envelope of every message and to no header. One per line.',
         warning='These addresses receive incident descriptions and caller '
                 'names from every queue.'),

    # -- access ------------------------------------------------------------
    Spec('auth.role_requests.enabled', 'Self-service requests', BOOL, 'access',
         'Off removes both request forms from the profile page. Existing '
         'pending requests can still be decided.'),
    Spec('auth.role_requests.require_justification_chars', 'Minimum reason length',
         INT, 'access',
         'What the administrator has to judge the request on.',
         minimum=0, maximum=2000),
    Spec('auth.role_requests.expire_after_days', 'Requests expire after (days)',
         INT, 'access',
         'An undecided request closes itself, so the badge reflects what is '
         'actually waiting rather than everything ever asked.',
         minimum=1, maximum=365),
    Spec('auth.role_requests.reject_cooldown_days', 'Wait after a decline (days)',
         INT, 'access',
         'Applies only to the queues that were declined. Zero disables it.',
         minimum=0, maximum=365),

    # -- display -----------------------------------------------------------
    Spec('display.team_label', 'Name for the owning team', TEXT, 'display',
         'What the board and the emails call the team that owes the next '
         'action, across however many queues are configured.',
         placeholder='Assigned team'),
    Spec('display.timezone', 'Service timezone', TEXT, 'display',
         'An IANA name - America/Chicago, Europe/London. Never a fixed '
         'offset: that would be an hour out for part of the year.',
         placeholder='America/Chicago'),
    Spec('display.allow_user_timezone', 'Let people choose their own timezone',
         BOOL, 'display',
         'Adds the picker to the navigation bar. Emails always use the '
         'service timezone - one message is rendered for everybody.'),
    Spec('display.datetime_format', 'Date and time format', TEXT, 'display',
         'strftime, for example %d %b %Y %H:%M.',
         placeholder='%d %b %Y %H:%M'),

    # -- schedule ----------------------------------------------------------
    Spec('scheduler.stream_interval_minutes', 'Delta sync every (minutes)',
         INT, 'schedule',
         'Sync, signals and risk alerts. No model calls, so this is cheap.',
         applies=RESTART, minimum=1, maximum=1440),
    Spec('scheduler.recommendation_interval_minutes', 'Full pass every (minutes)',
         INT, 'schedule',
         'Full sync, signals, then the model pass over whatever changed.',
         applies=RESTART, minimum=5, maximum=1440),
    Spec('scheduler.hourly_full_sync', 'Full sync on the hourly pass', BOOL,
         'schedule',
         'Only the full pass notices tickets that have been resolved or moved '
         'to another team. Off leaves that to the daily digest.',
         applies=RESTART),

    # -- model -------------------------------------------------------------
    Spec('llm.min_confidence_to_surface', 'Minimum confidence to surface',
         NUMBER, 'model',
         'Below this a recommendation is marked as needing review rather '
         'than presented as a suggestion.',
         minimum=0, maximum=1),
    Spec('llm.min_similarity', 'Minimum evidence similarity', NUMBER, 'model',
         'How close a resolved incident has to be before it is offered as '
         'evidence.',
         applies=RESTART, minimum=0, maximum=1),
]

BY_KEY: Dict[str, Spec] = {spec.key: spec for spec in SPECS}

# Prefixes that must never become editable, asserted by the test suite rather
# than merely intended. Everything here either carries a credential, points at
# one, or has to be correct before the database this page writes to can be
# reached at all.
FORBIDDEN_PREFIXES = (
    'vault.', 'database.', 'audit_database.', 'llm.api_key', 'llm.endpoint',
    'auth.oidc.client_secret', 'auth.oidc.issuer', 'auth.oidc.client_id',
    'auth.oidc.redirect_uri', 'auth.reconcile.client_id',
    'web.host', 'web.port', 'web.base_url', 'web.session_secret',
    'servicenow.url', 'servicenow.instance', 'customer_name',
)


def editable_keys() -> List[str]:
    return [spec.key for spec in SPECS]


def section_specs(section: str) -> List[Spec]:
    return [spec for spec in SPECS if spec.section == section]


def section_summaries(settings, current: Optional[str] = None) -> List[Dict[str, Any]]:
    """One entry per section, for the grid of cards at the top of the page.

    The grid exists because the question that page answers is "what can I
    change here". A reader who cannot see the whole answer at once assumes
    the rest is somewhere else - which, before this page existed, meant SSH.

    Each card carries how many of its settings have been changed from the
    file, so the one section somebody has been editing is findable without
    opening all seven.
    """
    overlay = getattr(settings, 'overlay', None)
    active = overlay.current() if overlay is not None else {}

    summaries = []
    for key, label, blurb, icon in SECTIONS:
        specs = section_specs(key)
        overridden = [s for s in specs if s.key in active]
        summaries.append({
            'key': key,
            'label': label,
            'blurb': blurb,
            'icon': icon,
            'count': len(specs),
            'overridden': len(overridden),
            'needs_restart': any(s.applies == RESTART for s in overridden),
            'active': key == current,
        })
    return summaries


def restart_pending(settings) -> List[Spec]:
    """Overridden settings whose new value is not in force yet.

    Shown as a banner. Without it the page is a convincing liar: the value
    reads back correctly from the form and the running service is still using
    the one it started with.
    """
    overlay = getattr(settings, 'overlay', None)
    if overlay is None:
        return []
    active = overlay.current()
    return [spec for spec in SPECS
            if spec.applies == RESTART and spec.key in active]


def rows_for(settings, section: str) -> List[Dict[str, Any]]:
    """Everything the template needs to render one section."""
    overlay = getattr(settings, 'overlay', None)
    active = overlay.current() if overlay is not None else {}

    rows = []
    for spec in section_specs(section):
        overridden = spec.key in active
        rows.append({
            'spec': spec,
            'value': settings.get(spec.key),
            'file_value': settings.file_value(spec.key),
            'overridden': overridden,
            # Shown only when it differs, so the page explains itself at
            # exactly the point where the file and the database disagree.
            'differs_from_file': (overridden
                                  and active.get(spec.key)
                                  != settings.file_value(spec.key)),
        })
    return rows


def apply_form(settings, form: Dict[str, Any], section: str,
               changed_by: str) -> Dict[str, Any]:
    """Validate and store one section's worth of changes.

    Every value is parsed before any value is written. A form with one bad
    field changes nothing - a partial save would leave the administrator
    reading an error beside a page that had already half-applied.
    """
    overlay = getattr(settings, 'overlay', None)
    if overlay is None:
        raise SettingError(
            'Settings storage is unavailable, so nothing was changed. Apply '
            'db/migrations/007_admin_settings.sql and try again.')

    specs = section_specs(section)
    if not specs:
        raise SettingError(f'{section} is not a settings section.')

    parsed: Dict[str, Any] = {}
    for spec in specs:
        if spec.kind == BOOL:
            # Checkboxes post nothing when unticked, so presence is the value
            # and every checkbox in the section is always decided.
            parsed[spec.key] = spec.parse(form.get(spec.key) is not None)
            continue
        if spec.key not in form:
            continue
        parsed[spec.key] = spec.parse(form.get(spec.key))

    active = overlay.current()
    changed, cleared = [], []

    for key, value in parsed.items():
        file_value = settings.file_value(key)
        if key in active and active[key] == value:
            continue
        if key not in active and _same(file_value, value):
            continue

        # Back to what the file says: drop the override rather than storing a
        # duplicate of it. Keeps "overridden" meaning "differs from the file".
        if _same(file_value, value):
            overlay.clear(key, changed_by)
            cleared.append(key)
        else:
            overlay.set(key, value, changed_by)
            changed.append(key)

    return {'changed': changed, 'cleared': cleared,
            'restart': [k for k in changed + cleared
                        if BY_KEY[k].applies == RESTART]}


def _same(a: Any, b: Any) -> bool:
    """Equality that survives a round trip through an HTML form."""
    if isinstance(a, list) or isinstance(b, list):
        return [str(x) for x in (a or [])] == [str(x) for x in (b or [])]
    if isinstance(a, bool) or isinstance(b, bool):
        return bool(a) == bool(b)
    if isinstance(a, (int, float)) and isinstance(b, (int, float)):
        return float(a) == float(b)
    return (a if a is not None else '') == (b if b is not None else '')


def export_json(settings) -> str:
    """conf.json as the service is actually running it."""
    return json.dumps(settings.effective_dict(), indent=4) + '\n'
