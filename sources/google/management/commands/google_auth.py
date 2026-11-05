"""One-time Google sign-in, then a check that Gmail, Calendar and Tasks answer.

Prints the account, message count, and per-API status. Never prints the
token or the client secret.
"""

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from sources.google.auth import GoogleAuthError, load_credentials, token_age
from sources.google.client import ApiDisabled, GoogleApiError, GoogleClient

CLIENT_HELP = """\
No OAuth client file at {path}.

To make one:
  1. https://console.cloud.google.com/ : pick or create a project.
  2. APIs & Services > Library: enable the Gmail API, Google Calendar API
     and Google Tasks API.
  3. APIs & Services > OAuth consent screen: user type External (or
     Internal on Workspace); add yourself as a test user.
  4. APIs & Services > Credentials > Create credentials > OAuth client ID,
     application type "Desktop app".
  5. Download the JSON and save it as {path}
     (or point GOOGLE_CLIENT_FILE at it), then run: manage.py google_auth
"""


class Command(BaseCommand):
    help = "Google sign-in (read-only Gmail, Calendar, Tasks); stores the token in secrets/."

    def add_arguments(self, parser):
        parser.add_argument("--check", action="store_true",
                            help="don't open a browser; just check the stored token and the APIs")
        parser.add_argument("--force", action="store_true",
                            help="run the consent flow even if a usable token exists")

    def handle(self, *args, check=False, force=False, **opts):
        if not check and not settings.GOOGLE_CLIENT_FILE.exists():
            raise CommandError(CLIENT_HELP.format(path=settings.GOOGLE_CLIENT_FILE))
        try:
            creds = load_credentials(interactive=not check, force=force and not check)
        except GoogleAuthError as exc:
            raise CommandError(str(exc)) from None
        self.stdout.write(f"Token: {settings.GOOGLE_TOKEN_FILE} ({token_age()})")

        client = GoogleClient(creds)
        failures = 0

        def probe(label, fn):
            nonlocal failures
            try:
                result = fn()
            except ApiDisabled as exc:
                failures += 1
                self.stdout.write(f"{label}: API not enabled. Enable it at:\n  {exc.url}")
                return None
            except GoogleApiError as exc:
                failures += 1
                self.stdout.write(f"{label}: error {exc}")
                return None
            return result

        profile = probe("Gmail", client.gmail_profile)
        if profile:
            self.stdout.write(f"Gmail: authorized as {profile.get('emailAddress')} "
                              f"({profile.get('messagesTotal', '?')} messages)")
        cals = probe("Calendar", client.calendars)
        if cals is not None:
            self.stdout.write(f"Calendar: ok, {len(cals)} calendars")
        lists = probe("Tasks", client.tasklists)
        if lists is not None:
            self.stdout.write(f"Tasks: ok, {len(lists)} task lists")
        if failures:
            raise CommandError(f"{failures} API check(s) failed")
