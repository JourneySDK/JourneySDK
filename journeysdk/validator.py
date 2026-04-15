"""AST guardrails for supported journey authoring patterns."""

from __future__ import annotations

import ast
import dis
import inspect
import textwrap
from dataclasses import dataclass
from functools import lru_cache
from types import CodeType, FrameType
from typing import Any

from .errors import (
    InvalidBranchUsageError,
    JourneyError,
    UnsupportedControlFlowError,
    UnsupportedLoopError,
)


@dataclass
class _ValidationIssue:
    exc_type: type[Exception]
    message: str
    hint: str | None = None


BranchSiteKey = tuple[int, int]
BranchTemplateKey = tuple[int, int]


@dataclass(frozen=True)
class BranchConditionSpec:
    template_key: BranchTemplateKey
    branch_key: str
    condition_index: int
    total_conditions: int


@dataclass(frozen=True)
class JourneyValidation:
    branch_conditions: dict[BranchSiteKey, BranchConditionSpec]


@lru_cache(maxsize=None)
def _instruction_positions(code: CodeType) -> dict[int, BranchSiteKey]:
    positions: dict[int, BranchSiteKey] = {}
    for instruction in dis.get_instructions(code):
        position = instruction.positions
        if position.lineno is None or position.col_offset is None:
            continue
        positions[instruction.offset] = (position.lineno, position.col_offset)
    return positions


def resolve_branch_call_site(frame: FrameType) -> BranchSiteKey:
    try:
        return _instruction_positions(frame.f_code)[frame.f_lasti]
    except KeyError as exc:
        raise InvalidBranchUsageError(
            "journey.branch(...) could not determine where it was called from.",
            hint="Use journey.branch(...) directly as the whole condition in an if/elif chain.",
        ) from exc


