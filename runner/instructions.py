"""Shared instruction files, snapshotted once per runner session."""

import hashlib
import re
from dataclasses import dataclass

PROFILE_NAME = re.compile(r"[a-z0-9][a-z0-9_-]{0,63}")
MAX_INSTRUCTION_BYTES = 65536


class InstructionError(ValueError):
    pass


@dataclass(frozen=True)
class Instructions:
    profile: str
    text: str
    sha256: str

    def metadata(self, harness):
        return {
            "profile": self.profile,
            "sha256": self.sha256,
            "delivery": "system_prompt_append" if harness == "claude" else "initial_prompt",
        }


def load_instructions(name, profiles):
    if name is None:
        return None
    path = profiles.get(name)
    if path is None:
        raise InstructionError("instruction_profile_not_found")
    try:
        with path.open("rb") as source:
            content = source.read(MAX_INSTRUCTION_BYTES + 1)
        if len(content) > MAX_INSTRUCTION_BYTES:
            raise InstructionError("instruction_profile_too_large")
        text = content.decode("utf-8")
        if not text.strip() or "\x00" in text:
            raise InstructionError("instruction_profile_invalid")
    except (OSError, UnicodeError):
        raise InstructionError("instruction_profile_unreadable") from None
    return Instructions(name, text, hashlib.sha256(content).hexdigest())
