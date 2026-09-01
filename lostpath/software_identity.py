"""把系统登记、环境变量和启动项关联到软件台账实体。

名称只用于候选缩小，路径和发布商才是强证据。关联结果始终带理由与置信度，
匹配不到时保持空值，不为界面制造看似完整但错误的关系。
"""
from __future__ import annotations

import os
import re
from collections.abc import Iterable


VENDOR_SUFFIXES = (
    " corporation", " corp", " inc", " ltd", " co", " llc", " gmbh", " networks",
    " technologies", " technology", " software", " systems", " interactive",
    " entertainment", " digital", " media", " studio", " labs", " limited",
)


def normalize_publisher(value: str | None) -> str:
    if not value:
        return ""
    normalized = value.strip().lower()
    for suffix in VENDOR_SUFFIXES:
        if normalized.endswith(suffix):
            normalized = normalized[:-len(suffix)]
            break
    return re.sub(r"[^a-z0-9\u4e00-\u9fff]+", "", normalized)


def normalize_name(value: str | None) -> str:
    """与软件台账实体 ID 使用同一套名称归一化。"""
    normalized = (value or "").lower()
    normalized = re.sub(r"\((user|x64|x86|64-bit|32-bit)\)", " ", normalized)
    normalized = re.sub(r"\d+(\.\d+)+", " ", normalized)
    return re.sub(r"[^a-z0-9\u4e00-\u9fff]+", "", normalized)


def registry_entity_id(name: str | None, publisher: str | None) -> str:
    return f"r:{normalize_publisher(publisher) or 'unknown'}:{normalize_name(name)}"


def normalize_path(value: object) -> str:
    if not isinstance(value, str) or not value.strip():
        return ""
    expanded = os.path.expandvars(value.strip().strip('"')).replace("/", "\\")
    return expanded.rstrip("\\").casefold()


def entity_reference(entity: dict, *, reason: str, confidence: float) -> dict:
    return {
        "entity_id": entity.get("id"),
        "name": entity.get("name") or "未命名软件",
        "publisher": entity.get("publisher"),
        "icon": entity.get("icon"),
        "reason": reason,
        "confidence": confidence,
    }


def match_path_entity(path: object, entities: Iterable[dict]) -> dict | None:
    """用可执行文件或 DLL 的实际路径关联软件，拒绝仅凭文件名猜测。"""
    candidate = normalize_path(path)
    if not candidate:
        return None
    best: tuple[int, dict, str, float] | None = None
    for entity in entities:
        executable = normalize_path(entity.get("exe_path"))
        if executable and candidate == executable:
            return entity_reference(
                entity, reason="右键扩展文件与软件主程序一致", confidence=1.0)
        location = normalize_path(entity.get("location"))
        if not location:
            continue
        if candidate == location or candidate.startswith(location + "\\"):
            score = len(location)
            if best is None or score > best[0]:
                best = (score, entity, "右键扩展文件位于软件安装目录", 0.98)
    if not best:
        return None
    return entity_reference(best[1], reason=best[2], confidence=best[3])


def match_registry_entity(entry: dict, entities: Iterable[dict]) -> dict | None:
    """把一条 Uninstall 登记匹配到台账，拒绝弱子串猜测。"""
    entity_list = list(entities)
    direct_id = registry_entity_id(entry.get("name"), entry.get("publisher"))
    direct = next((entity for entity in entity_list if entity.get("id") == direct_id), None)
    if direct:
        return entity_reference(direct, reason="软件名与发布商和台账登记一致", confidence=1.0)

    entry_path = normalize_path(entry.get("location") or entry.get("install_location"))
    if entry_path:
        path_matches = []
        for entity in entity_list:
            entity_path = normalize_path(entity.get("location"))
            if not entity_path:
                continue
            if entry_path == entity_path:
                path_matches.append((len(entity_path), entity))
        if path_matches:
            entity = max(path_matches, key=lambda pair: pair[0])[1]
            return entity_reference(entity, reason="安装目录和软件台账一致", confidence=0.98)

    entry_name = normalize_name(entry.get("name"))
    entry_publisher = normalize_publisher(entry.get("publisher"))
    fragment_matches = []
    for entity in entity_list:
        if entry_publisher and normalize_publisher(entity.get("publisher")) != entry_publisher:
            continue
        if any(normalize_name(fragment) == entry_name for fragment in entity.get("fragments") or []):
            fragment_matches.append(entity)
    if len(fragment_matches) == 1:
        return entity_reference(
            fragment_matches[0], reason="该登记是软件台账已聚合的组件", confidence=0.94)

    name_matches = [
        entity for entity in entity_list
        if normalize_name(entity.get("name")) == entry_name
        and (not entry_publisher
             or normalize_publisher(entity.get("publisher")) == entry_publisher)
    ]
    if len(name_matches) == 1:
        return entity_reference(name_matches[0], reason="软件登记名称唯一匹配", confidence=0.9)
    return None


def _value_paths(value: str) -> list[str]:
    paths = []
    for part in value.split(";"):
        normalized = normalize_path(part)
        if normalized and ":\\" in normalized and "%" not in normalized:
            paths.append(normalized)
    return paths


def relate_environment_variable(
    name: str,
    value: str,
    entities: Iterable[dict],
    *,
    sensitive: bool = False,
) -> list[dict]:
    """用官方重定向声明或路径归属关联变量，可返回 PATH 的多个软件。"""
    variable = name.casefold()
    value_paths = [] if sensitive else _value_paths(value)
    matches: dict[str, dict] = {}
    for entity in entities:
        entity_id = entity.get("id")
        if not entity_id:
            continue
        redirects = {
            str(redirect).strip().casefold()
            for redirect in entity.get("redirects") or []
            if str(redirect).strip()
        }
        if variable in redirects:
            matches[entity_id] = entity_reference(
                entity, reason="该软件声明使用此变量重定向数据目录", confidence=1.0)
            continue

        references = [normalize_path(entity.get("location")), normalize_path(entity.get("exe_path"))]
        references = [path for path in references if path]
        if not references:
            continue
        if any(
            candidate == reference or candidate.startswith(reference + "\\")
            for candidate in value_paths
            for reference in references
        ):
            matches[entity_id] = entity_reference(
                entity, reason="变量值指向该软件的安装目录", confidence=0.98)

    return sorted(
        matches.values(),
        key=lambda relation: (-relation["confidence"], relation["name"].casefold()),
    )


__all__ = [
    "entity_reference", "match_path_entity", "match_registry_entity", "normalize_name",
    "normalize_path", "normalize_publisher", "registry_entity_id",
    "relate_environment_variable",
]
