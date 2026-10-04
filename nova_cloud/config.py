"""nova_cloud.config — environment-driven configuration.

Every secret comes from the environment. Nothing sensitive is committed, and
the app refuses to start in production without real secrets rather than
silently falling back to a default that would be identical on every install.
"""
from __future__ import annotations

import os
import secrets
import sys
from dataclasses import dataclass, field
from pathlib import Path

# Load .env before anything reads the environment.
#
# A shell variable lives only in the window that set it, and on Windows the
# quoting is a trap: PowerShell interpolates $ inside double quotes, so a
# password containing $74 is silently mangled. A gitignored .env file avoids
# both problems and survives reopening the terminal.
# Never under pytest. A test run that inherits production credentials would
# point the suite at the live database and the real mail provider, and the
# first sign of it would be test data in production.
if "PYTEST_CURRENT_TEST" not in os.environ and "pytest" not in sys.modules:
    try:
        from dotenv import load_dotenv as _load_dotenv
        for _candidate in (Path.cwd() / ".env",
                           Path(__file__).resolve().parents[1] / ".env"):
            if _candidate.exists():
                _load_dotenv(_candidate, override=False)
                break
    except ImportError:
        pass


def _bool(name: str, default: bool) -> bool:
    v = os.getenv(name)
    if v is None:
        return default
    return v.strip().lower() in ("1", "true", "yes", "on")


def _int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, "").strip() or default)
    except ValueError:
        return default


