import os
import pathlib
import pwd
import subprocess
import sys


APP_USER = "appuser"
BACKEND_DIR = pathlib.Path(__file__).resolve().parent
DEFAULT_LOG_DIR = "/app/logs"


def ensure_log_dir(log_dir: str) -> None:
    path = pathlib.Path(log_dir)
    path.mkdir(parents=True, exist_ok=True)

    user_info = pwd.getpwnam(APP_USER)
    uid = user_info.pw_uid
    gid = user_info.pw_gid

    os.chown(path, uid, gid)
    for child in path.iterdir():
        try:
            os.chown(child, uid, gid)
        except FileNotFoundError:
            continue


def drop_privileges() -> None:
    user_info = pwd.getpwnam(APP_USER)
    os.setgid(user_info.pw_gid)
    os.setuid(user_info.pw_uid)


def main() -> None:
    log_dir = os.getenv("LOG_DIR", DEFAULT_LOG_DIR)

    try:
        ensure_log_dir(log_dir)
        drop_privileges()
    except Exception as exc:
        print(f"Failed to prepare log directory '{log_dir}': {exc}", file=sys.stderr)
        sys.exit(1)

    # cwd=BACKEND_DIR: gunicorn кладет рабочий каталог в sys.path и находит wsgi.
    subprocess.run(
        ["gunicorn", "-c", "gunicorn.conf.py", "wsgi:app"],
        cwd=BACKEND_DIR,
        check=True,
    )


if __name__ == "__main__":
    main()
