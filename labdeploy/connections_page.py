"""The account and destination setup screen; all provider data is escaped before rendering."""
from urllib.parse import urlencode, urlparse

from . import settings_page as ui
from .connections import CALLBACK


def action(visit, name, label, extra=None):
    hidden = {"csrf": visit.csrf, "tab": "connections", "action": name, **(extra or {})}
    return (f'<form method="post" action="{ui.PREFIX}/connections/action">'
            + "".join(f'<input type="hidden" name="{ui.esc(key)}" value="{ui.esc(value)}">' for key, value in hidden.items())
            + f'<button class="btn" type="submit">{ui.esc(label)}</button></form>')


def link(url, label):
    return f'<a class="btn" href="{ui.esc(url)}" target="_blank" rel="noopener noreferrer">{ui.esc(label)} {ui.icon("external")}</a>'


def next_step(step, label):
    return f'<a class="btn primary" href="{ui.PREFIX}?tab=connections&amp;step={step}#{step}">{ui.esc(label)}</a>'


def panel(step, number, title, summary, body, current):
    opened = ' open' if step == current else ''
    return (f'<details class="card setup-step" id="{step}"{opened}><summary>'
            f'<span class="step-number">{number}</span><span><strong>{title}</strong><span class="step-caption">{summary}</span></span>'
            '<span class="step-toggle" aria-hidden="true">⌄</span></summary><div class="card-body">' + body + '</div></details>')


def details(title, body, opened=False):
    return f'<details class="extra-options"{" open" if opened else ""}><summary>{ui.esc(title)}</summary>{body}</details>'


def problem(message):
    """Keep the precise provider diagnostic available without making it the main instruction."""
    text = str(message)
    hints = (
        ("expired", "This check or connection has expired. Check again or reconnect your account."),
        ("already used", "This request was already used. Check the app again to continue."),
        ("Settings changed", "Settings changed. Check the app again to review your current choices."),
        ("Access was refused", "Access was refused. Check that your account can use this project or code."),
        ("billing", "Billing is not enabled for this project. Ask the project owner to enable it in Google Cloud."),
        ("could not be reached", "We could not reach Google or GitHub. Please try again shortly."),
        ("Select and save", "First, select and save access to this app in step 2."),
        ("already belongs", "This name is already in use. Choose another name under More options."),
        ("foundation", "This project is not ready to run apps yet. Ask your server administrator to finish its setup."),
        ("another project", "The server setup does not match this project. Ask your server administrator to check it."),
        ("Releases are disabled", "App launches are paused or disabled. Your server administrator can enable them in Service settings."),
    )
    hint = next((hint for match, hint in hints if match.lower() in text.lower()),
                "This step could not finish. Check your choices. If it keeps happening, share the details below with your server administrator.")
    return ui.callout("amber", hint, details("View error details", f'<p dir="auto">{ui.esc(text)}</p>'))


def settings_review(ctx, back, change, change_id, old, new, diff_lines):
    body = ui.head("Save these choices?", "Nothing has changed yet. Save to continue to the next step.")
    entries = []
    if any(key.startswith("onboarding.") for key in change.keys):
        entries.append(("Google project for new apps", new["onboarding"]["project_id"]))
    preferences = new.get("connections", {})
    if "connections.google_account" in change.keys:
        entries.append(("Selected Google account", preferences.get("google_account") or "Not selected"))
    if "connections.repositories" in change.keys:
        selected = preferences.get("repositories")
        entries.append(("Allowed code repositories", ", ".join(selected) if selected else
                        "Only the files needed to operate this service" if selected == [] else "Everything allowed by the GitHub connection"))
        body += '<p class="help">The setup files for this service stay accessible. Running apps will keep running.</p>'
    if "connections.google_client_id" in change.keys:
        entries.append(("Google client ID", preferences.get("google_client_id") or "Remove connection"))
    if any(key.startswith("github_app") for key in change.keys):
        entries.append(("GitHub App ID", new["github_app"]["app_id"]))
        body += ui.callout("amber", "After saving, your server administrator needs to restart the server.")
    body += ui.card("Your choices", '<dl class="facts">' + ''.join(
        f'<div><dt>{ui.esc(label)}</dt><dd><bdi>{ui.esc(value)}</bdi></dd></div>' for label, value in entries) + '</dl>')
    body += details("Technical details", '<pre dir="ltr">' + ui.esc('\n'.join(diff_lines)) + '</pre>')
    body += (f'<form method="post" action="{ui.PREFIX}/confirm"><input type="hidden" name="csrf" value="{ui.esc(ctx.visit.csrf)}">'
             f'<input type="hidden" name="change" value="{ui.esc(change_id)}"><div class="actions">'
             '<button class="btn primary" type="submit">Save choices</button>'
             f'<a class="btn secondary" href="{ui.esc(back)}">Go back and edit</a></div></form>')
    return ui.shell(ctx, "connections", '<div class="connections">' + body + '</div>')


