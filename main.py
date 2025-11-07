import argparse
import json
import asyncio
import hashlib
import os
import pathlib
import re
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

import javalang
import lizard
from langchain.chat_models import init_chat_model
from langchain.schema import HumanMessage, SystemMessage
from tenacity import retry, stop_after_attempt, wait_exponential
from tqdm import tqdm

from prompts import MAP_PROMPT, REDUCE_PROMPT

SUPPORTED_EXTENSIONS = {".java", ".kt", ".js", ".ts", ".py", ".cs"}
SKIP_DIR_FRAGMENTS = {"node_modules", "target", "build", ".git", "dist", "out", "venv", ".venv"}


@dataclass
class MethodInfo:
    file: str
    package: Optional[str]
    clazz: Optional[str]
    signature: str
    loc: int
    cyclomatic_complexity: Optional[int]
    code_snippet: str
    llm_summary: Optional[Dict[str, Any]] = None


@dataclass
class ClassInfo:
    file: str
    package: Optional[str]
    clazz: str
    methods: List[MethodInfo]


def read_text(path: str) -> str:
    with open(path, "r", encoding="utf-8", errors="ignore") as f:
        return f.read()


def list_source_files(root: str) -> List[str]:
    files: List[str] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIR_FRAGMENTS]
        for filename in filenames:
            full_path = os.path.join(dirpath, filename)
            if pathlib.Path(full_path).suffix.lower() in SUPPORTED_EXTENSIONS:
                files.append(full_path)
    return files


def infer_java_package(source: str) -> Optional[str]:
    match = re.search(r"^\s*package\s+([a-zA-Z0-9_.]+)\s*;", source, re.MULTILINE)
    return match.group(1) if match else None


def extract_method_snippet(source: str, start_line: int) -> Tuple[str, int]:
    lines = source.splitlines()
    index = max(0, start_line - 1)

    while index < len(lines) and "{" not in lines[index]:
        index += 1
    if index >= len(lines):
        snippet_lines = lines[start_line - 1 : min(len(lines), start_line + 50)]
        return "\n".join(snippet_lines), len(snippet_lines)

    brace_balance = 0
    snippet: List[str] = []
    for cursor in range(index, len(lines)):
        brace_balance += lines[cursor].count("{")
        brace_balance -= lines[cursor].count("}")
        snippet.append(lines[cursor])
        if brace_balance <= 0 and cursor > index:
            break

    return "\n".join(snippet), len(snippet)


def get_java_classes_methods(source: str, file_path: str) -> List[ClassInfo]:
    classes: List[ClassInfo] = []
    try:
        tree = javalang.parse.parse(source)
    except (javalang.parser.JavaSyntaxError, IndexError, TypeError):
        return classes

    package = infer_java_package(source)

    for _, node in tree.filter(javalang.tree.TypeDeclaration):
        if not isinstance(node, javalang.tree.ClassDeclaration):
            continue

        methods: List[MethodInfo] = []
        for method in node.methods:
            params: List[str] = []
            for param in method.parameters:
                if getattr(param, "type", None) is None:
                    continue
                type_name = getattr(param.type, "name", "Object")
                if getattr(param.type, "arguments", None):
                    type_name += "<" + ",".join(str(arg) for arg in param.type.arguments) + ">"
                array_dims = getattr(param.type, "dimensions", None) or []
                type_name += "[]" * len(array_dims)
                params.append(f"{type_name} {param.name}")
            return_type = getattr(method.return_type, "name", "void") if getattr(method, "return_type", None) else "void"
            signature = f"{return_type} {method.name}({', '.join(params)})"
            start_line = method.position.line if getattr(method, "position", None) else 1
            snippet, loc = extract_method_snippet(source, start_line)
            methods.append(
                MethodInfo(
                    file=file_path,
                    package=package,
                    clazz=node.name,
                    signature=signature,
                    loc=loc,
                    cyclomatic_complexity=None,
                    code_snippet=snippet,
                )
            )
        classes.append(ClassInfo(file=file_path, package=package, clazz=node.name, methods=methods))

    return classes


def compute_complexity(file_path: str) -> Dict[str, int]:
    try:
        analysis = lizard.analyze_file(file_path)
    except Exception:
        return {}
    complexities: Dict[str, int] = {}
    for function in analysis.function_list:
        key = f"{function.name}@{function.start_line}"
        complexities[key] = function.cyclomatic_complexity
    return complexities


