"""Explicit local configuration; live calls require separately verified pricing."""

from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

PROJECT_ROOT = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=PROJECT_ROOT / ".env", extra="ignore")

    database_url: str = "sqlite:///./data/runtime/runtime.db"
    live_api_enabled: bool = False
    openai_api_key: str = Field(default="", repr=False)
    openai_model: str = "gpt-5.6-terra"
    openai_embedding_model: str = ""
    max_agent_steps: int = Field(default=6, ge=1, le=6)
    max_candidate_repairs: int = Field(default=2, ge=0, le=2)
    model_timeout_seconds: float = Field(default=45, gt=0)
    tool_timeout_seconds: float = Field(default=5, gt=0)
    run_timeout_seconds: float = Field(default=180, gt=0)
    mvp_api_budget_sgd: float = Field(default=10, gt=0, le=10)
    api_warning_sgd: float = Field(default=8, gt=0, le=8)
    max_output_tokens: int = Field(default=4000, ge=256, le=8000)
    price_model: str = ""
    price_embedding_model: str = ""
    price_input_usd_per_million: float = Field(default=0, ge=0)
    price_output_usd_per_million: float = Field(default=0, ge=0)
    price_embedding_usd_per_million: float = Field(default=0, ge=0)
    usd_to_sgd: float = Field(default=0, ge=0)
    pricing_verified_on: str = ""
    pricing_source: str = ""
    cost_ledger_path: str = ""

    @property
    def project_root(self) -> Path:
        return PROJECT_ROOT

    @property
    def ledger_path(self) -> Path:
        return (
            Path(self.cost_ledger_path)
            if self.cost_ledger_path
            else self.project_root / "data/runtime/costs.sqlite3"
        )


def live_readiness(settings: Settings) -> tuple[bool, str]:
    from datetime import UTC, date, datetime

    if not settings.live_api_enabled:
        return False, "真实调用尚未启用；当前可使用离线脚本演示。"
    if not settings.openai_api_key:
        return False, "未配置本机 OPENAI_API_KEY。"
    if not settings.openai_model or not settings.openai_embedding_model:
        return False, "需要配置运行模型和 embedding 模型。"
    if (
        settings.price_model != settings.openai_model
        or settings.price_embedding_model != settings.openai_embedding_model
        or min(
            settings.price_input_usd_per_million,
            settings.price_output_usd_per_million,
            settings.price_embedding_usd_per_million,
            settings.usd_to_sgd,
        )
        <= 0
        or not settings.pricing_source.startswith("https://")
    ):
        return False, "需填写对应模型的已核验计价、来源与汇率，才能执行预算检查。"
    try:
        age = (
            datetime.now(UTC).date() - date.fromisoformat(settings.pricing_verified_on)
        ).days
    except ValueError:
        return False, "计价核验日期无效。"
    if not 0 <= age <= 7:
        return False, "计价核验日期需在最近七天内。"
    return True, "真实调用配置齐全；每次请求仍会检查剩余额度。"
