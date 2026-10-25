"""`shrinkray setup`: the terminal UI that configures and controls (issue #252).

The dashboard only observes; this is where an operator changes what the
resident app does (PRODUCT.md, "The terminal is where the operator configures
and controls"). It edits config.json and the pause flag in the data folder,
the state the resident app already reads, and never calls a network route:
control needs a shell on the host or in the container.

Rules it keeps:

- It edits settings, it never loosens a product rule: every value goes through
  the same schema as a config file loaded from disk (the 1080p floor is
  raise-only, AV1 is the only output, ...), so an invalid value is shown and
  nothing is written.
- It names what a change costs before saving: a setting in the stored plan
  fingerprint re-measures the library.
- A saved change applies at the next file (issue #247); `idle_priority` and
  `auto_backlog` at the next start.
- Keys it does not show stay in the file untouched.
"""

import json
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, ClassVar

from textual.app import App, ComposeResult
from textual.binding import BindingType
from textual.containers import Horizontal, VerticalScroll
from textual.widgets import Button, Footer, Header, Input, Label, Static, Switch

from shrinkray.config import Conf
from shrinkray.engine.pause import operator_paused, pause_reason, set_operator_paused


@dataclass(frozen=True)
class Field:
    """One setting the terminal UI edits.

    Attributes:
        group (str): The heading it is listed under.
        key (str): Its dotted path in config.json ("quality.target_vmaf").
        label (str): What the operator reads.
        kind (str): "int", "float", "text", "list" (comma-separated) or "bool".
        default (Any): The value when config.json does not set it.
        note (str): When the change takes effect, or what it means.
        remeasures (bool): Part of the stored plan fingerprint: a change
            makes every cached plan stale.
    """

    group: str
    key: str
    label: str
    kind: str
    default: Any
    note: str = ""
    remeasures: bool = False

    @property
    def widget_id(self) -> str:
        """A Textual id for the field's input (ids allow no dots)."""
        return "f-" + self.key.replace(".", "-")


def fields_for(defaults: Conf) -> list[Field]:
    """Lists the settings the terminal UI edits, with the code defaults.

    Args:
        defaults (Conf): The config a missing key resolves to.

    Returns:
        list[Field]: The fields, in display order.
    """
    return [
        Field(
            "Quality",
            "quality.target_vmaf",
            "Required VMAF",
            "float",
            defaults.target_vmaf,
            remeasures=True,
        ),
        Field(
            "Quality",
            "handbrake.encoder_preset",
            "Final encode preset",
            "text",
            defaults.handbrake_encoder_preset,
            "SVT-AV1 speed, lower = slower and smaller",
        ),
        Field(
            "Quality",
            "handbrake.probe_encoder_preset",
            "Probe preset",
            "text",
            defaults.handbrake_probe_encoder_preset,
            remeasures=True,
        ),
        Field(
            "Quality",
            "quality.sample_every",
            "Probe window every (s)",
            "float",
            defaults.sample_every,
            remeasures=True,
        ),
        Field(
            "Resources",
            "handbrake.max_threads",
            "Thread cap per encode",
            "int",
            defaults.handbrake_max_threads,
            "0 = all cores",
        ),
        Field(
            "Resources",
            "handbrake.stall_minutes",
            "Stall watchdog (min)",
            "float",
            defaults.handbrake_stall_minutes,
            "0 = off",
        ),
        Field(
            "Resources",
            "resources.idle_priority",
            "Below-normal priority",
            "bool",
            defaults.idle_priority,
            "applies at the next start",
        ),
        Field(
            "Eligibility",
            "eligibility.min_source_height",
            "Min height (px)",
            "int",
            defaults.min_source_height,
            "raise only: 1080 is the floor",
        ),
        Field(
            "Eligibility",
            "eligibility.min_source_width",
            "Min width (px)",
            "int",
            defaults.min_source_width,
            "raise only: 1920 is the floor",
        ),
        Field(
            "Eligibility",
            "eligibility.min_source_size_gb",
            "Min size (GB)",
            "float",
            defaults.min_source_size_gb,
            "0 = no floor",
        ),
        Field(
            "Eligibility",
            "eligibility.candidate_codecs",
            "Extra codecs",
            "list",
            list(defaults.candidate_codecs),
            "beyond h264, hevc, vp9, av1",
        ),
        Field("Library", "file.extensions", "File extensions", "list", list(defaults.extensions)),
        Field(
            "Library",
            "app.auto_backlog",
            "Work the backlog on start",
            "bool",
            defaults.auto_backlog,
            "applies at the next start",
        ),
    ]


