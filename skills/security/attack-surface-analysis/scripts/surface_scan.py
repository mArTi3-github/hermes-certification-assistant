#!/usr/bin/env python3
"""surface_scan.py - MVP attack-surface extractor.

Scope of this version (deliberately small):
  * Python sources only, FastAPI/Starlette routing conventions.
  * Static analysis, no build, no taint engine, no runtime probing.
  * Output: surface.json + standalone HTML report.

The extractor answers three questions for every network entry point:
  1. Where is the handler and which parameters does it accept?
  2. Which project code is reachable from it (call graph, depth-limited)?
  3. Is there any input validation on that path, and where are the risky sinks?

Everything it cannot resolve is reported as a limitation instead of guessed.
"""

from __future__ import annotations

import argparse
import ast
import html
import json
import os
import re
import subprocess
import sys
from collections import Counter, deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

# --------------------------------------------------------------------------
# Signal dictionaries (kept as data so other stacks can extend them later)
# --------------------------------------------------------------------------

HTTP_METHODS = {"get", "post", "put", "patch", "delete", "head", "options", "trace"}

ROUTE_DECORATOR = re.compile(r"^(?P<name>[\w\.]+)\.(?P<method>get|post|put|patch|delete|head|options|api_route|websocket)$")

# Validation: (kind, regex, weight). weight 2 = strong (schema / typed model),
# 1 = medium (explicit check), 0 = structural parse only.
VALIDATION_PATTERNS = [
    ("schema", r"\bmodel_validate\b|\bparse_obj\b|\.from_orm\b", 2),
    ("schema", r"\bBaseModel\b", 2),
    ("schema", r"\bField\s*\(", 2),
    ("schema", r"\bconstr\b|\bconint\b|\bconlist\b", 2),
    ("schema", r"\bjsonschema\b|\bvalidate\s*\(\s*instance\b", 2),
    ("constraint", r"\b(ge|le|gt|lt|min_length|max_length|pattern|regex)\s*=", 2),
    ("allowlist", r"ALLOW|ALLOWED|allowlist|whitelist|_PERMITTED|VALID_\w+", 1),
    ("regex", r"re\.(fullmatch|match|compile)\s*\(", 1),
    ("explicit-check", r"\bif\s+not\s+isinstance\s*\(|\bif\s+isinstance\s*\(", 1),
    ("explicit-check", r"\bmax_file_size\b|\bmax_size\b|\blen\s*\([^)]*\)\s*[<>]=?", 1),
    ("explicit-check", r"\bstatus_code\s*=\s*(400|413|415|422)\b", 1),
    ("explicit-check", r"\bHTTPException\s*\(\s*status_code\s*=\s*(400|413|415|422)", 1),
    ("url-policy", r"_is_private_url|\bvalidate_url\s*\(", 2),
    ("parse", r"\bjson\.loads?\b", 0),
    ("parse", r"\bint\s*\(|\bfloat\s*\(", 0),
]

SINK_PATTERNS = [
    ("process-exec", r"\bsubprocess\.(run|Popen|call|check_output|check_call)\b|\bos\.system\b|\bos\.exec"),
    ("dynamic-code", r"\beval\s*\(|\bexec\s*\("),
    ("deserialization", r"\bpickle\.loads?\b|\byaml\.load\s*\(|\bmarshal\.loads?\b"),
    ("sql", r"\.execute\s*\(|\.executemany\s*\(|\bcursor\s*\("),
    ("filesystem-write", r"\bopen\s*\([^)]*[\"'][wa]|\.write_text\s*\(|\.write_bytes\s*\(|shutil\.(copy|move|rmtree)"),
    ("archive-extract", r"\.extractall\s*\(|\btarfile\.open\b|\bzipfile\.ZipFile\b"),
    ("outbound-http", r"\b(requests|httpx|aiohttp)\.(get|post|put|head|request)\s*\(|\burllib\.request\b"),
    ("template", r"\bTemplate\s*\(|jit2?\b|render_template"),
]

AUTH_PATTERNS = [
    ("dependency", r"\bDepends\s*\("),
    ("security", r"\bSecurity\s*\("),
    ("basic", r"HTTPBasic|HTTPBearer|OAuth2|APIKeyHeader|APIKeyQuery"),
    ("middleware", r"\badd_middleware\b|@app\.middleware"),
    ("session-user", r"request\.state\.user|request\.user\b|current_user"),
    ("manual-header", r"headers\.get\s*\(\s*[\"'](authorization|x-api-key)"),
]

EXCLUDE_DIRS = {
    ".git", "node_modules", "__pycache__", ".venv", "venv", "dist", "build",
    ".mypy_cache", ".pytest_cache", ".ruff_cache", "htmlcov", "coverage",
}


# --------------------------------------------------------------------------
# AST model
# --------------------------------------------------------------------------

@dataclass
class FuncInfo:
    qual: str                      # module.Class.method | module.function
    name: str
    file: str                      # repo-relative
    lineno: int
    end_lineno: int
    params: list
    decorators: list[str]
    calls: list[tuple[str, int]] = field(default_factory=list)   # (dotted callee, line)
    source: str = ""
    cls: str | None = None
    returns: str = ""
    local_types: dict[str, str] = field(default_factory=dict)
    assigns: list[tuple[str, str, str]] = field(default_factory=list)

    def line_texts(self) -> list[tuple[int, str]]:
        base = self.lineno
        return [(base + i, line) for i, line in enumerate(self.source.splitlines())]


@dataclass
class ModuleInfo:
    rel: str
    qual: str
    path: Path
    imports: dict[str, str] = field(default_factory=dict)   # local name -> module qual
    funcs: dict[str, FuncInfo] = field(default_factory=dict)  # qual -> info
    classes: dict[str, dict] = field(default_factory=dict)    # class -> {"attrs": {attr: class}}
    source: str = ""


def dotted(node: ast.AST) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        base = dotted(node.value)
        return f"{base}.{node.attr}" if base else node.attr
    if isinstance(node, ast.Call):
        return dotted(node.func)
    return ""


def _walk_body(func: ast.AST):
    """Walk a function body without descending into nested defs/classes."""
    stack = list(getattr(func, "body", []))
    while stack:
        node = stack.pop()
        yield node
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                continue
            stack.append(child)


