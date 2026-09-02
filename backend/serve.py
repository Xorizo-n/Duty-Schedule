import os
import pathlib
import subprocess

from duty_scheduler import create_app, start_background_workers


BACKEND_DIR = pathlib.Path(__file__).resolve().parent


def run_waitress() -> None:
    from waitress import serve

    app = create_app()
    start_background_workers(app)

    host = os.getenv("HOST", "0.0.0.0")
    port = int(os.getenv("PORT", "5000"))
    threads = int(os.getenv("WAITRESS_THREADS", "4"))

    serve(app, host=host, port=port, threads=threads)


def run_gunicorn() -> None:
    config_path = os.getenv("GUNICORN_CONFIG", "gunicorn.conf.py")
    # cwd=BACKEND_DIR: gunicorn кладет рабочий каталог в sys.path и находит wsgi.
    subprocess.run(
        ["gunicorn", "-c", config_path, "wsgi:app"],
        cwd=BACKEND_DIR,
        check=True,
    )


def main() -> None:
    if os.name == "nt":
        run_waitress()
        return

    run_gunicorn()


if __name__ == "__main__":
    main()