def attach_complexity_to_methods(methods: List[MethodInfo], file_path: str) -> None:
    complexity_map = compute_complexity(file_path)
    for method in methods:
        method_name = method.signature.split("(")[0].split()[-1]
        relevant = [value for key, value in complexity_map.items() if key.startswith(f"{method_name}@")]
        method.cyclomatic_complexity = min(relevant) if relevant else None


def parse_pom_tech_stack(pom_path: str) -> Dict[str, Any]:
    if not os.path.exists(pom_path):
        return {}
    try:
        tree = ET.parse(pom_path)
        root = tree.getroot()
        namespaces = {"m": "http://maven.apache.org/POM/4.0.0"}
        dependencies = [
            dep.findtext("m:artifactId", namespaces=namespaces)
            for dep in root.findall(".//m:dependencies/m:dependency", namespaces)
        ]
        dependencies = [dep for dep in dependencies if dep]
        frameworks = [dep for dep in dependencies if any(token in dep for token in ("spring-boot", "thymeleaf", "mysql"))]
        return {
            "build": "maven",
            "dependencies": dependencies,
            "frameworks_detected": frameworks,
        }
    except ET.ParseError:
        return {}


def init_model(provider: str, model: str):
    if provider == "openai":
        return init_chat_model(model=model, model_provider="openai", temperature=0.0)
    if provider == "azure":
        return init_chat_model(model=model, model_provider="azure", temperature=0.0)
    if provider == "bedrock":
        return init_chat_model(model=model, model_provider="amazon-bedrock", temperature=0.0)
    raise ValueError("Unsupported provider. Choose from openai, azure, bedrock.")


@retry(wait=wait_exponential(min=1, max=30), stop=stop_after_attempt(5))
def call_map(model, code_snippet: str) -> Dict[str, Any]:
    messages = [SystemMessage(content=MAP_PROMPT), HumanMessage(content=code_snippet[:12000])]
    response = model.invoke(messages)
    content = getattr(response, "content", "").strip()
    try:
        return json.loads(content)
    except json.JSONDecodeError:
        raise ValueError("LLM returned invalid JSON for map step")


@retry(wait=wait_exponential(min=1, max=30), stop=stop_after_attempt(5))
def call_reduce(model, fragments: List[Dict[str, Any]]) -> Dict[str, Any]:
    payload = json.dumps(fragments)[:24000]
    messages = [SystemMessage(content=REDUCE_PROMPT), HumanMessage(content=payload)]
    response = model.invoke(messages)
    content = getattr(response, "content", "").strip()
    try:
        return json.loads(content)
    except json.JSONDecodeError:
        raise ValueError("LLM returned invalid JSON for reduce step")


# Async variant of map call for concurrency
@retry(wait=wait_exponential(min=1, max=30), stop=stop_after_attempt(5))
async def call_map_async(model, code_snippet: str) -> Dict[str, Any]:
    messages = [SystemMessage(content=MAP_PROMPT), HumanMessage(content=code_snippet[:12000])]
    response = await model.ainvoke(messages)
    content = getattr(response, "content", "").strip()
    try:
        return json.loads(content)
    except json.JSONDecodeError:
        raise ValueError("LLM returned invalid JSON for map step")


def _load_cache(cache_path: Optional[str]) -> Dict[str, Any]:
    if not cache_path:
        return {}
    try:
        if os.path.exists(cache_path):
            with open(cache_path, "r", encoding="utf-8") as fh:
                return json.load(fh)
    except Exception:
        return {}
    return {}


def _save_cache(cache_path: Optional[str], cache: Dict[str, Any]) -> None:
    if not cache_path:
        return
    try:
        out_dir = os.path.dirname(cache_path)
        if out_dir:
            os.makedirs(out_dir, exist_ok=True)
        with open(cache_path, "w", encoding="utf-8") as fh:
            json.dump(cache, fh, indent=2, ensure_ascii=False)
    except Exception:
        pass


def _cache_key(model_name: str, code_snippet: str) -> str:
    # Tie cache to model and snippet content
    payload = f"{model_name}\n{code_snippet}"
    return hashlib.sha1(payload.encode("utf-8", errors="ignore")).hexdigest()


