"""Configuration manager.

Reads and parses the YAML config file so the application can be started from config.
Pydantic models provide type-safe access to the settings.
"""

import os
import re
import traceback
from pathlib import Path
from typing import Any

import yaml

from .model import (
    AppConfig,
    AuthConfig,
    ConfigModel,
    LoggingConfig,
    ModuleConfig,
    ModulesConfig,
    ServerConfig,
)

# Allowed config file directories (allowlist)
ALLOWED_CONFIG_DIRS: list[str] = [
    "config",
    ".",
]

# Env-var placeholders: ${VAR} or ${VAR:-default}
_ENV_VAR_PATTERN = re.compile(r"\$\{([^}:]+)(?::-([^}]*))?\}")


def _expand_env_vars(value: Any) -> Any:
    """Recursively expand env-var placeholders in config values.

    Supports ``${VAR}`` (required) and ``${VAR:-default}`` (optional, falls back to
    default when unset); dicts and lists are processed recursively.

    Args:
        value: Config value to expand (str / dict / list / anything else).

    Returns:
        The expanded value; non-string types are returned unchanged.

    Raises:
        ValueError: A placeholder has no default and its env var is not set.
    """
    if isinstance(value, str):
        def _replace(match: "re.Match[str]") -> str:
            var_name, default = match.group(1), match.group(2)
            env_value = os.environ.get(var_name)
            if env_value is not None:
                return env_value
            if default is not None:
                return default
            raise ValueError(
                f"Environment variable '{var_name}' is not set and has no default. "
                f"Set it, or use ${{{var_name}:-default}} in the config"
            )
        return _ENV_VAR_PATTERN.sub(_replace, value)
    if isinstance(value, dict):
        return {k: _expand_env_vars(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_expand_env_vars(item) for item in value]
    return value


def _load_dotenv_if_available() -> None:
    """Load .env from the working directory if python-dotenv is installed (optional)."""
    try:
        from dotenv import load_dotenv  # type: ignore
    except ImportError:
        return
    env_path = Path(os.getcwd()) / ".env"
    if env_path.exists():
        load_dotenv(env_path)


class Config:
    _config: dict[str, Any] = {}
    _config_model: ConfigModel | None = None
    _module_configs: dict[str, ModuleConfig] = {}  # dict storage avoids setattr injection
    
    @classmethod
    def _validate_config_path(cls, config_path: str) -> Path:
        """Validate the config path to prevent path traversal.

        Args:
            config_path: User-supplied config file path.

        Returns:
            The validated Path object.

        Raises:
            ValueError: The path is unsafe or outside the allowed directories.
        """
        # Base directory (current working directory)
        base_dir = os.path.realpath(os.getcwd())
        
        # Resolve the config path to an absolute, normalized path
        if not os.path.isabs(config_path):
            full_path = os.path.realpath(os.path.join(base_dir, config_path))
        else:
            full_path = os.path.realpath(config_path)
        
        # Make sure the normalized path stays inside the base directory
        if not full_path.startswith(base_dir + os.sep) and full_path != base_dir:
            raise ValueError(
                f"Security Error: config_path '{config_path}' resolves to '{full_path}' "
                f"which is outside the allowed base directory '{base_dir}'"
            )
        
        # Path relative to the base directory
        rel_path = os.path.relpath(full_path, base_dir)
        
        # Check it lives in an allowed directory
        path_parts = Path(rel_path).parts
        if path_parts:
            first_dir = path_parts[0]
            # Allow files at the root or inside an allowlisted directory
            is_allowed = (
                first_dir in ALLOWED_CONFIG_DIRS or
                len(path_parts) == 1 or  # file directly under the root
                "." in ALLOWED_CONFIG_DIRS  # "." allows every sub-path
            )
            if not is_allowed:
                raise ValueError(
                    f"Security Error: config file must be in allowed directories: {ALLOWED_CONFIG_DIRS}"
                )
        
        # Only .yaml / .yml extensions are accepted
        config_file = Path(full_path)
        if config_file.suffix.lower() not in ['.yaml', '.yml']:
            raise ValueError(
                f"Security Error: config file must have .yaml or .yml extension, got '{config_file.suffix}'"
            )
        
        return config_file
    
    @classmethod
    def set_config(cls, config_path: str):
        """Load the configuration:
        1. Read the YAML config file from the given path
        2. Parse and validate it with Pydantic
        3. Store the per-module config of every enabled module in a plain dict
        """
        cls._load_config(config_path)
        try:
            config_data = dict(cls._config)
            modules_data = config_data.get('modules', {}) or {}
            enabled_modules = modules_data.get('enabled', [])

            # Reduce modules to what Pydantic validates
            config_data['modules'] = {'enabled': enabled_modules}

            cls._config_model = ConfigModel(**config_data)

            # Module configs live in a plain dict instead of setattr: dict access
            # cannot touch the object's internals, so it is injection-safe
            cls._module_configs: dict[str, ModuleConfig] = {}
            
            for module_name in enabled_modules:
                if module_name in modules_data and module_name != 'enabled':
                    try:
                        module_config = ModuleConfig(**modules_data[module_name])
                        # Dict storage instead of setattr blocks the unsafe data flow
                        cls._module_configs[module_name] = module_config
                    except Exception:
                        traceback.print_exc()
        except Exception:
            traceback.print_exc()
            cls._config_model = None
    
    @classmethod
    def _load_config(cls, config_path: str):
        """Load the config file.

        Args:
            config_path: Config file path.

        Raises:
            ValueError: The path is unsafe.
            FileNotFoundError: The config file does not exist.
        """
        # Validate the path first to prevent path traversal
        config_file_path = cls._validate_config_path(config_path)

        _load_dotenv_if_available()

        try:
            if config_file_path.exists():
                with open(config_file_path, 'r', encoding='utf-8') as file:
                    raw_config = yaml.safe_load(file) or {}
                cls._config = _expand_env_vars(raw_config)
            else:
                raise FileNotFoundError(f"Config file not found: {config_path}")
                
        except ValueError:
            # Re-raise security validation errors
            raise
        except Exception as e:
            print(f"Failed to load config file: {e}")
            traceback.print_exc()
            cls._config = {}
    
    @classmethod
    def get_config(cls) -> dict[str, Any]:
        """Return the full config dict."""
        return cls._config


    @classmethod
    def get_config_model(cls) -> ConfigModel | None:
        """Return the parsed Pydantic ConfigModel, if any."""
        return cls._config_model

    # --- Pydantic model getters ---
    @classmethod
    def get_server_config(cls) -> ServerConfig | None:
        if cls._config_model is None:
            return None
        return cls._config_model.server

    @classmethod
    def get_auth_config(cls) -> AuthConfig | None:
        if cls._config_model is None:
            return None
        return cls._config_model.auth

    @classmethod
    def get_logging_config(cls) -> LoggingConfig | None:
        if cls._config_model is None:
            return None
        return cls._config_model.logging

    @classmethod
    def get_app_config_model(cls) -> AppConfig | None:
        if cls._config_model is None:
            return None
        return cls._config_model.app

    @classmethod
    def get_modules_config(cls) -> ModulesConfig | None:
        if cls._config_model is None:
            return None
        return cls._config_model.modules

    @classmethod
    def get_module_model(cls, module_name: str) -> ModuleConfig | None:
        """Return the ModuleConfig of a single module from the dict, if any.

        Dict access instead of getattr/setattr prevents attribute injection.
        """
        if cls._config_model is None:
            return None
        # Safe dict lookup
        return cls._module_configs.get(module_name, None)


def get_config(config_path: str) -> ConfigModel | None:
    """Convenience: load the config and return the parsed ConfigModel on success."""
    Config.set_config(config_path)
    return Config.get_config_model()