"""Modal dialogs: instruction entry, stop confirmation and help."""

from __future__ import annotations

from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widgets import Input, Static

HELP_TEXT = """\
GCAE keys
  q            quit (a running agent is stopped safely first)
  p / r        pause / resume the run
  s            stop the run (asks for confirmation)
  i            inject a user instruction · also re-opens the task prompt
  d            candidate diff: file list + rendered diff
  l            detailed event log (f cycles filters)
  m            memory inspector (facts, decisions, failures)
  c            context inspector (what the model receives)
  e            latest evaluation and verification evidence
  t            full plan with goals and validation requirements
  Enter        open the detail view for the focused panel
  Tab / j / k  move focus between panels
  ?            this help · Esc closes
"""


class DialogScreen(ModalScreen[object]):
    """Centered dialog shell with a consistent frame."""

    BINDINGS = [Binding("escape", "dismiss_dialog", "Cancel", show=False)]

    def __init__(self, title: str, hint: str = "") -> None:
        super().__init__()
        self.dialog_title = title
        self.dialog_hint = hint
        self.body = Text("")

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog") as dialog:
            dialog.border_title = self.dialog_title
            yield Static("", id="dialog-body")
            yield from self.compose_body()

    def compose_body(self) -> ComposeResult:  # pragma: no cover - overridden
        return
        yield

    def action_dismiss_dialog(self) -> None:
        self.dismiss(None)


class InstructionModal(DialogScreen):
    """Single-line instruction entry; Enter submits, Esc cancels."""

    BINDINGS = [
        Binding("escape", "dismiss_dialog", "Cancel", show=False),
        Binding("enter", "submit", "Submit"),
    ]

    def __init__(
        self,
        title: str = "Add instruction",
        hint: str = "Enter submits · Esc cancels",
        placeholder: str = "e.g. do not change the public parser interface",
    ) -> None:
        super().__init__(title, hint)
        self.placeholder = placeholder

    def compose_body(self) -> ComposeResult:
        yield Static(Text(self.dialog_hint), id="dialog-hint")
        yield Input(placeholder=self.placeholder, id="dialog-input")
        yield Static("", id="dialog-ack")

    def on_mount(self) -> None:
        self.query_one("#dialog-input", Input).focus()

    def action_submit(self) -> None:
        self.dismiss(self.query_one("#dialog-input", Input).value)

    def on_input_submitted(self, event: Input.Submitted) -> None:
        self.dismiss(event.value)


class RequestModal(InstructionModal):
    """First-run prompt for the task; the planner derives the success criteria."""

    def __init__(self) -> None:
        super().__init__(
            title="New run · task",
            hint="Describe the task, then press Enter. Success criteria are derived automatically.",
            placeholder="e.g. add a --dry-run flag to the importer",
        )


class ProcessInputModal(InstructionModal):
    """Answer a live process. The value is sent to its stdin and never written to the log."""

    def __init__(self, command: str, prompt: str, sensitive: bool = False) -> None:
        label = "sensitive input · not recorded" if sensitive else "sent to the process stdin"
        super().__init__(
            title="Process input required",
            hint=f"{label} · Enter sends · Esc cancels",
            placeholder=prompt or "type the answer the process is waiting for",
        )
        self.command = command
        self.prompt = prompt

    def compose_body(self) -> ComposeResult:
        if self.command:
            yield Static(Text(f"  {self.command}", style="dim"), id="dialog-body-text")
        if self.prompt:
            yield Static(Text(f"  {self.prompt}", style="bold"), id="dialog-body-text-prompt")
        yield Static(Text(self.dialog_hint), id="dialog-hint")
        yield Input(placeholder=self.placeholder, id="dialog-input", password=False)
        yield Static("", id="dialog-ack")


class ConfirmStopModal(DialogScreen):
    """Stopping is consequential: ask before discarding an active candidate."""

    BINDINGS = [
        Binding("escape", "dismiss_dialog", "Cancel", show=False),
        Binding("n", "dismiss_dialog", "Cancel", show=False),
        Binding("y", "confirm", "Stop"),
        Binding("enter", "confirm", "Stop"),
    ]

    def __init__(self) -> None:
        super().__init__("Stop run", "Enter stops · Esc cancels")

    def compose_body(self) -> ComposeResult:
        self.body = Text(
            "Stop this run?\n\n"
            "The accepted checkpoint is preserved.\n"
            "Speculative candidate changes are discarded; state is persisted."
        )
        yield Static(self.body, id="dialog-body-text")

    def action_confirm(self) -> None:
        self.dismiss(True)


class HelpModal(DialogScreen):
    BINDINGS = [
        Binding("escape", "dismiss_dialog", "Close", show=False),
        Binding("q", "dismiss_dialog", "Close", show=False),
        Binding("question_mark", "dismiss_dialog", "Close", show=False),
    ]

    def __init__(self) -> None:
        super().__init__("Help", "Esc closes")

    def compose_body(self) -> ComposeResult:
        self.body = Text(HELP_TEXT)
        yield Static(self.body, id="dialog-body-text")
