from __future__ import annotations

import ast
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SDK = ROOT / "journeysdk"
TOUCHPOINT_MODULES = ("browser", "docker", "email", "webhook")
EXTRA_PUBLIC_NAMES = {
    SDK / "api.py": {"is_journey_callable"},
}


def _parse(path: Path) -> ast.Module:
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def _literal_string_list(node: ast.AST) -> list[str] | None:
    if not isinstance(node, (ast.List, ast.Tuple)):
        return None
    values: list[str] = []
    for item in node.elts:
        if not isinstance(item, ast.Constant) or not isinstance(item.value, str):
            return None
        values.append(item.value)
    return values


def _module_all(path: Path) -> set[str]:
    tree = _parse(path)
    for node in tree.body:
        if isinstance(node, ast.Assign):
            if any(isinstance(target, ast.Name) and target.id == "__all__" for target in node.targets):
                values = _literal_string_list(node.value)
                if values is not None:
                    return set(values)
    raise AssertionError(f"{path.relative_to(ROOT)} does not define a literal __all__ list")


def _root_exports_by_module() -> dict[Path, set[str]]:
    init_path = SDK / "__init__.py"
    root_exports = _module_all(init_path)
    imported_by_name: dict[str, Path] = {}
    for node in _parse(init_path).body:
        if not isinstance(node, ast.ImportFrom) or node.level != 1 or node.module is None:
            continue
        module_path = SDK / f"{node.module}.py"
        for alias in node.names:
            imported_by_name[alias.asname or alias.name] = module_path

    exports_by_module: dict[Path, set[str]] = {}
    for name in root_exports:
        module_path = imported_by_name.get(name)
        if module_path is not None:
            exports_by_module.setdefault(module_path, set()).add(name)
    return exports_by_module


def _model_public_names() -> set[str]:
    names: set[str] = set()
    for node in _parse(SDK / "models.py").body:
        if isinstance(node, ast.ClassDef) and not node.name.startswith("_"):
            names.add(node.name)
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            if not node.target.id.startswith("_"):
                names.add(node.target.id)
    return names


def _definition_nodes(path: Path) -> dict[str, ast.AST]:
    definitions: dict[str, ast.AST] = {}
    for node in _parse(path).body:
        if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            definitions[node.name] = node
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            definitions[node.target.id] = node
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    definitions[target.id] = node
    return definitions


def _relative_imports(path: Path) -> dict[str, Path]:
    imports: dict[str, Path] = {}
    for node in _parse(path).body:
        if not isinstance(node, ast.ImportFrom) or node.level == 0 or node.module is None:
            continue
        base = path.parent
        for _ in range(node.level - 1):
            base = base.parent
        module_path = base.joinpath(*node.module.split(".")).with_suffix(".py")
        for alias in node.names:
            imports[alias.asname or alias.name] = module_path
    return imports


def _public_definition(path: Path, name: str) -> tuple[Path, ast.AST] | None:
    definitions = _definition_nodes(path)
    if name in definitions:
        return path, definitions[name]
    imported_path = _relative_imports(path).get(name)
    if imported_path is None:
        return None
    imported_definitions = _definition_nodes(imported_path)
    if name not in imported_definitions:
        return None
    return imported_path, imported_definitions[name]


def _annotation_name(node: ast.AST) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        parent = _annotation_name(node.value)
        return f"{parent}.{node.attr}" if parent is not None else node.attr
    return None


def _is_any(node: ast.AST) -> bool:
    name = _annotation_name(node)
    return name == "Any" or name == "typing.Any"


def _is_callable(node: ast.AST) -> bool:
    if not isinstance(node, ast.Subscript):
        return False
    name = _annotation_name(node.value)
    return name == "Callable" or name == "collections.abc.Callable"


def _check_annotation(
    annotation: ast.AST | None,
    *,
    path: Path,
    owner: str,
    allow_callable: bool = False,
) -> None:
    if annotation is None:
        return
    for node in ast.walk(annotation):
        if _is_any(node):
            raise AssertionError(
                f"{path.relative_to(ROOT)}:{owner} exposes Any in a public annotation"
            )
        if not allow_callable and _is_callable(node):
            raise AssertionError(
                f"{path.relative_to(ROOT)}:{owner} exposes anonymous Callable[...] "
                "instead of a named callable type"
            )


def _check_function_signature(path: Path, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
    owner = node.name
    args = [
        *node.args.posonlyargs,
        *node.args.args,
        *node.args.kwonlyargs,
    ]
    if node.args.vararg is not None:
        args.append(node.args.vararg)
    if node.args.kwarg is not None:
        args.append(node.args.kwarg)

    for arg in args:
        _check_annotation(arg.annotation, path=path, owner=f"{owner}.{arg.arg}")
    _check_annotation(node.returns, path=path, owner=f"{owner}.return")


def _check_class_signature(path: Path, node: ast.ClassDef) -> None:
    for item in node.body:
        if isinstance(item, ast.AnnAssign):
            target = item.target.id if isinstance(item.target, ast.Name) else node.name
            _check_annotation(item.annotation, path=path, owner=f"{node.name}.{target}")
        elif isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if item.name.startswith("_") and item.name not in {"__call__", "__exit__", "__restore__", "__store__"}:
                continue
            _check_function_signature(path, item)


def _check_public_definition(path: Path, node: ast.AST) -> None:
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
        _check_function_signature(path, node)
    elif isinstance(node, ast.ClassDef):
        _check_class_signature(path, node)
    elif isinstance(node, ast.AnnAssign):
        _check_annotation(
            node.annotation,
            path=path,
            owner=getattr(node.target, "id", "<alias>"),
            allow_callable=True,
        )
        _check_annotation(
            node.value,
            path=path,
            owner=getattr(node.target, "id", "<alias>"),
            allow_callable=True,
        )
    elif isinstance(node, ast.Assign):
        _check_annotation(node.value, path=path, owner="<alias>", allow_callable=True)


def test_public_sdk_exports_do_not_expose_any_or_anonymous_callable() -> None:
    exports_by_module = _root_exports_by_module()
    exports_by_module.setdefault(SDK / "models.py", set()).update(_model_public_names())
    for path, names in EXTRA_PUBLIC_NAMES.items():
        exports_by_module.setdefault(path, set()).update(names)

    for path, names in sorted(exports_by_module.items()):
        for name in sorted(names):
            definition = _public_definition(path, name)
            assert definition is not None, (
                f"{name} is exported but not defined in {path.relative_to(ROOT)}"
            )
            definition_path, node = definition
            _check_public_definition(definition_path, node)


def test_official_touchpoint_exports_do_not_expose_any_or_anonymous_callable() -> None:
    for module in TOUCHPOINT_MODULES:
        path = SDK / "touchpoints" / f"{module}.py"
        for name in sorted(_module_all(path)):
            definition = _public_definition(path, name)
            assert definition is not None, (
                f"{name} is exported but not defined in {path.relative_to(ROOT)}"
            )
            definition_path, node = definition
            _check_public_definition(definition_path, node)
