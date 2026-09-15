/*
 * Dikte.app launcher.
 *
 * macOS grants privacy permissions (Microphone, Accessibility, Input
 * Monitoring) to the "responsible" process. This small signed executable
 * stays alive as the parent and spawns the Python app as its child, so the
 * permissions belong to Dikte.app, not to Python or the terminal. Editing the
 * Python code never resets them; only rebuilding this launcher does.
 *
 * Because the child inherits those permissions, the launcher is strict about
 * what it runs: launcher.conf must belong to the user and not be writable by
 * others, the child gets a minimal environment (no PYTHON* / DYLD_*), and
 * Python runs in isolated mode (-I).
 *
 * ~/Library/Application Support/Dikte/launcher.conf:
 *     python=/absolute/path/.venv/bin/python
 *     workdir=/absolute/path/to/project
 * A child exit code of 75 means "restart me". Giving up exits 0 so launchd's
 * KeepAlive does not respawn the launcher in a loop.
 */
#include <errno.h>
#include <limits.h>
#include <mach-o/dyld.h>
#include <pwd.h>
#include <signal.h>
#include <spawn.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
#include <sys/wait.h>
#include <time.h>
#include <unistd.h>

#define RESTART_CODE 75
#define MAX_RESTARTS 5
#define RESTART_WINDOW_S 60
#define MAX_ENV 16

static volatile sig_atomic_t g_child = 0;
static volatile sig_atomic_t g_stopping = 0;

static void forward_signal(int sig) {
    g_stopping = 1;
    if (g_child > 0) {
        kill((pid_t)g_child, sig);
    }
}

/* A GUI app has no visible stderr, so show a dialog as well. Fixed strings only. */
static void alert(const char *message) {
    char script[512];
    snprintf(script, sizeof script, "display alert \"Dikte\" message \"%s\"", message);
    char *argv[] = {"/usr/bin/osascript", "-e", script, NULL};
    char *minimal_env[] = {"PATH=/usr/bin:/bin:/usr/sbin:/sbin", NULL};
    pid_t pid;
    if (posix_spawn(&pid, argv[0], NULL, NULL, argv, minimal_env) == 0) {
        waitpid(pid, NULL, 0);
    }
    fprintf(stderr, "Dikte: %s\n", message);
}

/* Returns 0 on success, -1 if missing/incomplete, -2 if the file is not trustworthy. */
static int read_conf(const char *home, char *python, char *workdir, size_t size) {
    char path[PATH_MAX];
    snprintf(path, sizeof path, "%s/Library/Application Support/Dikte/launcher.conf", home);
    FILE *file = fopen(path, "r");
    if (file == NULL) {
        return -1;
    }
    struct stat info;
    if (fstat(fileno(file), &info) != 0 || info.st_uid != getuid() || (info.st_mode & (S_IWGRP | S_IWOTH)) != 0) {
        fclose(file);
        return -2;
    }
    char line[PATH_MAX + 16];
    python[0] = workdir[0] = '\0';
    while (fgets(line, sizeof line, file) != NULL) {
        line[strcspn(line, "\r\n")] = '\0';
        if (strncmp(line, "python=", 7) == 0) {
            strlcpy(python, line + 7, size);
        } else if (strncmp(line, "workdir=", 8) == 0) {
            strlcpy(workdir, line + 8, size);
        }
    }
    fclose(file);
    return (python[0] == '/' && workdir[0] == '/') ? 0 : -1;
}

/* .../Dikte.app/Contents/MacOS/Dikte -> .../Dikte.app */
static void bundle_path(char *out, size_t size) {
    char exe[PATH_MAX];
    char real[PATH_MAX];
    uint32_t length = sizeof exe;
    out[0] = '\0';
    if (_NSGetExecutablePath(exe, &length) != 0 || realpath(exe, real) == NULL) {
        return;
    }
    for (int i = 0; i < 3; i++) {
        char *slash = strrchr(real, '/');
        if (slash == NULL) {
            return;
        }
        *slash = '\0';
    }
    strlcpy(out, real, size);
}

static int add_env(char **env, int *count, const char *name, const char *value) {
    if (value == NULL || *count >= MAX_ENV - 1) {
        return 0;
    }
    size_t length = strlen(name) + strlen(value) + 2;
    char *entry = malloc(length);
    if (entry == NULL) {
        return -1;
    }
    snprintf(entry, length, "%s=%s", name, value);
    env[(*count)++] = entry;
    env[*count] = NULL;
    return 0;
}