def settings_result(ctx, back, outcome):
    keys = outcome["keys"]
    body = ui.head("Choices saved", "")
    if not outcome["reloaded"]:
        body += ui.callout("amber", "Saved, but the server has not applied the new settings. Ask your server administrator to check it.")
    elif outcome["notes"]:
        body += ui.callout("amber", "Saved. Some changes need a server restart before they take effect.")
    else:
        body += ui.callout("green", "Your new settings are ready to use.")
    if outcome["failed"]:
        body += ui.callout("amber", "Settings were saved, but the backup failed. Ask your server administrator to check it.")
    if outcome["reloaded"]:
        if "connections.repositories" in keys:
            body += next_step("application-setup", "Next: check and launch")
        elif any(key.startswith("onboarding.") or key == "connections.google_account" for key in keys):
            body += next_step("github", "Next: choose GitHub access")
        else:
            body += next_step("google", "Back to account connections")
    else:
        body += f'<a class="btn" href="{ui.esc(back)}">Back to settings</a>'
    body += details("Save details for your administrator", '<div dir="ltr">' + ui.outcome_body(outcome) + '</div>')
    return ui.shell(ctx, "connections", '<div class="connections">' + body + '</div>')


def page(ctx, submitted=None, found=None):
    config, visit = ctx.config, ctx.visit
    snapshot = ctx.connections or {"accounts": [], "github": None, "google_ready": False}
    bag = ui.Problems(found)
    action_name = submitted.get("action", "") if submitted else ""
    form_name = submitted.get("form", "") if submitted else ""
    current = snapshot.get("step")
    if action_name.startswith("google-") or form_name == "destination":
        current = "google"
    elif action_name == "github-refresh" or form_name == "repositories":
        current = "github"
    elif action_name in ("check-repository", "start-application") or (not current and (snapshot.get("review") or snapshot.get("onboarding"))):
        current = "application-setup"
    if current not in ("google", "github", "application-setup"):
        current = "google" if not config.onboarding else "github" if config.connections.repositories is None else "application-setup"
    body = ui.head("Get your app running", "Choose your account, choose your code, then check and launch.")
    if found:
        body += ''.join(problem(message) for message in found.values())
    if snapshot.get("notice"):
        notices = {
            "GitHub access refreshed. Select repositories below and save your selection.": "List updated. Choose which code this app may access, then save.",
            "Account disconnected from this session. Saved destinations and running apps are unchanged.": "Account disconnected. Your running apps are unchanged.",
            "Settings changed. Check the repository again to review the current destination and permissions.": "Settings changed. Check the app again.",
        }
        body += ui.callout("green", notices.get(snapshot["notice"], snapshot["notice"]), "")
    if snapshot.get("error"):
        body += problem(snapshot["error"])
    destination = config.onboarding
    summary = '<p>' + (f'<strong>{ui.esc(destination.project_id)}</strong> · {ui.esc(destination.region)} · Test environment' if destination else "No destination selected") + '</p>'
    if config.connections.google_account:
        summary += f'<p class="help">Selected using {ui.esc(config.connections.google_account)}. Apps run using the deployment access set up for this project.</p>'
    google = summary
    if destination:
        google += '<div class="actions">' + next_step("github", "Use this saved project and continue") + '</div>'
    google += '<p>Connect your Google account, then choose the project where your app will run.</p>'
    google += '<p class="help">A Google Cloud project groups your app and its cloud costs.</p>'
    if snapshot["google_ready"]:
        google += action(visit, "google-connect", "Connect another Google account" if snapshot["accounts"] else "Connect Google account")
    else:
        google += '<p class="help">Google sign-in is not set up yet. Your server administrator needs to enable it once under Connection setup below.</p>'
    for account in snapshot["accounts"]:
        key = account["key"]
        google += f'<div class="row"><div><strong>{ui.esc(account["email"])}</strong><p class="help">Connected for this session; reconnect within an hour.</p></div>'
        google += action(visit, "google-disconnect", "Disconnect", {"account": key}) + '</div>'
        refresh = action(visit, "google-projects", "Refresh projects", {"account": key})
        if account["projects"] is None:
            google += '<p class="help">Your project list has not loaded yet.</p>' + refresh
        elif not account["projects"]:
            google += '<p class="help">No active projects were found. Choose another account or ask a project owner for access.</p>' + refresh
        else:
            form = ui.Form(submitted if submitted and submitted.get("account") == key else None)
            choices = [("", "Choose a project…")] + [(project["id"], f'{project["name"]} ({project["id"]})') for project in account["projects"]]
            google += ui.form_open(visit, "connections", "destination", {"account": key})
            picker = ui.select_row(form, bag, "project", "Google Cloud project", None,
                                   destination.project_id if destination else "", choices)
            google += picker.replace('id="project"', f'id="project-{ui.esc(key)}"').replace('for="project"', f'for="project-{ui.esc(key)}"')
            google += ui.submit_bar("Check this project", "Checks billing and deployment setup before saving.") + '</form>'
            google += details("My project is missing", '<p>If access was just added, refresh the project list.</p>' + refresh)
    google += '<p class="help">Google sign-in lets you choose a project. '
    google += 'Your server administrator must prepare that project to run apps first.</p>'
    body += panel("google", "1", "Choose your Google account", ui.esc(destination.project_id) if destination else "Not selected yet", google, current)

    github = '<p>GitHub stores code in projects called repositories. Choose which ones this app may access.</p>'
    catalog = snapshot.get("github")
    github += action(visit, "github-refresh", "Refresh list" if catalog else "Load code from GitHub")
    token_connection = catalog and catalog["kind"] == "fine_grained_token"
    if token_connection:
        github += link("https://github.com/settings/personal-access-tokens", "Manage access on GitHub")
        github += '<p class="help">Your server administrator can change access for the existing connection. Then refresh the list here.</p>'
    else:
        github += link(catalog.get("install_url") if catalog and catalog.get("install_url") else "https://github.com/settings/installations",
                       "Choose account and code on GitHub")
        github += '<p class="help">On GitHub, choose your account and “Only select repositories”. Select the code you want, then return here.</p>'
    if catalog:
        if catalog.get("app_name"):
            github += f'<p class="help">Connected GitHub App: {ui.esc(catalog["app_name"])}</p>'
        if catalog["kind"] == "fine_grained_token":
            github += '<p class="help">Using the GitHub connection already set up on this server.</p>'
        for installation in catalog["installations"]:
            github += f'<div class="row"><div><strong>{ui.esc(installation["account"])}</strong>'
            github += '<p class="help">Connected GitHub account</p></div>'
            github += link(installation["url"], "Manage access") + '</div>'
        selected = config.connections.repositories
        platform = config.platform.repository.lower() if config.platform else None
        github += ui.form_open(visit, "connections", "repositories")
        if not catalog["repositories"]:
            github += '<p>No repositories were found. Grant access using the GitHub link above, then refresh.</p>'
        for index, repo in enumerate(catalog["repositories"]):
            name = repo["name"]
            required = name.lower() == platform
            checked = required or (name in submitted.getlist("repositories") if submitted and submitted.get("form") == "repositories"
                                   else selected is None or name.lower() in selected)
            github += (f'<div class="row repository-choice"><label for="repo-{index}"><strong>{ui.esc(name)}</strong>'
                       f'<span class="help"> · {"Private" if repo["private"] else "Public"}{" · Required for this service" if required else ""}</span></label>'
                       f'<input id="repo-{index}" type="checkbox" name="repositories" value="{ui.esc(name)}"'
                       + (' checked' if checked else '') + (' disabled' if required else '') + '></div>')
        known = {repo["name"].lower() for repo in catalog["repositories"]}
        if platform and platform not in known:
            github += ui.callout("amber", "Access to the setup files is missing", f'<p>Grant GitHub access to {ui.esc(platform)}, then refresh.</p>')
        missing = set(selected or []) - known
        if missing:
            github += '<p class="help">No longer visible on GitHub: ' + ui.esc(', '.join(sorted(missing))) + '. Saving this list removes their local selection.</p>'
        github += ui.submit_bar("Review and save access", "Only selected code can be used. Work already running will not be cancelled.") + '</form>'
    elif config.connections.repositories is not None:
        github += '<p class="help">Saved selection: ' + ui.esc(', '.join(config.connections.repositories) or 'only the files needed to operate this service') + '</p>'
    else:
        github += '<p class="help">No selection saved yet. The current GitHub access is still in use.</p>'
    if not catalog:
        github += '<div class="actions">' + next_step("application-setup", "Use current access and continue") + '</div>'
    selected_count = len(config.connections.repositories or [])
    github_summary = f"{selected_count} selected" if config.connections.repositories is not None else "Choose the account and code this app can access"
    body += panel("github", "2", "Choose GitHub access", github_summary, github, current)

    body += panel("application-setup", "3", "Check and launch", "Choose an app and check it before starting", application_setup(ctx, snapshot, submitted, bag), current)

    google_form = ui.Form(submitted if submitted and submitted.get("form") == "google-setup" else None)
    setup = '<details class="extra-options"' + (' open' if form_name in ("google-setup", "github-setup") else '') + '><summary>Connection setup — for your server administrator</summary>'
    setup += '<p class="help">Set up each provider once. No passwords, private keys or Google tokens belong in these forms.</p>'
    setup += ui.form_open(visit, "connections", "google-setup")
    setup += ui.text_row(google_form, bag, "connections.google_client_id", "Google web client ID", None, config.connections.google_client_id or "")
    redirect = config.oauth.public_url + CALLBACK if config.oauth else None
    setup += '<p class="help">Create a Web application OAuth client in Google Auth Platform and register this exact redirect address:</p>'
    setup += f'<p class="mono">{ui.esc(redirect or "Set the controller’s public HTTPS address in Advanced first.")}</p>'
    setup += '<p class="help">The server administrator stores its client secret in LAB_GOOGLE_CLIENT_SECRET and restarts the server. '
    setup += 'Enable the Cloud Resource Manager API and configure the Google consent audience for the accounts that will use this page.</p>'
    setup += link('https://console.cloud.google.com/auth/clients', 'Open Google connection setup')
    setup += ui.submit_bar("Review Google setup") + '</form>'
    github_form = ui.Form(submitted if submitted and submitted.get("form") == "github-setup" else None)
    setup += ui.form_open(visit, "connections", "github-setup")
    setup += ui.text_row(github_form, bag, "github_app.app_id", "GitHub App ID", None, config.github_app.app_id if config.github_app else "")
    setup += ui.text_row(github_form, bag, "github_app.private_key_secret", "App key location in Google Secret Manager", None,
                         config.github_app.private_key_secret or "" if config.github_app else "",
                         'The secret’s resource name, not its contents. Use projects/PROJECT/secrets/FAST_DEPLOY_GITHUB_APP_KEY.')
    setup += '<p class="help">An administrator creates the GitHub App and stores its key once. Saving a different App requires a server restart. '
    setup += 'An existing key-file configuration stays unchanged unless you save this form.</p>'
    setup += link('https://github.com/settings/apps', 'Open GitHub App setup')
    setup += ui.submit_bar("Review GitHub setup") + '</form></details>'
    return '<div class="connections">' + body + setup + '</div>'


