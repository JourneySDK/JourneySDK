"""Error types for journey v1."""

from __future__ import annotations


class JourneyError(Exception):
    """Base error for all journey failures."""

    def __init__(self, message: str, *, hint: str | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.hint = hint


class JourneySelectionError(JourneyError):
    """Base error for discovery and CLI journey selection failures."""


class JourneyDiscoveryError(JourneySelectionError):
    """Raised when journey discovery cannot inspect a Python file."""

    def __init__(self, file_path: str, message: str) -> None:
        super().__init__(
            f"Could not load the journey file '{file_path}'. {message}",
            hint=(
                "Check the file for syntax errors, missing imports, or other code "
                "that fails at import time."
            ),
        )
        self.file_path = file_path
        self.message = message


class NoJourneysFoundError(JourneySelectionError):
    """Raised when discovery does not find any matching decorated journeys."""

    def __init__(
        self,
        *,
        root: str,
        file_path: str | None = None,
        journey_name: str | None = None,
    ) -> None:
        if file_path is not None and journey_name is not None:
            message = (
                f"No journey named '{journey_name}' was found in '{file_path}'."
            )
        elif file_path is not None:
            message = f"No journeys were found in '{file_path}'."
        elif journey_name is not None:
            message = (
                f"No journey named '{journey_name}' was found under '{root}'."
            )
        else:
            message = f"No journeys were found under '{root}'."

        if file_path is not None and journey_name is not None:
            hint = (
                "Check that the file exists, the function is decorated with @journey, "
                "and the name matches exactly."
            )
        elif file_path is not None:
            hint = "Check that the file contains at least one function decorated with @journey."
        elif journey_name is not None:
            hint = (
                "Pass `--file` to narrow the search, or check that the journey "
                "function name matches exactly."
            )
        else:
            hint = (
                "Run this command from the directory that contains your journey file, "
                "or pass `--file` to point at one explicitly."
            )

        super().__init__(message, hint=hint)
        self.root = root
        self.file_path = file_path
        self.journey_name = journey_name


class AmbiguousJourneySelectionError(JourneySelectionError):
    """Raised when a journey name matches multiple decorated functions."""

    def __init__(self, journey_name: str, matches: list[str]) -> None:
        joined = ", ".join(matches)
        super().__init__(
            f"Journey name '{journey_name}' matches more than one file: {joined}.",
            hint="Pass `--file` to choose the exact file you want to run.",
        )
        self.journey_name = journey_name
        self.matches = matches


class CompilationError(JourneyError):
    """Base error for compile-time failures."""


class InvalidBranchUsageError(CompilationError):
    """Raised when branch selectors are used outside the supported pattern."""

    def __init__(
        self,
        message: str = "This journey uses branch selection in an unsupported way.",
        *,
        hint: str | None = None,
    ) -> None:
        super().__init__(message, hint=hint)


class UnsupportedControlFlowError(CompilationError):
    """Raised when unsupported control flow is detected."""

    def __init__(
        self,
        message: str = "This journey uses control flow that journey v1 does not support.",
        *,
        hint: str | None = None,
    ) -> None:
        super().__init__(message, hint=hint)


class UnsupportedLoopError(UnsupportedControlFlowError):
    """Raised when a loop form is unsupported."""

    def __init__(
        self,
        message: str = "This journey uses a loop form that journey v1 does not support.",
        *,
        hint: str | None = None,
    ) -> None:
        super().__init__(message, hint=hint)


class ExecutionError(JourneyError):
    """Base error for execution-time failures."""


class StepNotFoundError(ExecutionError):
    """Raised when a requested step label is not present in any case."""

    def __init__(self, step: str) -> None:
        super().__init__(
            f"Step label '{step}' was not found in the selected journey.",
            hint=(
                "Check that the target step label exists, or pass `--file` / "
                "`--journey` to narrow the selection."
            ),
        )
        self.step = step


class AmbiguousStepSelectionError(ExecutionError):
    """Raised when a step label appears in multiple case plans."""

    def __init__(self, step: str, matches: list[str]) -> None:
        joined = ", ".join(matches)
        super().__init__(
            f"Step label '{step}' matches more than one journey flow: {joined}.",
            hint="Pass `--file` or `--journey` to narrow the selection to one journey.",
        )
        self.step = step
        self.matches = matches


class CallableExecutionError(ExecutionError):
    """Raised when a step callable throws."""

    def __init__(
        self,
        message: str = "A journey step failed while it was running.",
        *,
        hint: str | None = None,
    ) -> None:
        super().__init__(message, hint=hint)


class ExecutionStateError(ExecutionError):
    """Base error for persisted execution state failures."""

    def __init__(
        self,
        message: str = "The journey state file could not be used.",
        *,
        hint: str | None = None,
    ) -> None:
        super().__init__(message, hint=hint)


class CorruptExecutionStateError(ExecutionStateError):
    """Raised when a persisted execution state file cannot be loaded."""

    def __init__(
        self,
        message: str = "The journey state file could not be read.",
        *,
        hint: str | None = None,
    ) -> None:
        super().__init__(
            message,
            hint=hint
            or "Delete the state file or rerun without `--state` if you want to start fresh.",
        )


class ExecutionStateMismatchError(ExecutionStateError):
    """Raised when a persisted execution state does not match the current plan."""

    def __init__(
        self,
        message: str = "The journey state file no longer matches this journey run.",
        *,
        hint: str | None = None,
    ) -> None:
        super().__init__(
            message,
            hint=hint
            or "Delete the state file or rerun without `--state` after changing the journey, step, or selection.",
        )


class ExecutionStateSerializationError(ExecutionStateError):
    """Raised when execution state cannot be serialized."""

    def __init__(
        self,
        message: str = "Journey progress could not be saved to the state file.",
        *,
        hint: str | None = None,
    ) -> None:
        super().__init__(
            message,
            hint=hint
            or "Use only pickle-serializable step outputs when `--state` is enabled.",
        )