@dataclass
class Config:
    env: str = field(default_factory=lambda: os.getenv("NOVA_ENV", "development"))

    # Storage. SQLite by default so a developer can run the whole platform with
    # no infrastructure; point DATABASE_URL at Postgres for deployment.
    database_url: str = field(default_factory=lambda: os.getenv("DATABASE_URL", ""))

    # Signing keys.
    secret_key: str = field(default_factory=lambda: os.getenv("NOVA_SECRET_KEY", ""))
    admin_secret_key: str = field(default_factory=lambda: os.getenv("NOVA_ADMIN_SECRET_KEY", ""))

    # Token lifetimes.
    access_ttl_s: int = field(default_factory=lambda: _int("NOVA_ACCESS_TTL", 900))          # 15 min
    refresh_ttl_s: int = field(default_factory=lambda: _int("NOVA_REFRESH_TTL", 60 * 86400))  # 60 d
    admin_session_ttl_s: int = field(default_factory=lambda: _int("NOVA_ADMIN_TTL", 1800))    # 30 min

    # How long a device may keep operating locally with no contact with the
    # backend before it must re-authenticate. See docs: offline behaviour.
    offline_grace_s: int = field(default_factory=lambda: _int("NOVA_OFFLINE_GRACE", 30 * 86400))

    # Retention, in days, per category. 0 means "keep indefinitely".
    retention_auth_events_d: int = field(default_factory=lambda: _int("NOVA_RET_AUTH", 180))
    retention_telemetry_d: int = field(default_factory=lambda: _int("NOVA_RET_TELEMETRY", 90))
    retention_errors_d: int = field(default_factory=lambda: _int("NOVA_RET_ERRORS", 90))
    retention_admin_audit_d: int = field(default_factory=lambda: _int("NOVA_RET_AUDIT", 730))

    # Required by default in production: an account is only fully active once
    # its owner has proved the address. Development keeps sign-up one step.
    require_email_verification: bool = field(
        default_factory=lambda: _bool(
            "NOVA_REQUIRE_EMAIL_VERIFICATION",
            os.getenv("NOVA_ENV", "development") in ("production", "prod")))

    # How many reverse proxies sit in front of the app and append to
    # X-Forwarded-For (Caddy on the EC2 = 1). 0 means the header is ignored
    # entirely: trusting its first entry let any client pick its own IP and
    # walk past every rate limit.
    trusted_proxy_hops: int = field(default_factory=lambda: _int("NOVA_TRUSTED_PROXY_HOPS", 0))

    # -- model access ------------------------------------------------------
    # The one Gemini key. It lives only here, on the server; the desktop gets
    # either a short-lived Live token or has its text requests forwarded.
    gemini_api_key: str = field(default_factory=lambda: os.getenv("NOVA_GEMINI_API_KEY", ""))
    # Models the gateway will forward to. Anything else is refused, so a
    # client cannot spend the key on a model the plan does not include.
    allowed_models: str = field(default_factory=lambda: os.getenv(
        "NOVA_ALLOWED_MODELS",
        "gemini-2.5-flash,gemini-2.5-flash-lite,gemini-flash-latest,"
        "gemini-flash-lite-latest,text-embedding-004,gemini-embedding-001"))
    # The Live model an ephemeral token is locked to.
    live_model: str = field(default_factory=lambda: os.getenv(
        "NOVA_LIVE_MODEL", "gemini-3.1-flash-live-preview"))
    live_token_minutes: int = field(default_factory=lambda: _int("NOVA_LIVE_TOKEN_MINUTES", 30))
    # Daily limits for the default plan. Other plans: NOVA_PLAN_<NAME>_LIVE /
    # NOVA_PLAN_<NAME>_GENERATE.
    free_live_per_day: int = field(default_factory=lambda: _int("NOVA_PLAN_FREE_LIVE", 60))
    free_generate_per_day: int = field(default_factory=lambda: _int("NOVA_PLAN_FREE_GENERATE", 400))

    # Public half of the owner's release-signing key (base64, 32 bytes). Used
    # to refuse publishing a release whose signature does not verify. The
    # private half is never on the server.
    update_public_key: str = field(default_factory=lambda: os.getenv("NOVA_UPDATE_PUBLIC_KEY", ""))

    def plan_limits(self, plan: str) -> dict:
        p = (plan or "free").upper()
        if p == "FREE":
            live, gen = self.free_live_per_day, self.free_generate_per_day
        else:
            live = _int(f"NOVA_PLAN_{p}_LIVE", self.free_live_per_day)
            gen = _int(f"NOVA_PLAN_{p}_GENERATE", self.free_generate_per_day)
        # Embeddings are cheap and come in bursts (indexing a document), so
        # they get a larger allowance -- but still a limit: 0 would mean none.
        return {"live_token": live, "generate": gen,
                "embed": _int(f"NOVA_PLAN_{p}_EMBED", gen * 10)}

    def model_allowed(self, model: str) -> bool:
        m = (model or "").strip().removeprefix("models/")
        return m in {x.strip() for x in self.allowed_models.split(",") if x.strip()}
    admin_require_mfa: bool = field(default_factory=lambda: _bool("NOVA_ADMIN_REQUIRE_MFA", True))

    def __post_init__(self) -> None:
        if not self.database_url:
            self.database_url = "sqlite:///" + str(self.default_db_path())
        prod = self.env in ("production", "prod", "staging")
        for name, attr in (("NOVA_SECRET_KEY", "secret_key"),
                           ("NOVA_ADMIN_SECRET_KEY", "admin_secret_key")):
            if not getattr(self, attr):
                if prod:
                    raise RuntimeError(
                        f"{name} must be set in {self.env}. Refusing to start with a "
                        "generated key: sessions would be invalidated on every restart "
                        "and would differ between workers."
                    )
                # Development: ephemeral key, kept in a file so a restart does
                # not log the developer out on every reload.
                setattr(self, attr, self._dev_key(name))
        if self.secret_key == self.admin_secret_key:
            raise RuntimeError("user and admin signing keys must differ: "
                               "a user token must never validate as an admin token")

    @staticmethod
    def default_db_path() -> Path:
        d = Path(os.getenv("NOVA_CLOUD_DATA", "")) if os.getenv("NOVA_CLOUD_DATA") else \
            Path.home() / ".nova" / "cloud"
        d.mkdir(parents=True, exist_ok=True)
        return d / "nova_cloud.db"

    @staticmethod
    def _dev_key(name: str) -> str:
        d = Path.home() / ".nova" / "cloud"
        d.mkdir(parents=True, exist_ok=True)
        p = d / f"dev_{name.lower()}.key"
        if p.exists():
            return p.read_text(encoding="utf-8").strip()
        k = secrets.token_urlsafe(48)
        p.write_text(k, encoding="utf-8")
        try:
            os.chmod(p, 0o600)
        except Exception:
            pass
        return k

    @property
    def is_production(self) -> bool:
        return self.env in ("production", "prod")


_cfg: Config | None = None


def config() -> Config:
    global _cfg
    if _cfg is None:
        _cfg = Config()
    return _cfg


def reset_config() -> None:
    """Test hook: re-read the environment."""
    global _cfg
    _cfg = None
