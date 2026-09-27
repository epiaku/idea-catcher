import os
import re
from collections.abc import Mapping
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict

BackendName = Literal["freellmapi", "claude-code", "fake"]


class Profile(BaseModel):
    backend: BackendName
    model: str | None = None
    when: Literal["now", "evening"] = "now"


class ProfilesConfig(BaseModel):
    model_config = ConfigDict(extra="allow")

    default: str
    review_profile: str = "claude-sub-evening"
    profiles: dict[str, Profile]


class UnknownProfile(ValueError):
    pass


_VAR = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}")


def expand_env(text: str, env: Mapping[str, str]) -> str:
    return _VAR.sub(lambda m: env.get(m.group(1)) or (m.group(2) or ""), text)


def load_profiles(path: Path, env: Mapping[str, str] | None = None) -> ProfilesConfig:
    raw = expand_env(path.read_text(encoding="utf-8"), os.environ if env is None else env)
    cfg = ProfilesConfig.model_validate(yaml.safe_load(raw))
    for name in (cfg.default, cfg.review_profile):
        if name not in cfg.profiles:
            raise UnknownProfile(f"profile {name!r} is not defined in {path}")
    return cfg


def resolve_profile(
    cfg: ProfilesConfig, *, requested: str | None, class_default: str | None
) -> tuple[str, Profile]:
    name = requested or class_default or cfg.default
    if name not in cfg.profiles:
        raise UnknownProfile(f"unknown LLM profile {name!r}; known: {', '.join(sorted(cfg.profiles))}")
    return name, cfg.profiles[name]