def read_value(data: dict[str, Any], key: str, default: Any) -> Any:
    """Reads a dotted key from the raw config, or the default when unset."""
    node: Any = data
    for part in key.split("."):
        if not isinstance(node, dict) or part not in node:
            return default
        node = node[part]
    return node


def write_value(data: dict[str, Any], key: str, value: Any) -> None:
    """Sets a dotted key in the raw config, creating its sections."""
    *sections, last = key.split(".")
    node = data
    for part in sections:
        node = node.setdefault(part, {})
    node[last] = value


def to_text(field: Field, value: Any) -> str:
    """Renders a value for an input box."""
    if field.kind == "list":
        return ", ".join(str(v) for v in value)
    return str(value)


def parse(field: Field, text: str) -> Any:
    """Turns an input box's text into the config value.

    Raises:
        ValueError: When the text is not a value of the field's kind.
    """
    text = text.strip()
    if field.kind == "int":
        return int(text)
    if field.kind == "float":
        return float(text)
    if field.kind == "list":
        return [part.strip() for part in text.split(",") if part.strip()]
    return text


def save_config(path: Path, data: dict[str, Any]) -> None:
    """Writes config.json atomically: a crash leaves the old or the new file.

    The temp file sits beside the target, so the final rename stays on one
    filesystem; it takes the old file's permissions when there is one.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(dir=path.parent, prefix=".config-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(data, handle, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        if path.exists():
            os.chmod(temp_name, path.stat().st_mode & 0o777)
        else:
            os.chmod(temp_name, 0o644)
        os.replace(temp_name, path)
    except BaseException:
        Path(temp_name).unlink(missing_ok=True)
        raise


class SetupApp(App[None]):
    """The `shrinkray setup` screen: settings, the pause switch, save."""

    TITLE = "ShrinkRay setup"
    CSS = """
    VerticalScroll { padding: 0 1; }
    .group { text-style: bold; margin-top: 1; }
    .row { height: auto; }
    .row Label { width: 26; padding-top: 1; }
    .row Input { width: 24; }
    .row .note { width: 1fr; padding-top: 1; color: $text-muted; }
    #pause-row { height: auto; margin-bottom: 1; }
    #pause-status { width: 1fr; padding-top: 1; }
    #warning { color: $warning; height: auto; }
    #message { height: auto; }
    #buttons { height: auto; margin-top: 1; }
    """
    BINDINGS: ClassVar[list[BindingType]] = [("ctrl+s", "save", "Save"), ("q", "quit", "Quit")]

    def __init__(self, config_path: Path, state_dir: Path | None) -> None:
        """Opens the editor on one config file.

        Args:
            config_path (Path): The config.json the resident app reads.
            state_dir (Path | None): The data folder holding the pause flag,
                or None when there is no resident app to pause.
        """
        super().__init__()
        self.config_path = config_path
        self.state_dir = state_dir
        self.data: dict[str, Any] = (
            json.loads(config_path.read_text(encoding="utf-8")) if config_path.is_file() else {}
        )
        self.fields = fields_for(Conf({}))

    def compose(self) -> ComposeResult:
        yield Header()
        with VerticalScroll():
            yield Static(f"Editing {self.config_path}", id="path", markup=False)
            if self.state_dir is not None:
                with Horizontal(id="pause-row"):
                    yield Static("", id="pause-status", markup=False)
                    yield Button("Pause", id="pause")
            group = ""
            for field in self.fields:
                if field.group != group:
                    group = field.group
                    yield Static(group, classes="group")
                value = read_value(self.data, field.key, field.default)
                with Horizontal(classes="row"):
                    yield Label(field.label)
                    if field.kind == "bool":
                        yield Switch(value=bool(value), id=field.widget_id)
                    else:
                        yield Input(value=to_text(field, value), id=field.widget_id)
                    yield Static(field.note, classes="note")
            yield Static("", id="warning", markup=False)
            yield Static("", id="message", markup=False)
            with Horizontal(id="buttons"):
                yield Button("Save", id="save", variant="primary")
                yield Button("Discard", id="discard")
                yield Button("Quit", id="quit")
        yield Footer()

    def on_mount(self) -> None:
        self._show_pause()

    def _show_pause(self) -> None:
        if self.state_dir is None:
            return
        reason = pause_reason(self.state_dir)
        status = self.query_one("#pause-status", Static)
        button = self.query_one("#pause", Button)
        if operator_paused(self.state_dir):
            status.update("Paused: no new file starts (the running one finishes).")
            button.label = "Resume"
        else:
            status.update("Running." if reason is None else f"Waiting: {reason}.")
            button.label = "Pause"

    def edited(self) -> dict[str, Any]:
        """Returns the raw config with every field's current value applied.

        Raises:
            ValueError: When a field's text is not a value of its kind.
        """
        data: dict[str, Any] = json.loads(json.dumps(self.data))
        for field in self.fields:
            if field.kind == "bool":
                value: Any = self.query_one(f"#{field.widget_id}", Switch).value
            else:
                text = self.query_one(f"#{field.widget_id}", Input).value
                try:
                    value = parse(field, text)
                except ValueError:
                    raise ValueError(f"{field.label}: '{text}' is not a {field.kind}") from None
            if value != read_value(self.data, field.key, field.default):
                write_value(data, field.key, value)
        return data

    def _remeasured(self, data: dict[str, Any]) -> list[str]:
        return [
            f.label
            for f in self.fields
            if f.remeasures
            and read_value(data, f.key, f.default) != read_value(self.data, f.key, f.default)
        ]

    def _refresh_warning(self) -> None:
        try:
            changed = self._remeasured(self.edited())
        except ValueError:
            changed = []
        self.query_one("#warning", Static).update(
            f"Saving re-measures the whole library: {', '.join(changed)} is part of every "
            "stored plan."
            if changed
            else ""
        )

    def on_input_changed(self, event: Input.Changed) -> None:
        self._refresh_warning()

    def action_save(self) -> None:
        message = self.query_one("#message", Static)
        try:
            data = self.edited()
            # the same schema as a file loaded from disk: a value the app
            # would refuse is never written
            Conf(data)
        except ValueError as e:
            message.update(f"Not saved: {e}")
            return
        save_config(self.config_path, data)
        self.data = data
        self._refresh_warning()
        message.update(
            "Saved. The resident app applies it at the next file "
            "(priority and backlog-on-start at the next start)."
        )

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "save":
            self.action_save()
        elif event.button.id == "discard":
            for field in self.fields:
                value = read_value(self.data, field.key, field.default)
                if field.kind == "bool":
                    self.query_one(f"#{field.widget_id}", Switch).value = bool(value)
                else:
                    self.query_one(f"#{field.widget_id}", Input).value = to_text(field, value)
            self.query_one("#message", Static).update("Changes discarded.")
        elif event.button.id == "quit":
            self.exit()
        elif event.button.id == "pause":
            self.action_toggle_pause()

    def action_toggle_pause(self) -> None:
        """Pauses or resumes the resident app (no-op without a data folder)."""
        if self.state_dir is None:
            return
        set_operator_paused(self.state_dir, not operator_paused(self.state_dir))
        self._show_pause()