def analyze_repo(repo_path: str, provider: str, model_name: str, out_path: str, workers: int = 6, cache_path: Optional[str] = ".llm_cache.json") -> None:
    files = list_source_files(repo_path)
    java_files = [file for file in files if file.endswith(".java")]

    tech_stack = parse_pom_tech_stack(os.path.join(repo_path, "pom.xml"))

    classes: List[ClassInfo] = []
    for java_file in tqdm(java_files, desc="Parsing Java"):
        source = read_text(java_file)
        parsed_classes = get_java_classes_methods(source, java_file)
        for class_info in parsed_classes:
            attach_complexity_to_methods(class_info.methods, java_file)
        classes.extend(parsed_classes)

    model = init_model(provider, model_name)

    # Flatten methods for easier parallelism
    methods: List[MethodInfo] = []
    for class_info in classes:
        methods.extend(class_info.methods)

    cache = _load_cache(cache_path)

    async def _run_map() -> None:
        sem = asyncio.Semaphore(max(1, int(workers)))
        pbar = tqdm(total=len(methods), desc="LLM map (per method)")

        async def _do_one(method: MethodInfo) -> None:
            key = _cache_key(model_name, method.code_snippet)
            cached = cache.get(key)
            if cached is not None:
                method.llm_summary = cached
                pbar.update(1)
                return
            try:
                async with sem:
                    result = await call_map_async(model, method.code_snippet)
                method.llm_summary = result
                cache[key] = result
            except Exception:
                method.llm_summary = {
                    "summary": "",
                    "risks": None,
                    "side_effects": None,
                    "external_calls": None,
                }
            finally:
                pbar.update(1)

        # Schedule tasks and await completion
        tasks = [asyncio.create_task(_do_one(m)) for m in methods]
        for t in asyncio.as_completed(tasks):
            await t
        pbar.close()

    # Run async map phase
    if methods:
        asyncio.run(_run_map())
        _save_cache(cache_path, cache)

    fragments: List[Dict[str, Any]] = []
    for class_info in classes:
        for method in class_info.methods:
            fragments.append(
                {
                    "file": method.file,
                    "class": class_info.clazz,
                    "signature": method.signature,
                    "summary": (method.llm_summary or {}).get("summary", ""),
                    "risks": (method.llm_summary or {}).get("risks"),
                    "external_calls": (method.llm_summary or {}).get("external_calls"),
                }
            )

    try:
        reduced = call_reduce(model, fragments)
    except Exception:
        reduced = {
            "project_overview": "Overview unavailable due to LLM error.",
            "noteworthy_design": [],
            "cross_cutting_concerns": [],
            "possible_gaps": [],
        }

    packages: Dict[str, List[Dict[str, Any]]] = {}
    for class_info in classes:
        package_name = class_info.package or "default"
        packages.setdefault(package_name, [])
        existing = next((entry for entry in packages[package_name] if entry["class"] == class_info.clazz), None)
        if existing is None:
            existing = {"class": class_info.clazz, "methods": []}
            packages[package_name].append(existing)
        for method in class_info.methods:
            existing["methods"].append(
                {
                    "signature": method.signature,
                    "loc": method.loc,
                    "cyclomatic_complexity": method.cyclomatic_complexity,
                    "summary": (method.llm_summary or {}).get("summary"),
                    "risks": (method.llm_summary or {}).get("risks"),
                    "side_effects": (method.llm_summary or {}).get("side_effects"),
                    "external_calls": (method.llm_summary or {}).get("external_calls"),
                }
            )

    report = {
        "repo": os.path.abspath(repo_path),
        "generated_at": datetime.utcnow().isoformat() + "Z",
        "tech_stack": tech_stack,
        "high_level_overview": reduced.get("project_overview", ""),
        "noteworthy_aspects": reduced.get("noteworthy_design", []),
        "cross_cutting_concerns": reduced.get("cross_cutting_concerns", []),
        "possible_gaps": reduced.get("possible_gaps", []),
        "packages": packages,
    }

    out_dir = os.path.dirname(out_path)
    if out_dir:
        os.makedirs(out_dir, exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as handle:
        json.dump(report, handle, indent=2, ensure_ascii=False)

    print(f"✅ Wrote: {out_path}")


def main() -> None:
    parser = argparse.ArgumentParser(description="LLM Codebase Analyzer")
    parser.add_argument("--repo", required=True, help="Path to the repository to analyze")
    parser.add_argument("--provider", choices=["openai", "azure", "bedrock"], default="openai")
    parser.add_argument("--model", dest="model_name", default="gpt-4o-mini", help="LLM model identifier")
    parser.add_argument("--out", default="output/report.json", help="Destination JSON path")
    parser.add_argument("--workers", type=int, default=6, help="Concurrent LLM calls during map phase")
    parser.add_argument("--cache", default=".llm_cache.json", help="Path to JSON cache (set empty to disable)")
    args = parser.parse_args()

    cache_path = args.cache if (args.cache and args.cache.strip()) else None
    analyze_repo(args.repo, args.provider, args.model_name, args.out, workers=args.workers, cache_path=cache_path)


if __name__ == "__main__":
    main()
