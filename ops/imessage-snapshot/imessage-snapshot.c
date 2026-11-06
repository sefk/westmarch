/* imessage-snapshot DEST: copy ~/Library/Messages/chat.db to DEST.
 *
 * The only program that needs Full Disk Access. The source is hardcoded
 * (home from the passwd entry, not the environment) and nothing but DEST
 * is configurable. Uses the SQLite online backup API, so the WAL is
 * included and the copy is consistent. Prints nothing on success; on
 * failure one line of error text (never database content) and a nonzero
 * exit, 77 when the source cannot be read for permission reasons.
 * Build: bin/build-imessage-snapshot
 */
#include <errno.h>
#include <fcntl.h>
#include <limits.h>
#include <pwd.h>
#include <sqlite3.h>
#include <stdio.h>
#include <string.h>
#include <sys/stat.h>
#include <unistd.h>

#define EX_NOPERM 77

static int denied(int rc) {
    int base = rc & 0xff;
    return base == SQLITE_CANTOPEN || base == SQLITE_AUTH || base == SQLITE_PERM;
}

static int fail(const char *what, int rc, const char *tmp) {
    fprintf(stderr, "imessage-snapshot: %s\n", what);
    if (tmp) unlink(tmp);
    return denied(rc) ? EX_NOPERM : 1;
}

int main(int argc, char **argv) {
    if (argc != 2) {
        fprintf(stderr, "usage: imessage-snapshot DEST\n");
        return 2;
    }
    struct passwd *pw = getpwuid(getuid());
    if (!pw || !pw->pw_dir) return fail("no home directory", 0, NULL);

    char src[PATH_MAX], tmp[PATH_MAX];
    if (snprintf(src, sizeof src, "%s/Library/Messages/chat.db", pw->pw_dir) >= (int)sizeof src ||
        snprintf(tmp, sizeof tmp, "%s.tmp", argv[1]) >= (int)sizeof tmp)
        return fail("path too long", 0, NULL);

    /* Create the temp file 0600 before SQLite opens it. */
    unlink(tmp);
    int fd = open(tmp, O_WRONLY | O_CREAT | O_EXCL, 0600);
    if (fd < 0) return fail(strerror(errno), 0, NULL);
    close(fd);

    sqlite3 *from = NULL, *to = NULL;
    int rc = sqlite3_open_v2(src, &from, SQLITE_OPEN_READONLY, NULL);
    if (rc != SQLITE_OK) return fail(sqlite3_errstr(rc), rc, tmp);
    rc = sqlite3_open_v2(tmp, &to, SQLITE_OPEN_READWRITE, NULL);
    if (rc != SQLITE_OK) return fail(sqlite3_errstr(rc), rc, tmp);

    sqlite3_backup *b = sqlite3_backup_init(to, "main", from, "main");
    if (!b) return fail(sqlite3_errmsg(to), sqlite3_errcode(to), tmp);
    int tries = 0;
    do {
        /* All pages in one step: Messages writing mid-copy would restart a
         * stepped backup, and a WAL reader doesn't block the writer. */
        rc = sqlite3_backup_step(b, -1);
        if ((rc == SQLITE_BUSY || rc == SQLITE_LOCKED) && ++tries <= 100) {
            sqlite3_sleep(100);
            rc = SQLITE_OK;
        }
    } while (rc == SQLITE_OK);
    int rc2 = sqlite3_backup_finish(b);
    if (rc == SQLITE_DONE) rc = rc2;
    if (rc != SQLITE_DONE && rc != SQLITE_OK) return fail(sqlite3_errstr(rc), rc, tmp);

    sqlite3_close(from);
    if (sqlite3_close(to) != SQLITE_OK) return fail("close failed", 0, tmp);
    if (rename(tmp, argv[1]) != 0) return fail(strerror(errno), 0, tmp);
    return 0;
}
