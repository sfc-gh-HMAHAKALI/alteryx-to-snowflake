"""Parse an Alteryx workflow XML into a typed graph.

Alteryx files are plain XML: <Nodes> (tools, possibly nested inside ToolContainers)
and <Connections> (origin tool/anchor -> destination tool/anchor).
"""
from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path

# Plugin suffix -> short tool type. Unknown plugins keep their suffix.
PLUGIN_ALIASES = {
    "DbFileInput": "Input", "DbFileOutput": "Output", "TextInput": "TextInput",
    "AlteryxSelect": "Select", "Formula": "Formula", "Filter": "Filter",
    "Join": "Join", "Union": "Union", "Summarize": "Summarize", "Unique": "Unique",
    "Sort": "Sort", "CrossTab": "CrossTab", "Transpose": "Transpose",
    "DynamicRename": "DynamicRename", "BrowseV2": "Browse", "Test": "Test",
    "ToolContainer": "Container", "TextBox": "Comment", "Tab": "AppInterface",
    "Sample": "Sample", "RecordID": "RecordID", "MultiRowFormula": "MultiRowFormula",
    "AppendFields": "AppendFields", "JoinMultiple": "JoinMultiple",
    "TextToColumns": "TextToColumns", "DateTime": "DateTime", "FindReplace": "FindReplace",
    "RegEx": "RegEx", "GenerateRows": "GenerateRows", "RunningTotal": "RunningTotal",
    "LockInInput": "InDbInput", "LockInFilter": "InDbFilter",
}
NON_DATA = {"Container", "Comment", "AppInterface"}

# Alteryx field types -> Snowflake types
ATYPE_TO_SF = {
    "Bool": "BOOLEAN", "Byte": "NUMBER(3,0)", "Int16": "NUMBER(5,0)", "Int32": "NUMBER(10,0)",
    "Int64": "NUMBER(19,0)", "FixedDecimal": "NUMBER(38,{scale})", "Float": "FLOAT",
    "Double": "FLOAT", "String": "VARCHAR", "WString": "VARCHAR", "V_String": "VARCHAR",
    "V_WString": "VARCHAR", "Date": "DATE", "Time": "TIME", "DateTime": "TIMESTAMP_NTZ",
    "Blob": "BINARY", "SpatialObj": "GEOGRAPHY",
}
NUMERIC_ATYPES = {"Byte", "Int16", "Int32", "Int64", "FixedDecimal", "Float", "Double"}
STRING_ATYPES = {"String", "WString", "V_String", "V_WString"}


@dataclass
class Field:
    name: str
    atype: str = "V_WString"
    size: str | None = None
    scale: str | None = None

    @property
    def kind(self) -> str:
        if self.atype in NUMERIC_ATYPES:
            return "num"
        if self.atype in STRING_ATYPES:
            return "str"
        if self.atype in ("Date", "DateTime", "Time"):
            return "date"
        if self.atype == "Bool":
            return "bool"
        return "str"

    @property
    def sf_type(self) -> str:
        t = ATYPE_TO_SF.get(self.atype, "VARCHAR")
        return t.format(scale=self.scale or (self.size.split(".")[-1] if self.size and "." in self.size else "6"))


@dataclass
class Tool:
    id: str
    plugin: str
    type: str
    config: ET.Element | None
    annotation: str = ""
    container: str | None = None
    macro: str | None = None
    meta: dict[str, list[Field]] = field(default_factory=dict)  # anchor -> cached schema

    @property
    def label(self) -> str:
        return f"{self.type}#{self.id}"


@dataclass
class Edge:
    src: str
    src_anchor: str
    dst: str
    dst_anchor: str
    name: str | None = None  # e.g. "#1" for Union/Test ordering


@dataclass
class Workflow:
    path: Path
    name: str
    tools: dict[str, Tool]
    edges: list[Edge]
    constants: dict[str, str]
    is_app: bool
    containers: dict[str, str]

    def inputs_of(self, tid: str) -> list[Edge]:
        return [e for e in self.edges if e.dst == tid]

    def outputs_of(self, tid: str) -> list[Edge]:
        return [e for e in self.edges if e.src == tid]

    def topo_order(self) -> list[str]:
        indeg = defaultdict(int)
        for t in self.tools:
            indeg[t] += 0
        for e in self.edges:
            indeg[e.dst] += 1
        order, ready = [], sorted([t for t, d in indeg.items() if d == 0], key=int)
        while ready:
            t = ready.pop(0)
            order.append(t)
            for e in self.outputs_of(t):
                indeg[e.dst] -= 1
                if indeg[e.dst] == 0:
                    ready.append(e.dst)
                    ready.sort(key=int)
        if len(order) != len(indeg):
            raise ValueError(f"{self.name}: cycle detected in tool graph")
        return order


def _fields(record_info: ET.Element | None) -> list[Field]:
    if record_info is None:
        return []
    return [Field(f.get("name"), f.get("type", "V_WString"), f.get("size"), f.get("scale"))
            for f in record_info.findall("Field")]