def application_setup(ctx, snapshot, submitted, bag):
    config, visit = ctx.config, ctx.visit
    review = snapshot.get("review")
    values = submitted if submitted and submitted.get("action") == "check-repository" else None
    if values is None and review:
        values = {key: review[key] or "" for key in ("repository", "application", "path")}
        values["mode"] = "as-is" if review["as_is"] else "prepared"
        if review["auto_deploy"]:
            values["auto_deploy"] = "on"
    form = ui.Form(values)
    catalog = snapshot.get("github")
    repositories = config.connections.repositories
    options = [(repo["name"], repo["name"]) for repo in catalog["repositories"]
               if repositories is None or repo["name"].lower() in repositories] if catalog else [(name, name) for name in repositories or []]
    # Keep a checked URL visible even when the catalog uses owner/name or different capitalization.
    chosen = values.get("repository") if values else None
    if options and chosen and chosen not in {value for value, _ in options}:
        options.insert(0, (chosen, chosen))
    body = '<p>Which app would you like to run? Choose its code below.</p>'
    body += (f'<form method="post" action="{ui.PREFIX}/connections/action#application-setup">'
             f'<input type="hidden" name="csrf" value="{ui.esc(visit.csrf)}"><input type="hidden" name="tab" value="connections">'
             '<input type="hidden" name="action" value="check-repository">')
    if options:
        body += ui.select_row(form, bag, "repository", "App code", None, "", options)
    else:
        body += ui.text_row(form, bag, "repository", "GitHub code link", None, "", "Paste a GitHub repository link. Private repositories need GitHub access first.")
    advanced = ui.text_row(form, bag, "application", "App name (optional)", None, "", "Optional. Leave blank to use the name from your code.")
    advanced += ui.text_row(form, bag, "path", "App folder (optional)", None, "", "Usually leave this blank. Enter a folder only if your app is inside one.")
    advanced += ui.select_row(form, bag, "mode", "Setup method", None, "as-is", [
        ("as-is", "Standard app — initial approval needed"),
        ("prepared", "App already prepared for deployment"),
    ], "Most apps use the first option. You approve a setup proposal on GitHub before deployment continues.")
    body += details("More options — usually no changes needed", advanced,
                    bool(values and (values.get("path") or values.get("application") or values.get("mode") == "prepared")))
    body += ui.switch_row(form, bag, "auto_deploy", "Publish updates automatically when the code changes", None, False,
                          "When off, you choose when to publish future updates.")
    body += ui.submit_bar("Check app", "This only checks the code. Your app will not start yet.") + '</form>'
    if review and review["ready"]:
        body = details("Change app or options", body)
    if review:
        check = review["check"]
        ready = review["ready"]
        destination = config.onboarding
        body += ui.callout("green" if ready else "amber", "Your app is ready to set up" if ready else "A few things need fixing first",
                           f'<p>{ui.esc(review["repository"])}' + (f' · {ui.esc(review["path"])}' if review["path"] else '') + '</p>')
        if review["as_is"]:
            findings = check.get("as_it_is", {})
            issues = [str(item.get("problem", item)) for item in findings.get("refusals", [])]
            if findings.get("error"):
                issues.append(str(findings["error"]))
            if findings.get("folders"):
                issues.append("Choose a service folder: " + ", ".join(findings["folders"]))
        else:
            issues = [item["item"] + (": " + str(item["detail"]) if item.get("detail") else "") for item in check.get("checklist", []) if not item["ok"]]
            if check.get("error"):
                issues.append(check["error"])
        if issues:
            body += '<p>The code is not ready for this setup method. If the app is inside a folder, select it under More options.</p>'
            body += details("See what needs fixing", '<ul dir="auto">' + ''.join(f'<li>{ui.esc(issue)}</li>' for issue in issues) + '</ul>')
        if ready:
            existing = next((app for name, app, _ in ctx.applications() if name == review["application"]), None)
            target = existing.target.project_id if existing else destination.project_id if destination else 'Choose a Google Cloud project first'
            body += '<dl class="strip"><div><dt>Application</dt><dd>' + ui.esc(review["application"])
            body += '</dd></div><div><dt>Setup</dt><dd>' + ('Initial approval on GitHub' if review['as_is'] else 'App already prepared for deployment')
            body += '</dd></div><div><dt>' + ('Google project' if existing else 'Default Google project') + '</dt><dd>' + ui.esc(target)
            body += '</dd></div><div><dt>Environment</dt><dd>Test environment</dd></div><div><dt>Future releases</dt><dd>'
            body += ('Automatic' if review['auto_deploy'] else 'When you start them') + '</dd></div></dl>'
            body += '<p class="help">Setup will use these choices. If you change them, check the app again.</p>'
            if not existing:
                body += '<p class="help">Some apps have their own saved project. If so, that project takes priority.</p>'
            if review.get("previous_state"):
                body += '<p>Setup has already started for this app. Continuing keeps its original update setting; you can change that in My apps.</p>'
            body += '<p class="help">Starting prepares your app for deployment. Running it on Google Cloud may cost money. '
            body += 'This check is valid for ten minutes. The code is checked again when setup starts.</p>'
            if review["as_is"]:
                body += '<p>Your approval is required: open the setup proposal on GitHub and use Merge to approve it.</p>'
            elif not config.onboarding or not config.onboarding.auto_merge or not visit.principal.may_merge_own_requests:
                body += '<p>Your access rules may require someone to approve the setup proposals on GitHub.</p>'
            if destination and config.live_dispatch and not config.paused:
                label = "View existing setup" if review.get("previous_state") == "complete" else "Continue setup" if review.get("previous_state") else "Start app setup"
                body += action(visit, "start-application", label, {"review": review["id"]})
            else:
                body += '<p class="help">Choose a Google project first. Your server administrator must also enable app launches in Service settings.</p>'
    status = snapshot.get("onboarding")
    if status:
        body = details("Check again or choose another app", body)
        state = status.get("state", "unknown")
        title = {"complete": "Setup finished — check the deployment", "blocked": "Setup needs your attention",
                 "merging_contract": "Waiting for your approval on GitHub"}.get(state, "Application setup in progress")
        body += ui.callout("amber" if state in ("blocked", "merging_contract") else "gray", title,
                           f'<p><bdi>{ui.esc(status["application"])}</bdi></p>')
        if status.get("error"):
            body += problem(status["error"]) + '<p>After fixing the issue, check the same repository above and start again to resume.</p>'
        for step in status.get("steps", {}).values():
            if not isinstance(step, dict):
                continue
            pr = step.get("pull_request") or step.get("pr") or step
            url = step.get("url") or (pr.get("url") if isinstance(pr, dict) else None)
            if url and urlparse(url).scheme == "https" and urlparse(url).netloc == "github.com":
                body += link(url, "Open proposal to approve")
            if step.get("waiting"):
                body += details("Why this is waiting", f'<p dir="auto">{ui.esc(step["waiting"])}</p>')
        if state == "complete":
            body += f'<p><a class="btn" href="{ui.PREFIX}?' + ui.esc(urlencode({'tab': 'deployments', 'onboarding': status['application']})) + '">View live deployment</a></p>'
        elif state == "merging_contract":
            body += '<p>Approve the setup proposal on GitHub to continue.</p>'
        elif state != "blocked":
            body += '<p class="help">Setup continues in the background. You can leave this page.</p>'
        body += f'<a class="btn" href="{ui.PREFIX}?' + ui.esc(urlencode({'tab': 'deployments', 'onboarding': status['application']})) + '">Follow live progress</a>'
    return body
