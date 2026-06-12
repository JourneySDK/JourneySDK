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
from .types import JourneyEntrypoint


@dataclass
class _ValidationIssue:
    exc_type: type[Exception]
    message: str
    hint: str | None = None


BranchSiteKey = tuple[int, int]
BranchTemplateKey = tuple[int, int]
BranchHandleGroupKey = tuple[BranchSiteKey, ...]


@dataclass(frozen=True)
class BranchHandleDefinitionSpec:
    name: str


@dataclass(frozen=True)
class BranchConditionSpec:
    template_key: BranchTemplateKey
    branch_key: str
    condition_index: int
    total_conditions: int
    handle_site: BranchSiteKey | None = None
    handle_name: str | None = None
    handle_group_key: BranchHandleGroupKey | None = None


@dataclass(frozen=True)
class JourneyValidation:
    branch_conditions: dict[BranchSiteKey, BranchConditionSpec]
    branch_handle_definitions: dict[BranchSiteKey, BranchHandleDefinitionSpec]


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
        self.branch_handle_definitions: dict[BranchSiteKey, BranchHandleDefinitionSpec] = {}
        self.branch_handles_by_name: dict[str, BranchSiteKey] = {}
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

    def _has_branch_call(self, node: ast.AST) -> bool:
        return bool(self._find_branch_calls(node))

    def _find_branch_handle_names(self, node: ast.AST) -> list[ast.Name]:
        found: list[ast.Name] = []
        for subnode in ast.walk(node):
            if isinstance(subnode, ast.Name) and subnode.id in self.branch_handles_by_name:
                found.append(subnode)
        return found

    def _forget_assigned_branch_handles(self, targets: list[ast.expr]) -> None:
        for target in targets:
            if isinstance(target, ast.Name):
                self.branch_handles_by_name.pop(target.id, None)

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
            if self._is_branch_call(node.value):
                if len(node.targets) != 1 or not isinstance(node.targets[0], ast.Name):
                    self._add_issue(
                        InvalidBranchUsageError,
                        "Assigned journey.branch(...) handles must use one simple variable target.",
                        hint="Use `branch_a = journey.branch(...)`, then check that variable directly in an if/elif chain.",
                    )
                else:
                    target = node.targets[0]
                    site = self._absolute_site(node.value)
                    self.allowed_branch_call_ids.add(id(node.value))
                    self.branch_handle_definitions[site] = BranchHandleDefinitionSpec(
                        name=target.id,
                    )
                    self.branch_handles_by_name[target.id] = site
        else:
            self._forget_assigned_branch_handles(node.targets)
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
        branch_handles_per_test = [self._find_branch_handle_names(test) for test in tests]
        has_branch_flags = [
            bool(branch_calls) or bool(branch_handles)
            for branch_calls, branch_handles in zip(
                branch_calls_per_test,
                branch_handles_per_test,
            )
        ]

        if any(has_branch_flags) and not all(has_branch_flags):
            self._add_issue(
                InvalidBranchUsageError,
                "Every condition in a branch-selection if/elif chain must use journey.branch(...).",
                hint="Do not mix branch selection checks with plain `if` conditions in the same chain.",
            )

        branch_conditions: list[tuple[ast.AST, BranchSiteKey | None, str | None]] = []
        for if_node, branch_calls, branch_handles in zip(
            chain,
            branch_calls_per_test,
            branch_handles_per_test,
        ):
            if branch_calls:
                if (
                    not isinstance(if_node.test, ast.Call)
                    or len(branch_calls) != 1
                    or branch_calls[0] is not if_node.test
                    or branch_handles
                ):
                    self._add_issue(
                        InvalidBranchUsageError,
                        "Use journey.branch(...) as the whole condition in each if/elif branch.",
                        hint="Do not combine journey.branch(...) with `and`, `or`, or other comparisons.",
                    )
                else:
                    call_node = branch_calls[0]
                    branch_conditions.append((call_node, None, None))
                    self.allowed_branch_call_ids.add(id(call_node))
            elif branch_handles:
                if (
                    not isinstance(if_node.test, ast.Name)
                    or len(branch_handles) != 1
                    or branch_handles[0] is not if_node.test
                ):
                    self._add_issue(
                        InvalidBranchUsageError,
                        "Use an assigned journey.branch(...) handle as the whole condition in each if/elif branch.",
                        hint="Do not combine branch handles with `and`, `or`, `not`, or other comparisons.",
                    )
                else:
                    handle_site = self.branch_handles_by_name[if_node.test.id]
                    branch_conditions.append((if_node.test, handle_site, if_node.test.id))
            else:
                if self._contains_ok_attribute(if_node.test):
                    self._add_issue(
                        UnsupportedControlFlowError,
                        "Branching on prior step result fields is not supported in journey v1.",
                        hint="Move that decision into separate steps or explicit branch() cases instead.",
                    )

        if len(branch_conditions) == len(chain):
            template_key = self._absolute_site(branch_conditions[0][0])
            total_conditions = len(branch_conditions)
            handle_sites = tuple(
                handle_site
                for _, handle_site, _ in branch_conditions
                if handle_site is not None
            )
            handle_group_key = (
                handle_sites
                if len(handle_sites) == len(branch_conditions)
                else None
            )
            for index, (condition_node, handle_site, handle_name) in enumerate(
                branch_conditions,
                start=1,
            ):
                self.branch_conditions[self._absolute_site(condition_node)] = BranchConditionSpec(
                    template_key=template_key,
                    branch_key=f"branch_{index}",
                    condition_index=index,
                    total_conditions=total_conditions,
                    handle_site=handle_site,
                    handle_name=handle_name,
                    handle_group_key=handle_group_key,
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
                hint="Rerun the same Journey command to resume from persistent state.",
            )

        branch_calls = self._find_branch_calls(node)
        if branch_calls and id(node) not in self.allowed_branch_call_ids:
            self._add_issue(
                InvalidBranchUsageError,
                "journey.branch(...) is only valid as a direct if/elif condition.",
                hint="Use journey.branch(...) directly as `if journey.branch(...):` or `elif journey.branch(...):`.",
            )

        self.generic_visit(node)