def module_qual(rel: str) -> str:
    p = rel[:-3] if rel.endswith(".py") else rel
    parts = [x for x in p.split("/") if x not in ("", ".")]
    if parts and parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(parts)


class Repo:
    def __init__(self, root: Path):
        self.root = root
        self.modules: dict[str, ModuleInfo] = {}     # module qual -> info
        self.by_file: dict[str, ModuleInfo] = {}
        self.errors: list[dict] = []
        self.parse_ok = 0
        self.parse_fail = 0
        self.state_attrs: dict[str, str] = {}        # app.state.<attr> -> ClassName
        self.all_funcs: dict[str, FuncInfo] = {}     # fully qualified name -> function

    # -- walking ---------------------------------------------------------
    def python_files(self) -> list[Path]:
        out = []
        for dirpath, dirnames, filenames in os.walk(self.root):
            dirnames[:] = [d for d in dirnames if d not in EXCLUDE_DIRS and not d.startswith(".")]
            for fn in filenames:
                if fn.endswith(".py"):
                    out.append(Path(dirpath) / fn)
        return sorted(out)

    def load(self) -> None:
        for path in self.python_files():
            rel = str(path.relative_to(self.root)).replace(os.sep, "/")
            try:
                src = path.read_text(encoding="utf-8", errors="replace")
                tree = ast.parse(src)
            except SyntaxError as exc:
                self.parse_fail += 1
                self.errors.append({"file": rel, "error": f"SyntaxError: {exc.msg} (line {exc.lineno})"})
                continue
            self.parse_ok += 1
            mod = ModuleInfo(rel=rel, qual=module_qual(rel), path=path, source=src)
            self.modules[mod.qual] = mod
            self.by_file[rel] = mod
            self._collect_imports(mod, tree)
            self._collect_defs(mod, tree)
        self._collect_state_attrs()
        self._infer_local_types()

    def _collect_imports(self, mod: ModuleInfo, tree: ast.Module) -> None:
        pkg_parts = mod.qual.split(".")

        def resolve(node: ast.ImportFrom) -> str | None:
            if node.level:
                base = pkg_parts[: len(pkg_parts) - node.level]
                if node.module:
                    base = base + node.module.split(".")
                return ".".join(base)
            return node.module or None

        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                target = resolve(node)
                if not target:
                    continue
                for alias in node.names:
                    local = alias.asname or alias.name
                    mod.imports[local] = f"{target}.{alias.name}" if alias.name != "*" else target
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    local = alias.asname or alias.name.split(".")[0]
                    mod.imports[local] = alias.name

    def _collect_defs(self, mod: ModuleInfo, tree: ast.Module) -> None:
        """Collect classes and functions with class-qualified names.

        Class nesting matters for the call graph: method qualifiers must be
        `module.Class.method`, otherwise every method is attributed to the module
        and `self.foo()` cannot be resolved to its real definition.
        """

        def walk(node: ast.AST, cls_prefix: str | None) -> None:
            for child in ast.iter_child_nodes(node):
                if isinstance(child, ast.ClassDef):
                    attrs = {}
                    for sub in ast.walk(child):
                        if isinstance(sub, ast.Assign) and sub.targets and isinstance(sub.targets[0], ast.Attribute):
                            tgt = sub.targets[0]
                            if isinstance(tgt.value, ast.Name) and tgt.value.id == "self" and isinstance(sub.value, ast.Call):
                                attrs[tgt.attr] = dotted(sub.value.func).split(".")[-1]
                    mod.classes[child.name] = {"attrs": attrs, "bases": [dotted(b) for b in child.bases]}
                    walk(child, f"{cls_prefix}.{child.name}" if cls_prefix else child.name)
                elif isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    params = []
                    pos = list(child.args.posonlyargs) + list(child.args.args)
                    defaults = [None] * (len(pos) - len(child.args.defaults)) + list(child.args.defaults)
                    for a, d in zip(pos, defaults):
                        if a.arg in ("self", "cls"):
                            continue
                        ann = ast.unparse(a.annotation) if a.annotation else ""
                        default = ast.unparse(d) if d is not None else ""
                        params.append({"name": a.arg, "annotation": ann, "default": default})
                    for a, d in zip(child.args.kwonlyargs, child.args.kw_defaults):
                        ann = ast.unparse(a.annotation) if a.annotation else ""
                        default = ast.unparse(d) if d is not None else ""
                        params.append({"name": a.arg, "annotation": ann, "default": default})
                    decos = [ast.unparse(d) for d in child.decorator_list]
                    prefix = f"{cls_prefix}." if cls_prefix else ""
                    qual = f"{mod.qual}.{prefix}{child.name}"
                    info = FuncInfo(
                        qual=qual, name=child.name, file=mod.rel, lineno=child.lineno,
                        end_lineno=child.end_lineno or child.lineno, params=params,
                        decorators=decos, cls=cls_prefix,
                        returns=(ast.unparse(child.returns) if child.returns else ""),
                        source="\n".join(mod.source.splitlines()[child.lineno - 1: (child.end_lineno or child.lineno)]),
                    )
                    for sub in ast.walk(child):
                        if isinstance(sub, ast.Call):
                            callee = dotted(sub.func)
                            if callee:
                                info.calls.append((callee, sub.lineno))
                    for sub in _walk_body(child):
                        if isinstance(sub, ast.AnnAssign) and isinstance(sub.target, ast.Name):
                            ann = dotted(sub.annotation) if not isinstance(sub.annotation, ast.Call) else ""
                            if ann:
                                info.assigns.append((sub.target.id, "annotation", ann))
                        elif isinstance(sub, ast.Assign) and len(sub.targets) == 1 and isinstance(sub.targets[0], ast.Name):
                            name = sub.targets[0].id
                            if isinstance(sub.value, ast.Call):
                                fn = dotted(sub.value.func)
                                if fn == "getattr" and len(sub.value.args) >= 2 and isinstance(sub.value.args[1], ast.Constant):
                                    info.assigns.append((name, "getattr", str(sub.value.args[1].value)))
                                else:
                                    info.assigns.append((name, "ctor", fn))
                            elif isinstance(sub.value, ast.Attribute):
                                info.assigns.append((name, "attr", dotted(sub.value)))
                    mod.funcs[qual] = info
                    self.all_funcs[qual] = info
                    walk(child, cls_prefix)
                else:
                    walk(child, cls_prefix)

        walk(tree, None)

    def _infer_local_types(self) -> None:
        """Give a local variable a project class when the code states it.

        Sources: `x: Cls = ...`, `x = Cls(...)`, `x = app.state.<attr>` where the
        attribute was assigned a concrete class elsewhere. This is what lets the
        call graph cross service layers without a real type checker.
        """
        for mod in self.modules.values():
            for func in mod.funcs.values():
                for name, kind, value in func.assigns:
                    cls = ""
                    if kind == "annotation":
                        cls = value.split(".")[-1]
                    elif kind == "ctor":
                        cls = value.split(".")[-1]
                    elif kind == "attr" and ".state." in f".{value}":
                        cls = self.state_attrs.get(value.split(".")[-1], "")
                    elif kind == "getattr":
                        cls = self.state_attrs.get(value, "")
                    if not cls:
                        continue
                    if not (cls[:1].isupper() and self._class_exists(cls)):
                        # `x = helper()` where helper's return annotation names a project class
                        fn = mod.funcs.get(f"{mod.qual}.{value.split('.')[-1]}")
                        ret = (fn.returns if fn else "").split("|")[0].strip()
                        cls = ret.split(".")[-1] if ret else ""
                    if cls and cls[:1].isupper() and self._class_exists(cls):
                        func.local_types[name] = cls

    def is_model_class(self, name: str, depth: int = 3) -> bool:
        """True when `name` is a project class deriving from pydantic BaseModel."""
        for mod in self.modules.values():
            info = mod.classes.get(name)
            if info is None:
                continue
            bases = [b.split(".")[-1] for b in info.get("bases", [])]
            if "BaseModel" in bases:
                return True
            if depth > 0:
                return any(self.is_model_class(b, depth - 1) for b in bases if b[:1].isupper())
            return False
        return False

    def _class_exists(self, cls: str) -> bool:
        return any(cls in m.classes for m in self.modules.values())

    def is_external(self, mod: ModuleInfo | None, callee: str) -> bool:
        """True for calls into third-party code (imports that resolve outside the repo)."""
        if mod is None:
            return False
        head = callee.split(".")[0]
        target = mod.imports.get(head)
        if not target:
            return False
        top = ".".join(target.split(".")[:2])
        return not any(m.qual == top or m.qual.startswith(top + ".") for m in self.modules.values())

    def _collect_state_attrs(self) -> None:
        # app.state.<attr> = ClassName(...) or <x>.state.<attr> = ClassName(...)
        for mod in self.modules.values():
            try:
                tree = ast.parse(mod.source)
            except SyntaxError:
                continue
            for node in ast.walk(tree):
                if not isinstance(node, ast.Assign):
                    continue
                for tgt in node.targets:
                    if isinstance(tgt, ast.Attribute) and isinstance(tgt.value, ast.Attribute) and tgt.value.attr == "state":
                        cls = ""
                        if isinstance(node.value, ast.Call):
                            cls = dotted(node.value.func).split(".")[-1]
                        self.state_attrs[tgt.attr] = cls

    # -- resolution ------------------------------------------------------
    def find_func(self, qual: str) -> FuncInfo | None:
        return self.all_funcs.get(qual)

    def _method(self, cls: str | None, meth: str) -> FuncInfo | None:
        if not cls:
            return None
        for mod in self.modules.values():
            info = mod.funcs.get(f"{mod.qual}.{cls}.{meth}")
            if info is not None:
                return info
        return None

    def resolve_call(self, caller: FuncInfo, callee: str) -> FuncInfo | None:
        parts = callee.split(".")
        mod = self.by_file.get(caller.file)
        # local variable with a known project class: svc = Service(...) / svc: Service
        if parts[0] in caller.local_types and len(parts) >= 2:
            found = self._method(caller.local_types[parts[0]], parts[1])
            if found:
                return found
        # self.method -> same class
        if parts[0] in ("self", "cls"):
            if len(parts) == 2:
                return self._method(caller.cls, parts[1])
            if len(parts) >= 3 and caller.cls:
                attrs = (mod.classes.get(caller.cls.split(".")[-1], {}) if mod else {}).get("attrs", {})
                found = self._method(attrs.get(parts[1]), parts[2])
                if found:
                    return found
        # app.state.<attr>.method / request.app.state.<attr>.method
        if "state" in parts:
            idx = parts.index("state")
            if idx + 2 < len(parts):
                found = self._method(self.state_attrs.get(parts[idx + 1]), parts[idx + 2])
                if found:
                    return found
        if mod is None:
            return None
        # bare name in the same module
        if len(parts) == 1:
            cand = f"{mod.qual}.{parts[0]}"
            if cand in mod.funcs:
                return mod.funcs[cand]
            target = mod.imports.get(parts[0])
            return self.find_func(target) if target else None
        # imported symbol / module then attribute
        target = mod.imports.get(parts[0])
        if target:
            rest = ".".join(parts[1:])
            found = self.find_func(f"{target}.{rest}")
            if found:
                return found
            # `from .mod import Class` -> Class.method
            return self._method(target.split(".")[-1], parts[1])
        # same-module class call: Class.method
        if len(parts) == 2:
            return self._method(parts[0], parts[1])
        return None


