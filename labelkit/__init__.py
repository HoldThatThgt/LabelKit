"""LabelKit — 采集数据自动标注工具.

Stateless, single-process CLI batch pipeline: ingest → dedup → (optional)
classify → quality scoring (QuRating) → annotate → (optional) generate /
verify → emit.
"""

__version__ = "1.0.0"
TOOL_VERSION = f"labelkit/{__version__}"