def _iter_nodes(parent: ET.Element, container: str | None, containers: dict[str, str]):
    for n in parent.findall("Node"):
        yield n, container
        child = n.find("ChildNodes")
        if child is not None:
            cap = n.findtext("Properties/Configuration/Caption") or f"Container {n.get('ToolID')}"
            containers[n.get("ToolID")] = cap
            yield from _iter_nodes(child, cap, containers)


def parse_workflow(path: str | Path) -> Workflow:
    path = Path(path)
    root = ET.parse(path).getroot()
    tools: dict[str, Tool] = {}
    containers: dict[str, str] = {}
    for n, cont in _iter_nodes(root.find("Nodes"), None, containers):
        gui = n.find("GuiSettings")
        plugin = gui.get("Plugin", "") if gui is not None else ""
        eng = n.find("EngineSettings")
        macro = eng.get("Macro") if eng is not None else None
        suffix = plugin.split(".")[-1] if plugin else ""
        ttype = PLUGIN_ALIASES.get(suffix, suffix) if plugin else ("Macro:" + Path(macro).stem if macro else "Unknown")
        if ttype in NON_DATA:
            continue
        ann = (n.findtext("Properties/Annotation/AnnotationText") or
               n.findtext("Properties/Annotation/DefaultAnnotationText") or "").strip()
        meta = {}
        for m in n.findall("Properties/MetaInfo"):
            meta[m.get("connection", "Output")] = _fields(m.find("RecordInfo"))
        tools[n.get("ToolID")] = Tool(n.get("ToolID"), plugin, ttype, n.find("Properties/Configuration"),
                                      re.sub(r"\s+", " ", ann), cont, macro, meta)
    edges = []
    for c in root.findall("Connections/Connection"):
        o, d = c.find("Origin"), c.find("Destination")
        if o.get("ToolID") in tools and d.get("ToolID") in tools:
            edges.append(Edge(o.get("ToolID"), o.get("Connection"), d.get("ToolID"), d.get("Connection"), c.get("name")))
    constants = {f"{c.findtext('Namespace')}.{c.findtext('Name')}": c.findtext("Value") or ""
                 for c in root.findall("Properties/Constants/Constant")}
    is_app = path.suffix.lower() == ".yxwz" or root.find(".//Questions") is not None
    wf = Workflow(path, path.stem, tools, edges, constants, is_app, containers)
    # analytic-app questions that inject user input (Tab is only a container for them)
    wf.app_questions = [q.findtext("Name") or "" for q in root.iter("Question") if (q.findtext("Type") or "") != "Tab"]
    return wf


# Tools the mappers translate deterministically; everything else is flagged.
SUPPORTED = {"Input", "Output", "TextInput", "Select", "Formula", "Filter", "Join", "Union",
             "Summarize", "Unique", "Sort", "CrossTab", "Transpose", "DynamicRename", "Browse",
             "Test", "Macro:Cleanse", "Sample", "RecordID", "AppendFields"}
# Weights for a rough complexity score (per tool)
WEIGHTS = {"Join": 3, "Formula": 2, "Filter": 1, "Summarize": 2, "CrossTab": 4, "Transpose": 3,
           "DynamicRename": 4, "Union": 1, "Unique": 2, "Select": 1, "Test": 2, "Sort": 1}


def inventory(wf: Workflow) -> dict:
    from collections import Counter
    cnt = Counter(t.type for t in wf.tools.values())
    unsupported = sorted({t.type for t in wf.tools.values() if t.type not in SUPPORTED})
    sources, sinks = [], []
    for t in wf.tools.values():
        if t.type in ("Input", "Output"):
            f = t.config.find("File")
            raw = (f.text or "").strip()
            entry = {"tool": t.id, "path": raw.split("|||")[0], "sheet": raw.split("|||")[1] if "|||" in raw else None,
                     "fields": [x.name for x in t.meta.get("Output", [])]}
            if t.type == "Input" and f.get("OutputFileName"):
                entry["adds_filename_field"] = f.get("OutputFileName")
            (sources if t.type == "Input" else sinks).append(entry)
        elif t.type == "TextInput":
            sources.append({"tool": t.id, "path": "<inline TextInput>",
                            "rows": len(t.config.findall("Data/r")),
                            "fields": [x.get("name") for x in t.config.findall("Fields/Field")]})
    score = sum(WEIGHTS.get(t.type, 1) for t in wf.tools.values())
    level = "Simple" if score < 60 else "Medium" if score < 180 else "Complex"
    return {"workflow": wf.name, "file": str(wf.path), "is_analytic_app": wf.is_app,
            "app_questions": getattr(wf, "app_questions", []),
            "tool_count": len(wf.tools), "tool_types": dict(cnt.most_common()),
            "unsupported_tools": unsupported, "containers": list(wf.containers.values()),
            "sources": sources, "sinks": sinks, "constants": wf.constants,
            "complexity_score": score, "complexity": level,
            "browse_terminals": [t.id for t in wf.tools.values() if t.type == "Browse"]}
