# `ata-login` — Keycloak login theme

Skins the Keycloak sign-in page to match the Aged Ticket Advisor board, so
someone arriving from a digest link does not feel handed off to a different
system mid-flow.

## What this is, and what it deliberately is not

A **CSS and message overlay over the stock `keycloak` theme**. It does not
fork `login.ftl`, `template.ftl` or any other FreeMarker template.

That matters: those templates carry the CSRF token field, the PKCE and state
handling, the brute-force lockout messaging and the password-policy rendering.
A forked copy keeps working after a Keycloak upgrade while quietly missing
whatever was fixed in the original — and nothing reports that. Overlaying means
every security fix arrives with the upgrade.

The cost is that this theme can only restyle and reword what the parent
renders. In practice that has been enough.

```
ata-login/
└── login/
    ├── theme.properties                 parent=keycloak, stylesheet order
    ├── messages/messages_en.properties  branding and wording
    └── resources/css/ata.css            layered over the parent's login.css
```

## Install

### 1. Copy to the Keycloak host

```bash
sudo mkdir -p /opt/keycloak/themes
sudo cp -r ata-login /opt/keycloak/themes/
sudo chown -R keycloak:keycloak /opt/keycloak/themes/ata-login
sudo find /opt/keycloak/themes/ata-login -type d -exec chmod 755 {} \;
sudo find /opt/keycloak/themes/ata-login -type f -exec chmod 644 {} \;
```

Container deployments mount it instead:

```yaml
volumes:
  - ./ata-login:/opt/keycloak/themes/ata-login:ro
```

### 2. Restart Keycloak

```bash
sudo systemctl restart keycloak
```

Themes are discovered at startup. A new theme will not appear in the admin
console dropdown until Keycloak has restarted.

### 3. Assign it to the client — not to the realm

**Clients → `ata-web` → Settings → Login theme → `ata-login` → Save**

Setting it under *Realm settings → Themes* would re-skin the login page for
**every** application in the AIOT realm, including ATK. Keep it on the client.

### 4. Check

Open `https://uswix865.kohlerco.com/oidc/login` in a private window. You should
see the Advisor wordmark, the Entra button reading *Continue with…*, and the
card matching the board's surface and border colours.

## Development loop

Theme caching is on by default, so edits appear only after a restart. While
iterating:

```bash
# Keycloak 17+ (Quarkus)
bin/kc.sh start-dev \
  --spi-theme-static-max-age=-1 \
  --spi-theme-cache-themes=false \
  --spi-theme-cache-template=false
```

**Turn these off again for production** — leaving them disabled makes Keycloak
re-read and re-parse every theme file on every request.

```bash
bin/kc.sh build --spi-theme-cache-themes=true
# and in conf/keycloak.conf
spi-theme-static-max-age=2592000
```

## Verify the properties file is still ASCII

Keycloak reads message bundles as **ISO-8859-1**. A literal `·`, `–` or `’`
renders as mojibake on the live login page — and only on the characters
affected, so it is easy to miss in review. Non-ASCII must be a `\uXXXX`
escape.

```bash
LC_ALL=C grep -n '[^[:print:][:space:]]' login/messages/messages_en.properties \
  && echo 'FAIL: non-ASCII above' || echo 'OK: pure ASCII'
```

## If an upgrade leaves the page unstyled

`styles` in `theme.properties` **replaces** the inherited value rather than
appending to it, so the parent's stylesheet is named explicitly:

```properties
styles=css/login.css css/ata.css
```

If a Keycloak upgrade adds an entry to the parent's list, that entry is lost
here and the page loses its base styling. Compare and re-add:

```bash
unzip -p /opt/keycloak/lib/lib/main/org.keycloak.keycloak-themes-*.jar \
  theme/keycloak/login/theme.properties | grep '^styles'
```

Our file is named `ata.css`, not `login.css`, on purpose — Keycloak resolves
resources child-first, so a file with the parent's name would shadow it
instead of layering over it.

## Keycloak version

Written against the `keycloak` login theme as shipped in Keycloak 20–26. The
selectors used (`#kc-form-login`, `#kc-login`, `.card-pf`, `#kc-header-wrapper`,
`#kc-social-providers`, `.login-pf-page`) have been stable across that range.

Keycloak 26.2 introduced a second login theme, `keycloak.v2`, with different
markup. If your realm uses it, either keep `parent=keycloak` (this theme
continues to work against the older base) or switch the parent and expect to
revisit the selectors.

```bash
bin/kc.sh --version
```

## Customising

**Logo.** Drop a file at `login/resources/img/logo.svg` and reference it from
the `.ata-brand` rule in `ata.css`. A favicon at `login/resources/img/favicon.ico`
is picked up automatically — the parent template already points at that path,
and a child file shadows the parent's.

**Wording.** Everything user-visible is in `messages_en.properties`. Keys not
listed there fall through to the parent, so new Keycloak screens still render
in English rather than showing raw message keys.

**Colours.** The tokens at the top of `ata.css` are copied verbatim from
`web/static/app.css`. If the board's palette changes, change them here too —
they are the only reason the two pages look like one product.

## Two things worth knowing

**The error messages are uniform on purpose.** `invalidUserMessage`,
`invalidUsernameOrEmailMessage` and `invalidPasswordMessage` are all set to the
same text. A message that distinguishes "no such user" from "wrong password"
turns the login form into a way of enumerating who works here.

**The realm name is not displayed.** The stock header prints the realm display
name — "AIOT" — which means nothing to a service desk lead and leaks that the
realm is shared. `loginTitleHtml` replaces it with the product name.