class _JourneyValidator(ast.NodeVisitor):
    def __init__(
        self,
        function_node: ast.FunctionDef | ast.AsyncFunctionDef,
        *,
        source_line_offset: int,
        source_col_offset: int,
    ) -> None:
        self.function_node = function_node
        self.allowed_branch_call_ids: set[int] = set()
        self.issues: list[_ValidationIssue] = []
        self.parents: dict[int, ast.AST] = {}
        self.branch_conditions: dict[BranchSiteKey, BranchConditionSpec] = {}
        self._source_line_offset = source_line_offset
        self._source_col_offset = source_col_offset

        for parent in ast.walk(function_node):
            for child in ast.iter_child_nodes(parent):
                self.parents[id(child)] = parent

    def validate(self) -> None:
        for stmt in self.function_node.body:
            self.visit(stmt)

    def _add_issue(
        self,
        exc_type: type[Exception],
        message: str,
        *,
        hint: str | None = None,
    ) -> None:
        self.issues.append(
            _ValidationIssue(exc_type=exc_type, message=message, hint=hint)
        )

    def _is_elif_node(self, node: ast.If) -> bool:
        parent = self.parents.get(id(node))
        return isinstance(parent, ast.If) and bool(parent.orelse) and parent.orelse[0] is node

    def _absolute_site(self, node: ast.AST) -> BranchSiteKey:
        lineno = getattr(node, "lineno", None)
        col_offset = getattr(node, "col_offset", None)
        if lineno is None or col_offset is None:
            raise InvalidBranchUsageError(
                "Journey validation could not resolve a branch location in the source.",
                hint="Keep journey.branch(...) calls in a regular Python file so the source can be inspected.",
            )
        return (
            self._source_line_offset + lineno,
            self._source_col_offset + col_offset,
        )

    def _is_branch_call(self, call: ast.Call) -> bool:
        func = call.func
        if isinstance(func, ast.Name):
            return func.id == "branch"
        if isinstance(func, ast.Attribute):
            return func.attr == "branch"
        return False

    def _is_branch_selector_call(self, call: ast.Call) -> bool:
        func = call.func
        is_checkpoint = False
        if isinstance(func, ast.Name):
            is_checkpoint = func.id == "checkpoint"
        elif isinstance(func, ast.Attribute):
            is_checkpoint = func.attr == "checkpoint"

        if not is_checkpoint:
            return False

        return any(keyword.arg == "branches" for keyword in call.keywords)

    def _find_branch_calls(self, node: ast.AST) -> list[ast.Call]:
        found: list[ast.Call] = []
        for subnode in ast.walk(node):
            if not isinstance(subnode, ast.Call):
                continue
            if self._is_branch_call(subnode):
                found.append(subnode)
        return found

    def _contains_ok_attribute(self, node: ast.AST) -> bool:
        for subnode in ast.walk(node):
            if isinstance(subnode, ast.Attribute) and subnode.attr == "ok":
                return True
        return False

    def _has_selector_call(self, node: ast.AST) -> bool:
        for subnode in ast.walk(node):
            if not isinstance(subnode, ast.Call):
                continue
            if isinstance(subnode.func, ast.Attribute) and subnode.func.attr == "is_":
                return True
        return False

    def _has_branch_call(self, node: ast.AST) -> bool:
        return bool(self._find_branch_calls(node))

    def _is_supported_for_iter(self, node: ast.AST) -> bool:
        if isinstance(node, (ast.List, ast.Tuple)):
            return True
        if isinstance(node, ast.Call):
            if not isinstance(node.func, ast.Name) or node.func.id != "range":
                return False
            if node.keywords:
                return False
            if not 1 <= len(node.args) <= 3:
                return False
            return all(
                isinstance(arg, ast.Constant) and isinstance(arg.value, int)
                for arg in node.args
            )
        return False

    def visit_Assign(self, node: ast.Assign) -> Any:
        if isinstance(node.value, ast.Call):
            if self._is_branch_selector_call(node.value):
                self._add_issue(
                    InvalidBranchUsageError,
                    "checkpoint(branches=[...]) is no longer supported.",
                    hint=(
                        "Create a plain checkpoint first, then use "
                        "`if journey.branch(start_from=checkpoint):` / "
                        "`elif journey.branch(start_from=checkpoint):`."
                    ),
                )
            elif self._is_branch_call(node.value):
                self._add_issue(
                    InvalidBranchUsageError,
                    "journey.branch(...) is only valid as a direct if/elif condition.",
                    hint="Use journey.branch(...) directly as `if journey.branch(...):` or `elif journey.branch(...):`.",
                )
        self.generic_visit(node)

    def visit_While(self, node: ast.While) -> Any:
        self._add_issue(
            UnsupportedLoopError,
            "while loops are not supported in journey v1.",
            hint="Use step(..., retry=..., retry_delay=..., retry_from=...) for polling instead of a while loop.",
        )

    def visit_For(self, node: ast.For) -> Any:
        if not self._is_supported_for_iter(node.iter):
            self._add_issue(
                UnsupportedLoopError,
                "for loops must iterate over a literal list or tuple, or over range() with literal integers.",
                hint="Move dynamic iteration into a step function if you need more flexible looping.",
            )
        self.generic_visit(node)

    def visit_If(self, node: ast.If) -> Any:
        if self._is_elif_node(node):
            return

        chain: list[ast.If] = []
        cursor: ast.If | None = node
        while cursor is not None:
            chain.append(cursor)
            if len(cursor.orelse) == 1 and isinstance(cursor.orelse[0], ast.If):
                cursor = cursor.orelse[0]
            else:
                cursor = None

        tests = [if_node.test for if_node in chain]
        branch_calls_per_test = [self._find_branch_calls(test) for test in tests]
        has_branch_flags = [bool(branch_calls) for branch_calls in branch_calls_per_test]

        if any(has_branch_flags) and not all(has_branch_flags):
            self._add_issue(
                InvalidBranchUsageError,
                "Every condition in a branch-selection if/elif chain must use journey.branch(...).",
                hint="Do not mix branch selection checks with plain `if` conditions in the same chain.",
            )

        direct_branch_calls: list[ast.Call] = []
        for if_node in chain:
            branch_calls = self._find_branch_calls(if_node.test)
            if branch_calls:
                if (
                    not isinstance(if_node.test, ast.Call)
                    or len(branch_calls) != 1
                    or branch_calls[0] is not if_node.test
                ):
                    self._add_issue(
                        InvalidBranchUsageError,
                        "Use journey.branch(...) as the whole condition in each if/elif branch.",
                        hint="Do not combine journey.branch(...) with `and`, `or`, or other comparisons.",
                    )
                else:
                    call_node = branch_calls[0]
                    direct_branch_calls.append(call_node)
                    self.allowed_branch_call_ids.add(id(call_node))
            else:
                if self._has_selector_call(if_node.test):
                    self._add_issue(
                        InvalidBranchUsageError,
                        "Branch selectors with `.is_(...)` are no longer supported.",
                        hint="Use journey.branch(...) directly as the whole if/elif condition instead.",
                    )
                if self._contains_ok_attribute(if_node.test):
                    self._add_issue(
                        UnsupportedControlFlowError,
                        "Branching on prior step result fields is not supported in journey v1.",
                        hint="Move that decision into separate steps or explicit branch() cases instead.",
                    )

        if len(direct_branch_calls) == len(chain):
            template_key = self._absolute_site(direct_branch_calls[0])
            total_conditions = len(direct_branch_calls)
            for index, call_node in enumerate(direct_branch_calls, start=1):
                self.branch_conditions[self._absolute_site(call_node)] = BranchConditionSpec(
                    template_key=template_key,
                    branch_key=f"branch_{index}",
                    condition_index=index,
                    total_conditions=total_conditions,
                )

        for if_node in chain:
            for stmt in if_node.body:
                self.visit(stmt)

        tail = chain[-1]
        if not (len(tail.orelse) == 1 and isinstance(tail.orelse[0], ast.If)):
            for stmt in tail.orelse:
                self.visit(stmt)

    def visit_Lambda(self, node: ast.Lambda) -> Any:
        if self._has_branch_call(node):
            self._add_issue(
                InvalidBranchUsageError,
                "journey.branch(...) cannot be used inside a lambda.",
            )
        self.generic_visit(node)

    def visit_ListComp(self, node: ast.ListComp) -> Any:
        if self._has_branch_call(node):
            self._add_issue(
                InvalidBranchUsageError,
                "journey.branch(...) cannot be used inside a comprehension.",
            )
        self.generic_visit(node)

    def visit_SetComp(self, node: ast.SetComp) -> Any:
        if self._has_branch_call(node):
            self._add_issue(
                InvalidBranchUsageError,
                "journey.branch(...) cannot be used inside a comprehension.",
            )
        self.generic_visit(node)

    def visit_DictComp(self, node: ast.DictComp) -> Any:
        if self._has_branch_call(node):
            self._add_issue(
                InvalidBranchUsageError,
                "journey.branch(...) cannot be used inside a comprehension.",
            )
        self.generic_visit(node)

    def visit_GeneratorExp(self, node: ast.GeneratorExp) -> Any:
        if self._has_branch_call(node):
            self._add_issue(
                InvalidBranchUsageError,
                "journey.branch(...) cannot be used inside a comprehension.",
            )
        self.generic_visit(node)

    def visit_Call(self, node: ast.Call) -> Any:
        if isinstance(node.func, ast.Name) and node.func.id == "resume":
            self._add_issue(
                UnsupportedControlFlowError,
                "Direct resume(...) calls are not supported in journey v1.",
                hint="Use `journey execute --state ...` to resume a run instead.",
            )

        if self._is_branch_selector_call(node):
            self._add_issue(
                InvalidBranchUsageError,
                "checkpoint(branches=[...]) is no longer supported.",
                hint=(
                    "Create a plain checkpoint first, then use "
                    "`if journey.branch(start_from=checkpoint):` / "
                    "`elif journey.branch(start_from=checkpoint):`."
                ),
            )

        if isinstance(node.func, ast.Attribute) and node.func.attr == "is_":
            self._add_issue(
                InvalidBranchUsageError,
                "Branch selectors with `.is_(...)` are no longer supported.",
                hint="Use journey.branch(...) directly as the whole if/elif condition instead.",
            )

        branch_calls = self._find_branch_calls(node)
        if branch_calls and id(node) not in self.allowed_branch_call_ids:
            self._add_issue(
                InvalidBranchUsageError,
                "journey.branch(...) is only valid as a direct if/elif condition.",
                hint="Use journey.branch(...) directly as `if journey.branch(...):` or `elif journey.branch(...):`.",
            )

        self.generic_visit(node)