def validate_journey(journey_fn: JourneyEntrypoint) -> JourneyValidation:
    """Validate the journey source against v1 authoring constraints."""

    branch_conditions: dict[BranchSiteKey, BranchConditionSpec] = {}
    branch_handle_definitions: dict[BranchSiteKey, BranchHandleDefinitionSpec] = {}
    visited_helpers: set[int] = set()

    def validate_function(fn: Any, *, include_helpers: bool) -> ast.AST:
        try:
            source_lines, source_start_line = inspect.getsourcelines(fn)
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
        expected_name = getattr(fn, "__name__", None)
        for stmt in module_ast.body:
            if not isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            if expected_name is None or stmt.name == expected_name:
                fn_node = stmt
                break
            if fn_node is None:
                fn_node = stmt

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

        branch_conditions.update(validator.branch_conditions)
        branch_handle_definitions.update(validator.branch_handle_definitions)

        if include_helpers:
            for helper in _direct_helper_calls(fn, fn_node):
                if id(helper) in visited_helpers:
                    continue
                if not _function_source_mentions_branch(helper):
                    continue
                visited_helpers.add(id(helper))
                validate_function(helper, include_helpers=True)

        return fn_node

    visited_helpers.add(id(journey_fn))
    validate_function(journey_fn, include_helpers=True)

    return JourneyValidation(
        branch_conditions=dict(branch_conditions),
        branch_handle_definitions=dict(branch_handle_definitions),
    )


def _direct_helper_calls(
    fn: Any,
    function_node: ast.FunctionDef | ast.AsyncFunctionDef,
) -> list[Any]:
    namespace: dict[str, Any] = {}
    try:
        closure = inspect.getclosurevars(fn)
    except TypeError:
        closure = None
    namespace.update(getattr(fn, "__globals__", {}))
    if closure is not None:
        namespace.update(closure.globals)
        namespace.update(closure.nonlocals)

    helpers: list[Any] = []
    for node in ast.walk(function_node):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Name):
            continue
        name = node.func.id
        if name in {"branch", "step", "journey", "resume"}:
            continue
        helper = namespace.get(name)
        if inspect.isfunction(helper):
            helpers.append(helper)
    return helpers


def _function_source_mentions_branch(fn: Any) -> bool:
    try:
        source = inspect.getsource(fn)
    except (OSError, TypeError):
        return False
    return "branch(" in source or ".branch(" in source