# --------------------------------------------------------------------------
# Entry-point extraction
# --------------------------------------------------------------------------

def extract_routes(repo: Repo) -> list[dict]:
    routes = []
    for mod in repo.modules.values():
        for func in mod.funcs.values():
            for deco in func.decorators:
                m = ROUTE_DECORATOR.match(deco.split("(")[0].strip())
                if not m:
                    continue
                method = m.group("method")
                path = ""
                pm = re.search(r"\(\s*[\"']([^\"']*)[\"']", deco)
                if pm:
                    path = pm.group(1)
                if method == "api_route":
                    mm = re.search(r"methods\s*=\s*\[([^\]]*)\]", deco)
                    method = (mm.group(1).split(",")[0].strip(" '\"") if mm else "GET").upper()
                elif method == "websocket":
                    method = "WS"
                else:
                    method = method.upper()
                params = []
                for p in func.params:
                    ann = p["annotation"]
                    kind = "body"
                    if "UploadFile" in ann:
                        kind = "file-upload"
                    elif "Request" in ann or "Response" in ann:
                        kind = "framework-object"
                    elif p["default"].startswith("Query"):
                        kind = "query"
                    elif p["default"].startswith("Path"):
                        kind = "path"
                    elif p["default"].startswith("Form") or p["default"].startswith("File"):
                        kind = "form"
                    elif p["default"].startswith("Depends"):
                        kind = "dependency"
                    elif ann:
                        kind = "body-model" if ann[:1].isupper() else "body-scalar"
                    if f"{{{p['name']}" in path:
                        kind = "path"
                    params.append({**p, "kind": kind})
                routes.append({
                    "method": method,
                    "path": path,
                    "handler": {"qual": func.qual, "file": func.file, "line": func.lineno},
                    "params": params,
                    "decorator": deco,
                })
    # static mounts
    for mod in repo.modules.values():
        for i, line in enumerate(mod.source.splitlines(), 1):
            if ".mount(" in line and re.search(r"^(from\s+(fastapi|starlette)\.staticfiles\s+import|"
                                                 r"from\s+starlette\.staticfiles\s+import)", mod.source, re.M):
                pm = re.search(r"mount\(\s*[\"']([^\"']*)[\"']", line)
                if pm:
                    base = pm.group(1).rstrip("/")
                    routes.append({
                        "method": "GET",
                        "path": (base + "/*") if base else "/*",
                        "handler": {"qual": f"{mod.qual}.<static-mount>", "file": mod.rel, "line": i},
                        "params": [],
                        "decorator": "app.mount(StaticFiles)",
                        "kind": "static",
                        "note": "статические файлы SPA, отдаваемые без аутентификации",
                    })

    # FastAPI's built-in documentation endpoints: enabled unless explicitly disabled
    for mod in repo.modules.values():
        src = mod.source
        if not re.search(r"^(from\s+fastapi\s+import|import\s+fastapi)", src, re.M):
            continue
        if not re.search(r"^(app|application)\s*=\s*FastAPI\(", src, re.M):
            continue
        for m in re.finditer(r"^(app|application)\s*=\s*FastAPI\(([^)]*)\)", src, re.S | re.M):
            args = m.group(2)
            line = src[: m.start()].count("\n") + 1
            for url_field, path, title in (
                ("docs_url", "/docs", "Swagger UI (интерактивная документация API)"),
                ("redoc_url", "/redoc", "ReDoc (документация API)"),
                ("openapi_url", "/openapi.json", "машиночитаемое описание API"),
            ):
                if re.search(url_field + r"\s*=\s*None", args):
                    continue
                routes.append({
                    "method": "GET", "path": path,
                    "handler": {"qual": f"{mod.qual}.<fastapi-{url_field.split('_')[0]}>",
                                "file": mod.rel, "line": line},
                    "params": [], "decorator": f"FastAPI({url_field}=default)", "kind": "docs",
                    "note": title,
                })
    # the same docs endpoint may be declared once per source file: keep one
    seen, unique = set(), []
    for r in routes:
        key = (r.get("kind", "http"), r["method"], r["path"])
        if r.get("kind") in ("docs", "static") and key in seen:
            continue
        seen.add(key)
        unique.append(r)
    return unique