def validate_journey(journey_fn: Any) -> JourneyValidation:
    """Validate the journey source against v1 authoring constraints."""

    try:
        source_lines, source_start_line = inspect.getsourcelines(journey_fn)
    except (OSError, TypeError) as exc:
        raise UnsupportedControlFlowError(
            "Journey source code could not be inspected for validation.",
            hint="Define the journey in a regular Python module instead of generating it dynamically.",
        ) from exc

    source = "".join(source_lines)
    source_col_offset = len(source_lines[0]) - len(source_lines[0].lstrip())
    source = textwrap.dedent(source)
    module_ast = ast.parse(source)

    fn_node: ast.FunctionDef | ast.AsyncFunctionDef | None = None
    for stmt in module_ast.body:
        if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef)):
            fn_node = stmt
            break

    if fn_node is None:
        raise UnsupportedControlFlowError(
            "The inspected journey source did not resolve to a function definition."
        )

    validator = _JourneyValidator(
        fn_node,
        source_line_offset=source_start_line - 1,
        source_col_offset=source_col_offset,
    )
    validator.validate()

    if validator.issues:
        issue = validator.issues[0]
        if issubclass(issue.exc_type, JourneyError):
            raise issue.exc_type(issue.message, hint=issue.hint)
        raise issue.exc_type(issue.message)

    return JourneyValidation(branch_conditions=dict(validator.branch_conditions))
