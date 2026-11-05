"""

Store packaged desktop secrets in the OS vault and protect source/Pi environment files.

"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path
import re
import sys

from dotenv import dotenv_values
from hooks.pideck_hook import private_write, protect_file


# --- Credential loading and persistence ---

NAMES = ("PIDECK_TOKEN", "PIDECK_HOOK_TOKEN", "SPOTIFY_CLIENT_ID", "SPOTIFY_CLIENT_SECRET",
         "SPOTIFY_REFRESH_TOKEN", "SLACK_BOT_TOKEN", "SLACK_APP_TOKEN")


class SecretStore:
    """Keep desktop secrets outside the application bundle and private on headless systems."""

    def __init__(
        self,
        config: Path,
        ) -> None:
        """
        Select the OS vault for frozen Windows/macOS applications and private files otherwise.

        Args:
            config (Path): Configuration whose installation owns these credentials.

        Returns:
            None: The installation-specific credential namespace is initialized.
        """
        self.path = config.resolve().parent / ".env"
        self.service = "piDeck:" + hashlib.sha256(str(config.resolve()).encode()).hexdigest()[:20]
        self.vault = None
        if getattr(sys, "frozen", False) and sys.platform in {"win32", "darwin"}:
            if sys.platform == "win32":
                from keyring.backends.Windows import WinVaultKeyring

                self.vault = WinVaultKeyring()
            else:
                from keyring.backends.macOS import Keyring

                self.vault = Keyring()

    def load(
        self,
        ) -> None:
        """
        Load private settings and import packaged desktop credentials into the OS vault.

        Returns:
            None: Credential environment values are populated without logging secrets.
        """
        values = {}
        if self.path.is_file():
            protect_file(self.path)
            values = dotenv_values(self.path)
        for name in NAMES:
            value = values.get(name)
            if value:
                if self.vault:
                    self.vault.set_password(self.service, name, value)
                os.environ.setdefault(name, value)
            elif self.vault and name not in os.environ:
                stored = self.vault.get_password(self.service, name)
                if stored:
                    os.environ[name] = stored
        if self.vault and any(values.get(name) for name in NAMES):
            remaining = {key: value for key, value in values.items() if key not in NAMES and value is not None}
            private_write(self.path, "# Secrets are stored in your OS credential vault.\n" +
                          "".join(f"{key}={value!r}\n" for key, value in remaining.items()))

    def set(
        self,
        name: str,
        value: str,
        ) -> None:
        """
        Persist one credential without including it in process arguments or output.

        Args:
            name (str): Supported piDeck credential environment name.
            value (str): Private credential value to retain.

        Returns:
            None: The vault or protected file and process environment are updated.
        """
        if name not in NAMES or any(char in value for char in "\n\r'"):
            raise ValueError("Invalid credential name or value")
        if self.vault:
            self.vault.set_password(self.service, name, value)
        else:
            text = self.path.read_text(encoding="utf-8") if self.path.is_file() else ""
            text = re.sub(r"(?m)^\s*(?:export\s+)?" + re.escape(name) + r"=.*(?:\n|$)", "", text)
            private_write(self.path, text.rstrip("\n") + ("\n" if text else "") + f"{name}='{value}'\n")
        os.environ[name] = value