# --------------------------------------------------------------------------
# Reachability + signals
# --------------------------------------------------------------------------

def reachable(repo: Repo, start: FuncInfo, max_depth: int = 8, max_nodes: int = 400):
    seen: dict[str, FuncInfo] = {start.qual: start}
    depth_of = {start.qual: 0}
    order = [start.qual]
    q = deque([start])
    unresolved = 0
    while q:
        cur = q.popleft()
        d = depth_of[cur.qual]
        if d >= max_depth or len(seen) >= max_nodes:
            continue
        for callee, _line in cur.calls:
            tgt = repo.resolve_call(cur, callee)
            if tgt is None:
                mod = repo.by_file.get(cur.file)
                if not repo.is_external(mod, callee) and not callee.startswith(
                        ("json.", "len", "str.", "print", "isinstance", "list.", "dict.", "set.", "range.")):
                    unresolved += 1
                continue
            if tgt.qual not in seen:
                seen[tgt.qual] = tgt
                depth_of[tgt.qual] = d + 1
                order.append(tgt.qual)
                q.append(tgt)
    return [(seen[q_], depth_of[q_]) for q_ in order], unresolved


def scan_signals(func: FuncInfo):
    validations, sinks = [], []
    for line_no, text in func.line_texts():
        stripped = text.strip()
        if not stripped or stripped.startswith("#"):
            continue
        for kind, pattern, weight in VALIDATION_PATTERNS:
            if re.search(pattern, text):
                validations.append({"kind": kind, "weight": weight, "file": func.file,
                                    "line": line_no, "text": stripped[:200], "where": func.qual})
        for kind, pattern in SINK_PATTERNS:
            if re.search(pattern, text):
                sinks.append({"kind": kind, "file": func.file, "line": line_no,
                              "text": stripped[:200], "where": func.qual})
    return validations, sinks


def _is_enum(repo: "Repo", name: str) -> bool:
    for mod in repo.modules.values():
        info = mod.classes.get(name)
        if info and any(b.split(".")[-1] in ("Enum", "StrEnum", "IntEnum") for b in info.get("bases", [])):
            return True
    return False


def classify(paths: list[tuple[FuncInfo, int]], handler: FuncInfo):
    boundary_v, depth_v, sinks = [], [], []
    for f, _depth in paths:
        v, s = scan_signals(f)
        (boundary_v if f is handler else depth_v).extend(v)
        sinks.extend(s)
    strong_boundary = [v for v in boundary_v if v["weight"] == 2]
    strong_depth = [v for v in depth_v if v["weight"] == 2]
    weak = [v for v in boundary_v + depth_v if v["weight"] == 1]
    parse_only = [v for v in boundary_v + depth_v if v["weight"] == 0]
    if strong_boundary:
        status = "validated-boundary"
    elif strong_depth:
        status = "validated-in-depth"
    elif weak:
        status = "partial"
    elif parse_only:
        status = "parse-only"
    else:
        status = "unvalidated"
    return status, boundary_v + depth_v, sinks


