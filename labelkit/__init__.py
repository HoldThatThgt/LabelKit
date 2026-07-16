"""LabelKit — 采集数据自动标注工具.

Stateless, single-process CLI batch pipeline: ingest → (optional) segment →
(optional) stitch → dedup → (optional) classify → (optional) extract →
quality scoring (QuRating) → (optional) generate → annotate → (optional)
verify → emit.
"""

__version__ = "1.0.0"
TOOL_VERSION = f"labelkit/{__version__}"
