"""Read-only configuration snapshots and one reversible writer for connection changes."""
from dataclasses import dataclass
from pathlib import Path
import tomllib
from typing import Callable


CONCURRENT_CHANGE = "Codex 配置在扫描期间发生变化，请重新检测"


@dataclass(frozen=True, repr=False)
class ConfigSnapshot:
    path: Path
    content: bytes | None

    @classmethod
    def read(cls, path):
        path = Path(path)
        try:
            content = path.read_bytes()
        except FileNotFoundError:
            content = None
        return cls(path, content)

    @property
    def text(self):
        return self.content.decode("utf-8-sig") if self.content is not None else ""

    def check(self):
        if ConfigSnapshot.read(self.path).content != self.content:
            raise ValueError(CONCURRENT_CHANGE)


@dataclass(frozen=True, repr=False)
class ConfigEdit:
    source: ConfigSnapshot
    updated: str

    def check_applied(self):
        # The writer uses UTF-8 without BOM and preserves the supplied newlines.
        if ConfigSnapshot.read(self.source.path).content != self.updated.encode("utf8"):
            raise ValueError(CONCURRENT_CHANGE)


@dataclass(frozen=True, repr=False)
class ConfigurationBatch:
    sources: tuple[ConfigSnapshot, ...]
    edits: tuple[ConfigEdit, ...]
    guards: tuple[Callable[[], None], ...] = ()

    def check(self):
        for source in self.sources:
            source.check()
        for edit in self.edits:
            tomllib.loads(edit.updated)
        for guard in self.guards:
            guard()

    def apply(self, link):
        """Preflight the entire batch, write, then verify every input again.

        Retain recovery information if a concurrent writer changes an applied
        file. Rollback never overwrites that writer's changes.
        """
        from .config_link import ConfigLink

        self.check()
        written, backups = [], []
        try:
            for edit in self.edits:
                edit.source.check()
                written.append(edit)
                backups.append(ConfigLink(edit.source.path, link.state_path)._write(edit.updated))
            changed = {edit.source.path: edit for edit in self.edits}
            for source in self.sources:
                if source.path in changed:
                    changed[source.path].check_applied()
                else:
                    source.check()
            for guard in self.guards:
                guard()
        except Exception:
            for edit in reversed(written):
                try:
                    edit.check_applied()
                except (OSError, ValueError):
                    continue
                path = edit.source.path
                if edit.source.content is None:
                    path.unlink()
                else:
                    staging = path.with_name(path.name + ".inspector-rollback")
                    staging.write_bytes(edit.source.content)
                    staging.replace(path)
            raise
        return backups