# --------------------------------------------------------------------------
# Report
# --------------------------------------------------------------------------

def git_commit(root: Path) -> str:
    try:
        return subprocess.run(["git", "-C", str(root), "rev-parse", "HEAD"],
                              capture_output=True, text=True, timeout=10).stdout.strip()
    except Exception:
        return ""


CODE_EXT = {".py", ".cs", ".java", ".kt", ".go", ".rs", ".c", ".cc", ".cpp", ".h", ".hpp",
            ".ts", ".tsx", ".js", ".jsx", ".vue", ".php", ".rb", ".erl", ".hrl", ".lua",
            ".hs", ".ex", ".exs", ".scala", ".swift"}


def repo_stats(root: Path) -> dict:
    exts, total, files = Counter(), 0, 0
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in EXCLUDE_DIRS and not d.startswith(".")]
        for fn in filenames:
            p = Path(dirpath) / fn
            try:
                if p.stat().st_size > 2_000_000:
                    continue
                n = sum(1 for _ in p.open("rb"))
            except Exception:
                continue
            total += n
            files += 1
            exts[p.suffix.lower() or "(none)"] += n
    by_lang = [(e, n) for e, n in exts.most_common(40) if e in CODE_EXT]
    return {"files": files, "loc": total, "by_ext": exts.most_common(12),
            "by_lang": by_lang[:12], "code_loc": sum(n for _, n in by_lang)}


CSS = """
:root{color-scheme:light;--fg:#111827;--muted:#6b7280;--line:#e5e7eb;--bg:#ffffff;--soft:#f9fafb;
--accent:#1d4ed8;--warn:#b45309;--bad:#b91c1c;--ok:#047857}
*{box-sizing:border-box}
body{margin:0;padding:32px 40px 80px;font:15px/1.55 -apple-system,"Segoe UI",Roboto,Arial,sans-serif;color:var(--fg);background:var(--bg)}
h1{font-size:26px;margin:0 0 4px}h2{font-size:20px;margin:36px 0 12px;padding-bottom:6px;border-bottom:2px solid var(--line)}
h3{font-size:16px;margin:0 0 8px}
p,li{max-width:110ch}
.meta{color:var(--muted);font-size:13px;margin-bottom:24px}
table{border-collapse:collapse;width:100%;margin:12px 0 24px;font-size:13px}
th,td{border:1px solid var(--line);padding:6px 8px;text-align:left;vertical-align:top;overflow-wrap:anywhere}
th{background:var(--soft);font-weight:600}
code{font:12px/1.45 ui-monospace,SFMono-Regular,Consolas,monospace;background:var(--soft);padding:1px 4px;border-radius:3px}
pre{font:12px/1.5 ui-monospace,SFMono-Regular,Consolas,monospace;background:var(--soft);border:1px solid var(--line);
border-radius:6px;padding:10px 12px;overflow:auto;margin:8px 0 16px;white-space:pre-wrap;overflow-wrap:anywhere}
.card{border:1px solid var(--line);border-radius:8px;padding:16px 18px;margin:0 0 18px}
.tag{display:inline-block;font-size:11px;padding:1px 7px;border-radius:10px;border:1px solid var(--line);background:var(--soft);color:var(--muted);margin-right:6px}
.st-validated-boundary{color:var(--ok);border-color:var(--ok)}
.st-validated-in-depth{color:var(--ok);border-color:var(--ok)}
.st-partial{color:var(--warn);border-color:var(--warn)}
.st-parse-only{color:var(--warn);border-color:var(--warn)}
.st-unvalidated{color:var(--bad);border-color:var(--bad)}
.k{color:var(--muted)}
ul{margin:6px 0 12px 22px;padding:0}
.warn{background:#fff7ed;border-left:3px solid var(--warn);padding:10px 14px;margin:12px 0}
"""


def esc(x) -> str:
    return html.escape(str(x), quote=True)


