"""AST guardrails for supported journey authoring patterns."""

from __future__ import annotations

import ast
import inspect
import textwrap
from dataclasses import dataclass
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


class _JourneyValidator(ast.NodeVisitor):
    def __init__(self, function_node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        self.function_node = function_node
        self.selector_names: set[str] = set()
        self.allowed_branch_call_ids: set[int] = set()
        self.issues: list[_ValidationIssue] = []
        self.parents: dict[int, ast.AST] = {}

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

    def _find_branch_is_calls(self, node: ast.AST) -> list[tuple[str, ast.Call]]:
        found: list[tuple[str, ast.Call]] = []
        for subnode in ast.walk(node):
            if not isinstance(subnode, ast.Call):
                continue
            func = subnode.func
            if not isinstance(func, ast.Attribute) or func.attr != "is_":
                continue
            if isinstance(func.value, ast.Name):
                found.append((func.value.id, subnode))
        return found

    def _contains_ok_attribute(self, node: ast.AST) -> bool:
        for subnode in ast.walk(node):
            if isinstance(subnode, ast.Attribute) and subnode.attr == "ok":
                return True
        return False

    def _has_branch_is_call(self, node: ast.AST) -> bool:
        return bool(self._find_branch_is_calls(node))

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
        if isinstance(node.value, ast.Call) and self._is_branch_selector_call(node.value):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    self.selector_names.add(target.id)
                else:
                    self._add_issue(
                        InvalidBranchUsageError,
                        "Assign the result of checkpoint(branches=[...]) to one variable before using it in if/elif checks.",
                        hint="Store the selector in a variable like `selected = checkpoint(branches=[...])`.",
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

        chain_selector: str | None = None
        chain_has_branch = False
        for if_node in chain:
            branch_calls = self._find_branch_is_calls(if_node.test)
            if branch_calls:
                chain_has_branch = True
                if not isinstance(if_node.test, ast.Call) or len(branch_calls) != 1:
                    self._add_issue(
                        InvalidBranchUsageError,
                        "Use branch.is_(...) as the whole condition in each if/elif branch.",
                        hint="Do not combine branch.is_(...) with `and`, `or`, or other comparisons.",
                    )
                selectors = {selector for selector, _ in branch_calls}
                if len(selectors) != 1:
                    self._add_issue(
                        InvalidBranchUsageError,
                        "Each if/elif condition can check only one branch selector.",
                    )
                selector = next(iter(selectors))
                if chain_selector is None:
                    chain_selector = selector
                elif chain_selector != selector:
                    self._add_issue(
                        InvalidBranchUsageError,
                        "One if/elif chain can only use one branch selector.",
                    )
                call_node = branch_calls[0][1]
                self.allowed_branch_call_ids.add(id(call_node))
            else:
                if chain_has_branch:
                    self._add_issue(
                        InvalidBranchUsageError,
                        "Every condition in a branch-selection if/elif chain must use selector.is_(...).",
                        hint="Do not mix branch selection checks with plain `if` conditions in the same chain.",
                    )
                if self._contains_ok_attribute(if_node.test):
                    self._add_issue(
                        UnsupportedControlFlowError,
                        "Branching on prior step result fields is not supported in journey v1.",
                        hint="Move that decision into separate steps or explicit branch() cases instead.",
                    )

        if chain_has_branch and chain_selector not in self.selector_names:
            self._add_issue(
                InvalidBranchUsageError,
                "branch.is_(...) must be called on the selector returned by checkpoint(branches=[...]).",
            )

        for if_node in chain:
            for stmt in if_node.body:
                self.visit(stmt)

        tail = chain[-1]
        if not (len(tail.orelse) == 1 and isinstance(tail.orelse[0], ast.If)):
            for stmt in tail.orelse:
                self.visit(stmt)

    def visit_Name(self, node: ast.Name) -> Any:
        if node.id in self.selector_names and isinstance(node.ctx, ast.Load):
            parent = self.parents.get(id(node))
            if isinstance(parent, ast.Attribute) and parent.value is node and parent.attr == "is_":
                return
            self._add_issue(
                InvalidBranchUsageError,
                "A branch selector can only be used directly in the matching if/elif chain.",
                hint="Do not store the selector elsewhere or pass it into helper functions.",
            )

    def visit_Lambda(self, node: ast.Lambda) -> Any:
        if self._has_branch_is_call(node):
            self._add_issue(
                InvalidBranchUsageError,
                "branch.is_(...) cannot be used inside a lambda.",
            )
        self.generic_visit(node)

    def visit_ListComp(self, node: ast.ListComp) -> Any:
        if self._has_branch_is_call(node):
            self._add_issue(
                InvalidBranchUsageError,
                "branch.is_(...) cannot be used inside a comprehension.",
            )
        self.generic_visit(node)

    def visit_SetComp(self, node: ast.SetComp) -> Any:
        if self._has_branch_is_call(node):
            self._add_issue(
                InvalidBranchUsageError,
                "branch.is_(...) cannot be used inside a comprehension.",
            )
        self.generic_visit(node)

    def visit_DictComp(self, node: ast.DictComp) -> Any:
        if self._has_branch_is_call(node):
            self._add_issue(
                InvalidBranchUsageError,
                "branch.is_(...) cannot be used inside a comprehension.",
            )
        self.generic_visit(node)

    def visit_GeneratorExp(self, node: ast.GeneratorExp) -> Any:
        if self._has_branch_is_call(node):
            self._add_issue(
                InvalidBranchUsageError,
                "branch.is_(...) cannot be used inside a comprehension.",
            )
        self.generic_visit(node)

    def visit_Call(self, node: ast.Call) -> Any:
        if isinstance(node.func, ast.Name) and node.func.id == "resume":
            self._add_issue(
                UnsupportedControlFlowError,
                "Direct resume(...) calls are not supported in journey v1.",
                hint="Use `journey execute --state ...` to resume a run instead.",
            )

        branch_calls = self._find_branch_is_calls(node)
        if branch_calls and id(node) not in self.allowed_branch_call_ids:
            self._add_issue(
                InvalidBranchUsageError,
                "branch.is_(...) is only valid as a direct if/elif condition.",
            )

        self.generic_visit(node)


def validate_journey(journey_fn: Any) -> None:
    """Validate the journey source against v1 authoring constraints."""

    try:
        source = inspect.getsource(journey_fn)
    except (OSError, TypeError) as exc:
        raise UnsupportedControlFlowError(
            "Journey source code could not be inspected for validation.",
            hint="Define the journey in a regular Python module instead of generating it dynamically.",
        ) from exc

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

    validator = _JourneyValidator(fn_node)
    validator.validate()

    if validator.issues:
        issue = validator.issues[0]
        if issubclass(issue.exc_type, JourneyError):
            raise issue.exc_type(issue.message, hint=issue.hint)
        raise issue.exc_type(issue.message)
