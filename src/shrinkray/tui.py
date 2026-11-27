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
import sqlite3
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, ClassVar

from textual.app import App, ComposeResult
from textual.binding import BindingType
from textual.containers import Horizontal, VerticalScroll
from textual.screen import Screen
from textual.widgets import Button, Footer, Header, Input, Label, Static, Switch

from shrinkray.config import Conf
from shrinkray.engine.pause import (
    operator_paused,
    pause_reason,
    set_operator_paused,
    write_cancel_request,
)


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


@dataclass(frozen=True)
class PendingJob:
    """One queued or running job the terminal UI lists.

    Attributes:
        id (int): The job's database id.
        kind (str): Its kind ("scan", "plan", "encode", ...).
        state (str): "queued" or "running".
        target (str): The first path the payload names, for the list line.
    """

    id: int
    kind: str
    state: str
    target: str


def read_pending_jobs(db_file: Path | None) -> list[PendingJob]:
    """Reads the queued and running jobs from the database, read-only.

    The connection is opened with ``mode=ro``: the terminal UI never creates,
    migrates or writes the database (the server owns it). A missing or
    undecodable database means no job list. Per-file stage rows (a payload
    with ``"pipeline"``) are hidden: the folder job that owns them is the
    one the operator cancels.

    Args:
        db_file (Path | None): The resident app's database file.

    Returns:
        list[PendingJob]: The queued and running jobs, oldest first.
    """
    if db_file is None:
        return []
    try:
        if not db_file.is_file():
            return []
        connection = sqlite3.connect(f"{db_file.resolve().as_uri()}?mode=ro", uri=True)
    except (OSError, sqlite3.Error):
        return []
    try:
        connection.row_factory = sqlite3.Row
        rows = connection.execute(
            "SELECT id, kind, payload, state FROM jobs WHERE state IN (?, ?) ORDER BY id",
            ("queued", "running"),
        ).fetchall()
    except sqlite3.Error:
        return []
    finally:
        connection.close()
    jobs: list[PendingJob] = []
    for row in rows:
        try:
            payload = json.loads(row["payload"])
        except (TypeError, ValueError):
            continue
        if not isinstance(payload, dict) or payload.get("pipeline"):
            continue
        jobs.append(
            PendingJob(
                id=int(row["id"]),
                kind=str(row["kind"]),
                state=str(row["state"]),
                target=_payload_target(payload),
            )
        )
    return jobs