def render_html(ctx: dict) -> str:
    st = ctx["stats"]
    lang_rows = "".join(
        f"<tr><td><code>{esc(e)}</code></td><td>{n:,}</td></tr>" for e, n in st["by_lang"]
    )
    status_rows = "".join(
        f"<tr><td>{esc(VALIDATION_LABELS.get(k, k))}</td><td>{v}</td></tr>"
        for k, v in st["by_status"].items()
    )
    conclusions = "".join(f"<li>{esc(c)}</li>" for c in ctx["conclusions"])

    parts = [f"""<!doctype html><html lang="ru"><head><meta charset="utf-8">
<meta name="color-scheme" content="light"><title>Поверхность атаки — {esc(ctx['product']['name'])}</title>
<style>{CSS}</style></head><body>
<h1>Поверхность атаки: {esc(ctx['product']['name'])}</h1>
<div class="meta">Отчёт сформирован {esc(ctx['generated_at'])} · коммит <code>{esc(ctx['product']['commit'][:12] or 'n/a')}</code>
· инструмент: surface_scan {esc(ctx['tool_version'])} · режим: статический анализ исходного кода, без сборки и без запуска</div>

<h2>1. Методика и модель нарушителя</h2>
<p>Поверхность атаки определена как множество сетевых интерфейсов, доступных нарушителю, и участков кода,
которые эти интерфейсы непосредственно задействуют для приёма и обработки внешних данных.</p>
<ul>
<li><b>Классы нарушителей.</b> Внешний нарушитель, действующий без аутентификации (приоритетный класс), и внутренний
нарушитель без прав администратора. Интерфейсы, доступные только администратору, в поверхность атаки не включаются.</li>
<li><b>Что считается интерфейсом.</b> Маршруты HTTP/WebSocket, зарегистрированные в приложении, статические точки
монтирования и встроенные точки документирования API. Обработчик маршрута — точка входа; далее по графу вызовов
определяется достижимый код проекта.</li>
<li><b>Валидация</b> фиксируется как свойство пути: схема/типизированная модель, ограничения полей, allowlist,
регулярные выражения, явные проверки размера и типа. Разбор без проверки (например, <code>json.loads</code> с возвратом 400)
отмечается отдельно как структурная проверка.</li>
</ul>
<p class="meta">Границы применимости: анализ статический, по исходному коду; сборка не выполняется; taint-анализ
(источник → сток с учётом санитайзеров) в этой версии не проводится; граф вызовов разрешается эвристически
(см. раздел «Ограничения»).</p>

<h2>2. Сводка</h2>
<table><tr><th>Показатель</th><th>Значение</th></tr>
<tr><td>Файлов в scope / строк всего</td><td>{st['files']} / {st['loc']:,}</td></tr>
<tr><td>Строк кода (только языки программирования)</td><td>{st['code_loc']:,}</td></tr>
<tr><td>Интерфейсов обнаружено (API + статика + документация)</td><td>{st['routes']}</td></tr>
<tr><td>— из них маршрутов API</td><td>{st['api_routes']}</td></tr>
<tr><td>Из них без аутентификации</td><td>{st['no_auth']}</td></tr>
<tr><td>Принимают файлы (upload)</td><td>{st['uploads']}</td></tr>
<tr><td>Уникальных функций проекта в достижимых путях</td><td>{st['reachable_unique']}</td></tr>
<tr><td>Вхождений функций в достижимые множества (сумма по интерфейсам)</td><td>{st['reachable_funcs']}</td></tr>
<tr><td>Потенциально опасных операций на путях</td><td>{st['sinks']}</td></tr>
</table>
<p>Распределение по методам: {esc(', '.join(f'{k}: {v}' for k, v in st['by_method'].items()))}.</p>
<h3>Статус валидации</h3>
<table><tr><th>Статус</th><th>Интерфейсов</th></tr>{status_rows}</table>
<h3>Состав исходных текстов</h3>
<table><tr><th>Расширение</th><th>Строк</th></tr>{lang_rows}</table>

<h2>3. Выводы по поверхности атаки</h2>
<ul>{conclusions}</ul>
"""]

    # registry table
    parts.append("<h2>4. Реестр интерфейсов</h2><table><tr><th>#</th><th>Метод</th><th>Путь</th>"
                 "<th>Обработчик</th><th>Входные данные</th><th>Нарушитель</th><th>Валидация</th>"
                 "<th>Достижимо функций</th><th>Стоки</th></tr>")
    for i, itf in enumerate(ctx["interfaces"], 1):
        params = ", ".join(f"{p['name']}:{p['kind']}" for p in itf["params"]) or "—"
        parts.append(
            f"<tr><td>{i}</td><td><code>{esc(itf['method'])}</code></td><td><code>{esc(itf['path'])}</code></td>"
            f"<td><code>{esc(itf['handler']['file'])}:{itf['handler']['line']}</code></td>"
            f"<td>{esc(params)}</td><td>{esc(itf['auth']['label'])}</td>"
            f"<td><span class='tag st-{itf['validation']['status']}'>{esc(itf['validation']['label'])}</span></td>"
            f"<td>{len(itf['reachable']['functions'])}</td><td>{len(itf['sinks'])}</td></tr>")
    parts.append("</table>")

    # cards
    parts.append("<h2>5. Карточки интерфейсов</h2>")
    for i, itf in enumerate(ctx["interfaces"], 1):
        parts.append(f"<div class='card'><h3>{i}. <code>{esc(itf['method'])} {esc(itf['path'])}</code></h3>")
        parts.append(f"<div><span class='k'>Обработчик:</span> <code>{esc(itf['handler']['qual'])}</code> "
                     f"({esc(itf['handler']['file'])}:{itf['handler']['line']})</div>")
        if itf.get("note"):
            parts.append(f"<div><span class='k'>Примечание:</span> {esc(itf['note'])}</div>")
        parts.append(f"<div><span class='k'>Класс нарушителя:</span> {esc(itf['auth']['label'])} — {esc(itf['auth']['reason'])}</div>")
        if itf.get("admin_like") and itf["auth"]["class"] == "unauthenticated":
            parts.append("<div><span class='k'>Административный по назначению:</span> да — но технических ограничений "
                         "доступа нет, поэтому интерфейс включён в поверхность атаки.</div>")
        parts.append(f"<div><span class='k'>Статус валидации:</span> "
                     f"<span class='tag st-{itf['validation']['status']}'>{esc(itf['validation']['label'])}</span></div>")
        if itf["params"]:
            parts.append("<div style='margin-top:10px'><span class='k'>Входные параметры:</span></div>"
                         "<table><tr><th>Имя</th><th>Тип</th><th>Роль</th><th>Объявление</th></tr>")
            for p in itf["params"]:
                parts.append(f"<tr><td><code>{esc(p['name'])}</code></td><td>{esc(p['annotation'] or '—')}</td>"
                             f"<td>{esc(p['kind'])}</td><td>{esc(p['default'] or '—')}</td></tr>")
            parts.append("</table>")
        if itf["validation"]["evidence"]:
            parts.append("<div style='margin-top:10px'><span class='k'>Признаки валидации:</span></div><ul>")
            for v in itf["validation"]["evidence"][:12]:
                parts.append(f"<li><code>{esc(v['file'])}:{v['line']}</code> [{esc(v['kind'])}] {esc(v['text'])}</li>")
            parts.append("</ul>")
        if itf["sinks"]:
            parts.append("<div style='margin-top:10px'><span class='k'>Потенциально опасные операции на пути:</span></div><ul>")
            for s_ in itf["sinks"][:12]:
                parts.append(f"<li><code>{esc(s_['file'])}:{s_['line']}</code> [{esc(s_['kind'])}] {esc(s_['text'])}</li>")
            parts.append("</ul>")
        chain = itf["reachable"]["functions"]
        parts.append(f"<div style='margin-top:10px'><span class='k'>Достижимый код проекта "
                     f"({len(chain)} функций, неразрешённых внутренних вызовов: {itf['reachable']['unresolved']}):</span></div>")
        if chain:
            parts.append("<table><tr><th>Глубина</th><th>Функция</th><th>Место</th></tr>")
            for f in chain[:40]:
                indent = "&nbsp;" * (2 * int(f.get("depth", 0)))
                short = ".".join(f["qual"].split(".")[-2:])
                parts.append(f"<tr><td>{f.get('depth', 0)}</td><td>{indent}<code title='{esc(f['qual'])}'>{esc(short)}</code></td>"
                             f"<td><code>{esc(f['file'])}:{f['line']}</code></td></tr>")
            if len(chain) > 40:
                parts.append(f"<tr><td colspan='3'>… ещё {len(chain) - 40} функций</td></tr>")
            parts.append("</table>")
        if itf.get("handler_source"):
            src_lines = itf["handler_source"].splitlines()[:30]
            parts.append("<details style='margin-top:10px'><summary><span class='k'>Исходный текст обработчика "
                         f"(первые {len(src_lines)} строк)</span></summary>"
                         f"<pre>{esc(chr(10).join(src_lines))}</pre></details>")
        parts.append("</div>")

    parts.append("<h2>6. Ограничения и что не покрыто</h2><ul>")
    for lim in ctx["limitations"]:
        parts.append(f"<li>{esc(lim)}</li>")
    parts.append("</ul>")

    parts.append("<h2>7. Воспроизводимость</h2>")
    parts.append(f"<p>Источник: <code>{esc(ctx['product']['root'])}</code>, коммит "
                 f"<code>{esc(ctx['product']['commit'] or 'n/a')}</code>. JSON-модель отчёта: "
                 f"<code>surface.json</code> рядом с этим файлом.</p>"
                 f"<pre>{esc(ctx['command'])}</pre>")
    parts.append("</body></html>")
    return "\n".join(parts)


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------

