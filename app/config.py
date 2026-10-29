import os
import sys
from dataclasses import dataclass
from pathlib import Path


def _bool(name: str, default: bool = False) -> bool:
    v = os.environ.get(name)
    if v is None or v == "":
        return default
    return v.strip().lower() in ("1", "true", "yes", "on")


@dataclass(frozen=True)
class Config:
    username: str
    password: str
    base_url: str
    host: str
    port: int
    data_dir: Path
    dry_run: bool
    log_level: str
    timeout_ms: int

    @property
    def state_file(self) -> Path:
        return self.data_dir / "session.json"

    @property
    def history_file(self) -> Path:
        return self.data_dir / "history.json"


def load() -> Config:
    username = os.environ.get("MIR3_USERNAME", "")
    password = os.environ.get("MIR3_PASSWORD", "")
    base_url = os.environ.get("MIR3_BASE_URL", "")
    missing = [
        n
        for n, v in (("MIR3_USERNAME", username), ("MIR3_PASSWORD", password), ("MIR3_BASE_URL", base_url))
        if not v
    ]
    if missing:
        sys.exit(f"Missing required env: {', '.join(missing)}")

    base_url = base_url.strip()
    if "://" not in base_url:
        base_url = f"https://{base_url}"

    data_dir = Path(os.environ.get("DATA_DIR", "/data"))
    data_dir.mkdir(parents=True, exist_ok=True)

    return Config(
        username=username,
        password=password,
        base_url=base_url.rstrip("/"),
        host=os.environ.get("API_HOST", "0.0.0.0"),
        port=int(os.environ.get("API_PORT", "8080")),
        data_dir=data_dir,
        dry_run=_bool("DRY_RUN"),
        log_level=os.environ.get("LOG_LEVEL", "INFO").upper(),
        timeout_ms=int(os.environ.get("MIR3_TIMEOUT_MS", "30000")),
    )
