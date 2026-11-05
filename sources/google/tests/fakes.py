"""A hand-written stand-in for GoogleClient. All content is made up."""

from __future__ import annotations

from sources.google.client import Gone, NotFound


def eml(n: int, subject: str | None = None, attach: bool = False) -> bytes:
    subject = subject or f"Test message {n}"
    head = (f"From: Alice Example <alice{n}@example.com>\r\n"
            f"To: Bob <bob@example.org>, carol@example.net\r\n"
            f"Cc: \"Dee, D.\" <dee@example.com>\r\n"
            f"Subject: {subject}\r\n"
            f"Date: Tue, 03 Feb 2026 10:0{n % 10}:00 -0800\r\n"
            f"Message-ID: <m{n}@example.com>\r\n"
            f"List-Id: Example List <list.example.com>\r\n"
            "MIME-Version: 1.0\r\n")
    if not attach:
        return (head + "Content-Type: text/plain\r\n\r\nhello\r\n").encode()
    return (head + 'Content-Type: multipart/mixed; boundary="B"\r\n\r\n'
            "--B\r\nContent-Type: text/plain\r\n\r\nbody\r\n"
            '--B\r\nContent-Type: application/pdf\r\nContent-Disposition: attachment; filename="a.pdf"\r\n'
            "Content-Transfer-Encoding: base64\r\n\r\nJVBERi0=\r\n"
            '--B\r\nContent-Type: image/png\r\nContent-Disposition: attachment; filename="b.png"\r\n'
            "Content-Transfer-Encoding: base64\r\n\r\niVBORw==\r\n--B--\r\n").encode()


def gid(n: int) -> str:
    return f"{n:016x}"


class Clock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t


class FakeClient:
    def __init__(self, n_messages=0, page_size=3, clock=None, tick=0.0):
        self.messages = {gid(i): self.message(i) for i in range(1, n_messages + 1)}
        self.page_size = page_size
        self.clock, self.tick = clock, tick
        self.history_id = "1000"
        self.history: list[dict] | int = []
        self.fetched: list[str] = []
        self.list_calls: list = []
        self.calendar_list: list[dict] = []
        self.events: dict = {}  # calendar id -> callable(sync_token) -> (items, next) or exception
        self.event_calls: list = []
        self.lists: list[dict] = []
        self.task_items: dict = {}
        self.task_calls: list = []

    @staticmethod
    def message(n, labels=("INBOX",), raw=None):
        return {"id": gid(n), "threadId": gid(n), "historyId": str(900 + n), "internalDate": "1770141600000",
                "labelIds": list(labels), "sizeEstimate": 1234, "snippet": f"snippet {n}",
                "raw": raw if raw is not None else eml(n)}

    # Gmail
    def gmail_profile(self):
        return {"emailAddress": "me@example.com", "messagesTotal": len(self.messages),
                "historyId": self.history_id}

    def gmail_labels(self):
        return [{"id": "INBOX", "name": "INBOX", "type": "system"},
                {"id": "Label_1", "name": "Receipts", "type": "user"}]

    def gmail_list(self, token, include_spam_trash=False):
        self.list_calls.append((token, include_spam_trash))
        ids = sorted(self.messages, reverse=True)
        start = int(token or 0)
        end = start + self.page_size
        return ids[start:end], (str(end) if end < len(ids) else None)

    def gmail_get_raw(self, ids):
        assert len(ids) <= 50
        if self.clock:
            self.clock.t += self.tick
        self.fetched.extend(ids)
        return {i: (dict(self.messages[i]) if i in self.messages else None) for i in ids}

    def gmail_history(self, start):
        if self.history == 404:
            raise NotFound(404, "notFound", "history id too old")
        return self.history, self.history_id

    # Calendar
    def calendars(self):
        return self.calendar_list

    def calendar_events(self, cal_id, sync_token):
        self.event_calls.append((cal_id, sync_token))
        result = self.events[cal_id](sync_token)
        if result == 410:
            raise Gone(410, "fullSyncRequired", "sync token expired")
        return result

    # Tasks
    def tasklists(self):
        return self.lists

    def tasks_in(self, list_id, updated_min):
        self.task_calls.append((list_id, updated_min))
        return self.task_items.get(list_id, [])
