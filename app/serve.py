"""Initialize the mounted volume before dropping privileges and starting Uvicorn."""
import os
from pathlib import Path


def mounted(path):
    return any(line.split()[4] == str(path) for line in Path("/proc/self/mountinfo").read_text().splitlines())


def main():
    railway = bool(os.getenv("RAILWAY_SERVICE_ID"))
    os.umask(0o077)
    data = Path(os.getenv("SUPERTRADE_DATA", "/data")).resolve()
    if railway and (str(data) != os.getenv("RAILWAY_VOLUME_MOUNT_PATH") or not mounted(data)):
        raise SystemExit("Supertrade requires its Railway state volume to be mounted at SUPERTRADE_DATA")
    data.mkdir(parents=True, exist_ok=True)
    if os.getuid() == 0:
        os.chown(data, 10001, 10001)
        os.setgroups([])
        os.setgid(10001)
        os.setuid(10001)
    os.execvp("uvicorn", ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", os.getenv("PORT", "8080"),
                         "--no-proxy-headers", "--no-access-log"])


if __name__ == "__main__":
    main()