/* Minimal environment for the child: nothing that could redirect Python or dyld. */
static int build_env(char **env, const char *home, const char *app) {
    static const char *passthrough[] = {
        "USER", "LOGNAME", "TMPDIR", "LANG", "LC_ALL", "LC_CTYPE", "__CF_USER_TEXT_ENCODING", "DIKTE_NO_PROMPTS", NULL,
    };
    int count = 0;
    env[0] = NULL;
    int failed = add_env(env, &count, "HOME", home);
    failed |= add_env(env, &count, "PATH", "/usr/bin:/bin:/usr/sbin:/sbin:/opt/homebrew/bin:/usr/local/bin");
    failed |= add_env(env, &count, "DIKTE_LAUNCHER", "1");
    if (app[0] != '\0') {
        failed |= add_env(env, &count, "DIKTE_APP_PATH", app);
    }
    for (int i = 0; passthrough[i] != NULL; i++) {
        failed |= add_env(env, &count, passthrough[i], getenv(passthrough[i]));
    }
    return failed;
}

/* Returns 1 if restarting now would exceed MAX_RESTARTS within RESTART_WINDOW_S. */
static int too_many_restarts(void) {
    static time_t history[MAX_RESTARTS];
    static int next = 0;
    time_t now = time(NULL);
    time_t oldest = history[next];
    history[next] = now;
    next = (next + 1) % MAX_RESTARTS;
    return oldest != 0 && now - oldest < RESTART_WINDOW_S;
}

int main(void) {
    char home[PATH_MAX];
    char python[PATH_MAX];
    char workdir[PATH_MAX];
    char app[PATH_MAX];
    char *child_env[MAX_ENV];

    struct passwd *user = getpwuid(getuid());
    if (user == NULL || user->pw_dir == NULL) {
        alert("Could not determine your home folder.");
        return 0;
    }
    strlcpy(home, user->pw_dir, sizeof home);

    int conf = read_conf(home, python, workdir, sizeof python);
    if (conf == -2) {
        alert("launcher.conf must belong to you and must not be writable by others. Run scripts/install.sh again.");
        return 0;
    }
    if (conf != 0) {
        alert("Setup is incomplete. Run scripts/install.sh in the Dikte project folder.");
        return 0;
    }
    if (chdir(workdir) != 0) {
        alert("The Dikte project folder was moved or deleted. Run scripts/install.sh again.");
        return 0;
    }
    bundle_path(app, sizeof app);
    if (build_env(child_env, home, app) != 0) {
        alert("Out of memory while starting.");
        return 0;
    }

    struct sigaction action;
    memset(&action, 0, sizeof action);
    action.sa_handler = forward_signal;
    sigemptyset(&action.sa_mask);
    sigaction(SIGTERM, &action, NULL);
    sigaction(SIGINT, &action, NULL);
    sigaction(SIGHUP, &action, NULL);

    sigset_t forwarded;
    sigset_t previous;
    sigset_t empty;
    sigemptyset(&forwarded);
    sigaddset(&forwarded, SIGTERM);
    sigaddset(&forwarded, SIGINT);
    sigaddset(&forwarded, SIGHUP);
    sigemptyset(&empty);

    posix_spawnattr_t attributes;
    posix_spawnattr_init(&attributes);
    posix_spawnattr_setsigmask(&attributes, &empty);
    posix_spawnattr_setflags(&attributes, POSIX_SPAWN_SETSIGMASK);

    /* -I: ignore PYTHON* variables and the user site dir; -u: unbuffered output. */
    char *argv[] = {python, "-I", "-u", "-m", "dikte", NULL};
    for (;;) {
        pid_t pid = 0;
        /* Block signals while spawning so none is lost before g_child is set. */
        sigprocmask(SIG_BLOCK, &forwarded, &previous);
        if (g_stopping) {
            sigprocmask(SIG_SETMASK, &previous, NULL);
            return 0;
        }
        int error = posix_spawn(&pid, python, NULL, &attributes, argv, child_env);
        if (error == 0) {
            g_child = pid;
        }
        sigprocmask(SIG_SETMASK, &previous, NULL);
        if (error != 0) {
            alert("Could not start Python. Run scripts/install.sh again.");
            return 0;
        }

        int status = 0;
        while (waitpid(pid, &status, 0) < 0) {
            if (errno != EINTR) {
                return 1;
            }
        }
        g_child = 0;

        if (WIFEXITED(status) && WEXITSTATUS(status) == RESTART_CODE && !g_stopping) {
            if (too_many_restarts()) {
                alert("Dikte restarted too many times in a minute and was stopped. See ~/Library/Logs/Dikte.");
                return 0;
            }
            continue;
        }
        if (WIFEXITED(status)) {
            return WEXITSTATUS(status);
        }
        return WIFSIGNALED(status) ? 128 + WTERMSIG(status) : 1;
    }
}