def _payload_target(payload: dict[str, Any]) -> str:
    """Returns the first target a job payload names, for the list line."""
    for key in ("path", "paths"):
        value = payload.get(key)
        if isinstance(value, list) and value:
            return str(value[0])
        if isinstance(value, str) and value:
            return value
    return ""


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
            "quality.min_saving_percent",
            "Minimum saving %",
            "int",
            defaults.min_saving_percent,
            "Skip a file whose predicted saving is below this (0 = any shrink)",
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
            "handbrake.audio_languages",
            "Audio languages kept",
            "list",
            list(defaults.handbrake_audio_languages),
            "empty = keep every track; ISO 639-2 codes (issue #330/#297)",
        ),
        Field(
            "Quality",
            "handbrake.subtitle_languages",
            "Subtitle languages kept",
            "list",
            list(defaults.handbrake_subtitle_languages),
            "empty = keep every track; ISO 639-2 codes",
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
            "Quality",
            "quality.sample_seconds",
            "Probe window length (s)",
            "float",
            defaults.sample_seconds,
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
            "beyond h264, hevc, vp9 (av1 is rejected: the output format)",
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


class JobsScreen(Screen[None]):
    """The read-only job list and the cancel action (issue #621)."""

    BINDINGS: ClassVar[list[BindingType]] = [
        ("escape", "app.pop_screen", "Back"),
        ("r", "refresh", "Refresh"),
    ]
    CSS = """
    #jobs-list { height: 1fr; padding: 0 1; }
    .job-row { height: auto; }
    .job-label { width: 1fr; padding-top: 1; }
    #jobs-message { height: auto; }
    #jobs-buttons { height: auto; margin-top: 1; }
    """

    def __init__(self, db_file: Path, state_dir: Path) -> None:
        """Opens the list on one database and one data folder.

        Args:
            db_file (Path): The resident app's database file (read-only).
            state_dir (Path): The data folder the cancel request is written to.
        """
        super().__init__()
        self.db_file = db_file
        self.state_dir = state_dir

    def compose(self) -> ComposeResult:
        yield Header()
        with VerticalScroll(id="jobs-list"):
            yield Static("", id="jobs-message", markup=False)
        with Horizontal(id="jobs-buttons"):
            yield Button("Refresh", id="jobs-refresh")
            yield Button("Back", id="jobs-back")
        yield Footer()

    def on_mount(self) -> None:
        self._refresh()

    def _refresh(self) -> None:
        """Re-reads the database and rebuilds the rows (read-only)."""
        container = self.query_one("#jobs-list", VerticalScroll)
        for child in list(container.children):
            if child.id != "jobs-message":
                child.remove()
        jobs = read_pending_jobs(self.db_file)
        message = self.query_one("#jobs-message", Static)
        if not jobs:
            message.update(f"No queued or running jobs in {self.db_file}.")
            return
        message.update(
            f"{len(jobs)} queued or running job(s). Cancel writes a request; the server applies it."
        )
        for job in jobs:
            container.mount(
                Horizontal(
                    Static(
                        f"#{job.id}  {job.kind}  {job.state}  {job.target}",
                        classes="job-label",
                        markup=False,
                    ),
                    Button("Cancel", id=f"cancel-{job.id}", classes="job-cancel"),
                    classes="job-row",
                )
            )

    def action_refresh(self) -> None:
        """Re-reads the job list."""
        self._refresh()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "jobs-refresh":
            self._refresh()
        elif event.button.id == "jobs-back":
            self.app.pop_screen()
        elif event.button.id is not None and event.button.id.startswith("cancel-"):
            self._cancel(int(event.button.id.removeprefix("cancel-")), event.button)

    def _cancel(self, job_id: int, button: Button) -> None:
        """Writes the cancel request; the row stays until the job ends."""
        try:
            write_cancel_request(self.state_dir, job_id)
        except (OSError, ValueError) as e:
            self.query_one("#jobs-message", Static).update(f"Not requested: {e}")
            return
        button.disabled = True
        button.label = "Requested"
        self.query_one("#jobs-message", Static).update(
            f"Cancel requested for job #{job_id}; the server applies it "
            "and the row stays until the job ends."
        )


class SetupApp(App[None]):
    """The `shrinkray setup` screen: settings, the pause switch, jobs, save."""

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

    def __init__(
        self,
        config_path: Path,
        state_dir: Path | None,
        db_file: Path | None = None,
    ) -> None:
        """Opens the editor on one config file.

        Args:
            config_path (Path): The config.json the resident app reads.
            state_dir (Path | None): The data folder holding the pause flag and
                the cancel requests, or None when there is no resident app.
            db_file (Path | None): The resident app's database; None falls
                back to state_dir/metadata.db (the container's default).
        """
        super().__init__()
        self.config_path = config_path
        self.state_dir = state_dir
        self.db_file = (
            db_file
            if db_file is not None
            else (state_dir / "metadata.db" if state_dir is not None else None)
        )
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
                    yield Button("Jobs", id="jobs")
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
        elif event.button.id == "jobs":
            self.action_open_jobs()

    def action_open_jobs(self) -> None:
        """Opens the read-only job list (no-op without a data folder)."""
        if self.state_dir is None or self.db_file is None:
            return
        self.push_screen(JobsScreen(self.db_file, self.state_dir))

    def action_toggle_pause(self) -> None:
        """Pauses or resumes the resident app (no-op without a data folder)."""
        if self.state_dir is None:
            return
        set_operator_paused(self.state_dir, not operator_paused(self.state_dir))
        self._show_pause()