VALIDATION_LABELS = {
    "n/a": "не применимо (нет кода проекта)",
    "validated-boundary": "валидация на границе",
    "validated-in-depth": "валидация в глубине",
    "partial": "частичная",
    "parse-only": "только разбор",
    "unvalidated": "не обнаружена",
}


def build(root: Path, name: str, outdir: Path, max_depth: int, commit: str = "") -> dict:
    repo = Repo(root)
    repo.load()
    routes = extract_routes(repo)

    auth_hits = []
    for mod in repo.modules.values():
        for i, line in enumerate(mod.source.splitlines(), 1):
            for kind, pattern in AUTH_PATTERNS:
                if re.search(pattern, line):
                    auth_hits.append({"kind": kind, "file": mod.rel, "line": i, "text": line.strip()[:160]})
                    break
    project_auth = bool(auth_hits)

    interfaces = []
    for r in routes:
        handler = repo.find_func(r["handler"]["qual"])
        if handler is None and r.get("kind") not in ("static", "docs"):
            continue
        if handler is None:
            # static mount / built-in docs: no project code behind the interface
            paths, unresolved = [], 0
            status, evidence, sinks = "n/a", [], []
        else:
            paths, unresolved = reachable(repo, handler, max_depth=max_depth)
            status, evidence, sinks = classify(paths, handler)

        model_ev = []
        if handler is not None:
            for prm in handler.params:
                ann = (prm["annotation"] or "").split("|")[0].strip()
                if ann[:1].isupper() and repo.is_model_class(ann):
                    model_ev.append({"kind": "schema", "weight": 2, "file": handler.file,
                                     "line": handler.lineno, "where": handler.qual,
                                     "text": f"{prm['name']}: {ann} — типизированная модель (pydantic BaseModel)"})
        typed_ev = []
        if handler is not None:
            for prm in handler.params:
                ann = (prm["annotation"] or "").split("|")[0].strip()
                if ann in ("int", "float", "bool", "UUID", "datetime", "Decimal") or (
                        ann[:1].isupper() and _is_enum(repo, ann)):
                    typed_ev.append({"kind": "typed-param", "weight": 0, "file": handler.file,
                                     "line": handler.lineno, "where": handler.qual,
                                     "text": f"{prm['name']}: {ann} — типизированный параметр (проверка типа/диапазона)"})
        if model_ev:
            status = "validated-boundary"
            evidence = model_ev + [e for e in evidence if e["kind"] != "schema"]
        if typed_ev and status == "unvalidated":
            evidence = evidence + typed_ev
            status = "parse-only"
        # identical lines are reached through several call paths: keep one
        deduped, seen_ev = [], set()
        for e in evidence:
            key = (e["file"], e["line"], e["kind"], e["text"][:60])
            if key in seen_ev:
                continue
            seen_ev.add(key)
            deduped.append(e)
        evidence = deduped

        if not project_auth:
            auth = {"class": "unauthenticated", "label": "внешний, без аутентификации",
                    "reason": "в проекте не обнаружено ни одного механизма аутентификации/авторизации"}
        else:
            auth = {"class": "unknown", "label": "требует уточнения",
                    "reason": "в проекте есть признаки аутентификации, но привязка к маршруту статически не доказана"}

        interfaces.append({
            "id": f"{r['method']} {r['path']}",
            "method": r["method"],
            "path": r["path"],
            "kind": r.get("kind", "http"),
            "note": r.get("note", ""),
            "handler": r["handler"],
            "params": r["params"],
            "auth": auth,
            "validation": {"status": status, "label": VALIDATION_LABELS[status], "evidence": evidence},
            "reachable": {"functions": [{"qual": f.qual, "file": f.file, "line": f.lineno, "depth": d}
                                        for f, d in paths],
                          "unresolved": unresolved},
            "handler_source": (handler.source if handler else ""),
            "admin_like": bool(re.search(r"/(db|settings|admin|config)(/|$)", r["path"])),
            "sinks": sinks,
        })

    interfaces.sort(key=lambda x: (x["path"], x["method"]))

    http = [i for i in interfaces if i["kind"] == "http"]
    unauth = [i for i in interfaces if i["auth"]["class"] == "unauthenticated"]
    uploads = [i for i in interfaces if any(p["kind"] == "file-upload" for p in i["params"])]
    unvalidated = [i for i in interfaces if i["validation"]["status"] == "unvalidated"]
    admin_like = [i for i in interfaces if i.get("admin_like")]
    docs = [i for i in interfaces if i["kind"] == "docs"]
    static = [i for i in interfaces if i["kind"] == "static"]
    sink_kinds = Counter(s_["kind"] for i in interfaces for s_ in i["sinks"])

    conclusions = []
    if unauth and len(unauth) == len(interfaces):
        conclusions.append(
            f"Все {len(interfaces)} обнаруженных интерфейсов доступны внешнему нарушителю без аутентификации: "
            "механизмы аутентификации и авторизации в исходном коде отсутствуют (что совпадает с разделом "
            "Security Notes документации проекта)."
        )
    elif unauth:
        conclusions.append(f"Без аутентификации доступно {len(unauth)} из {len(interfaces)} интерфейсов.")
    if uploads:
        conclusions.append(
            f"{len(uploads)} интерфейсов принимают файл: " + ", ".join(f"{i['method']} {i['path']}" for i in uploads)
            + ". Границей валидации для них являются проверка размера и разбор формата; схемная валидация "
              "содержимого файла по коду не подтверждается."
        )
    if admin_like:
        conclusions.append(
            f"{len(admin_like)} интерфейсов являются административными по назначению (/db/*, /settings): "
            + ", ".join(f"{i['method']} {i['path']}" for i in admin_like)
            + ". Технических ограничений доступа нет, поэтому они включены в поверхность атаки как доступные "
              "внешнему нарушителю; при появлении аутентификации их следует переклассифицировать."
        )
    if docs:
        conclusions.append(
            "Встроенные точки документирования API (" + ", ".join(sorted({i["path"] for i in docs}))
            + ") доступны без аутентификации и раскрывают полный контракт API, включая схемы входных данных."
        )
    if static:
        conclusions.append(
            "Статическая раздача SPA смонтирована на "
            + ", ".join(sorted({i["path"] for i in static}))
            + " и отдаёт файлы без аутентификации; эта точка входа не обрабатывает пользовательские данные, "
              "но расширяет доступную нарушителю файловую поверхность."
        )
    if unvalidated:
        conclusions.append(
            f"Признаков валидации не обнаружено на {len(unvalidated)} интерфейсах, в том числе: "
            + ", ".join(f"{i['method']} {i['path']}" for i in unvalidated[:8])
            + (", …" if len(unvalidated) > 8 else "") + "."
        )
    if sink_kinds:
        conclusions.append(
            "На достижимых путях встречаются потенциально опасные операции: "
            + ", ".join(f"{k} ({v})" for k, v in sink_kinds.most_common())
            + ". Их привязка к внешним данным требует отдельной проверки (taint-анализ в этой версии не выполняется)."
        )

    stats = {
        "routes": len(interfaces),
        "api_routes": sum(1 for i in interfaces if i["kind"] == "http"),
        "no_auth": sum(1 for i in interfaces if i["auth"]["class"] == "unauthenticated"),
        "uploads": sum(1 for i in interfaces if any(p["kind"] == "file-upload" for p in i["params"])),
        "by_status": dict(Counter(i["validation"]["status"] for i in interfaces)),
        "by_method": dict(Counter(i["method"] for i in interfaces)),
        "reachable_funcs": sum(len(i["reachable"]["functions"]) for i in interfaces),
        "reachable_unique": len({f["qual"] for i in interfaces for f in i["reachable"]["functions"]}),
        "sinks": sum(len(i["sinks"]) for i in interfaces),
        **repo_stats(root),
    }

    limitations = [
        f"Анализ статический, без сборки: {repo.parse_fail} файл(ов) не разобраны."
        if repo.parse_fail else "Анализ статический, без сборки; все файлы Python разобраны.",
        "Taint-анализ (источник → сток с учётом санитайзеров) не проводится; статус валидации — результат "
        "эвристического поиска признаков, а не доказательство отсутствия проверки.",
        "Граф вызовов разрешается эвристически: динамическая диспетчеризация, декораторы, фабрики и вызовы через "
        "внешние библиотеки могут давать пропуски рёбер (счётчик неразрешённых вызовов приведён в карточках).",
        "Анализируется только серверный код Python; клиентская часть (Vue/TypeScript) в поверхность атаки как "
        "источник обработки внешних данных не включалась.",
        "Рантайм-проверка (фактические слушающие порты, OpenAPI, аутентификация на стенде) в этой версии не выполнялась.",
        "Классификация по классам нарушителей выполнена по коду: при отсутствии аутентификации все интерфейсы "
        "отнесены к внешним, включая административные.",
    ]

    ctx = {
        "schema": "attack-surface/1",
        "tool_version": "0.1.0",
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
        "product": {"name": name, "root": str(root), "commit": commit or git_commit(root)},
        "scope": {"languages": ["Python"], "framework": "FastAPI/Starlette", "build": "not used"},
        "stats": stats,
        "interfaces": interfaces,
        "auth_signals": auth_hits[:20],
        "parse_errors": repo.errors,
        "limitations": limitations,
        "conclusions": conclusions,
        "command": f"python3 surface_scan.py --root {root} --name \"{name}\" --out {outdir} --max-depth {max_depth}",
    }

    outdir.mkdir(parents=True, exist_ok=True)
    (outdir / "surface.json").write_text(json.dumps(ctx, ensure_ascii=False, indent=2), encoding="utf-8")
    (outdir / "attack-surface.html").write_text(render_html(ctx), encoding="utf-8")
    return ctx


def main() -> int:
    ap = argparse.ArgumentParser(description="MVP attack-surface extractor (Python/FastAPI)")
    ap.add_argument("--root", required=True, help="source tree root")
    ap.add_argument("--name", default=None, help="product name for the report")
    ap.add_argument("--out", default="surface-out", help="output directory")
    ap.add_argument("--max-depth", type=int, default=8, help="call-graph depth limit")
    args = ap.parse_args()

    root = Path(args.root).resolve()
    if not root.is_dir():
        print(f"not a directory: {root}", file=sys.stderr)
        return 2
    name = args.name or root.name
    ctx = build(root, name, Path(args.out), args.max_depth)
    st = ctx["stats"]
    print(f"interfaces: {st['routes']}  reachable funcs: {st['reachable_funcs']}  sinks: {st['sinks']}")
    print(f"validation: {st['by_status']}")
    print(f"report: {Path(args.out).resolve() / 'attack-surface.html'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
