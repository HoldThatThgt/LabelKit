"""M11 —— 输出发射器（spec 3.11、ch.6；CONTRACTS.md §7.10、§9）。

三个通道：
- 主输出 JSONL：逐批追加写入 ``{output}.part`` 并 flush，finalize 时 fsync + 原子改名
  交付；
- rejects 通道 ``{output_stem}.rejects.jsonl``（流式追加日志，无 ``.part``）；
- ``{output_stem}.report.json``（finalize 时恒写；v1.17 SPEC-SP §5.1：live 与
  dry-run 的 report 命名由 M1 一次裁决写进 ``ResolvedPaths.report``，发射器只消费）。

按状态分发（v1.9 四路，spec 3.11.2）：``active`` → 主输出；``absorbed`` → 两个通道都不
进，只计数（成员内容活在其 episode 的序列记录里）；``stitched`` → 两个通道都不进，只
计数（v1.9 T21：被合并片段壳的内容活在其 thread 的重绑记录里——壳绝不能落到 rejects
兜底路径）；其余一切非 active 状态 → rejects。

v1.13（裁决·按类标注 Schema，spec §6.3）：写前终检按行取类有效 Schema——该行自身标签
声明了 ``[class.<name>.annotate]`` 覆盖时用覆盖，否则用全局 ``output.schema``。

发射器绝不因单条坏记录崩溃：写前 ``validate_only`` 不通过（内部不变式破裂）就把该条
改道 rejects 并标 ``internal_error``，运行继续。记录级隔离只覆盖 meta 装配与序列化——
通道写出的 ``OSError`` 是 run 级失败（``.part`` 里可能已留半行）：它以
``LabelKitError`` 上抛（CLI 退出码 4）并把本次运行标记为不可交付，使 ``finalize``
永不把损坏的 ``.part`` 改名（spec 3.11.3 ④）。
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Mapping, Sequence

from labelkit import TOOL_VERSION
from labelkit.common.errors import ErrorKind, LabelKitError
from labelkit.common.contracts.types import PipelineItem, Record, StageError
# v1.10（U21）：plain 模式的进度/摘要行格式活在 common 层的纯函数模块里，与 CLI
# 渲染器共用（operators → common 是许可的依赖方向；cli ↛ operators 依旧成立）。
from labelkit.common.observability import console_format

if TYPE_CHECKING:  # pragma: no cover —— 导入期服务模块可能尚未就位
    from labelkit.common.config.model import ResolvedConfig
    from labelkit.common.runtime.schema_engine import SchemaEngine

_log = logging.getLogger("labelkit.emitter")


@dataclass(frozen=True)                            # [FROZEN in CONTRACTS.md §7.10]
class EmitResult:
    """单批发射结果（CONTRACTS.md §7.10 冻结形态）。"""
    emitted: int                                   # 本批写入主输出的行数
    rejected: int                                  # 本批判入 rejects 的条数


def _dumps(obj: Any) -> str:
    """紧凑单行 JSON 序列化，非 ASCII 保真（CONTRACTS.md §9.1）。

    :param obj: 待序列化对象。
    :returns: 单行 JSON 文本（不含换行）。
    """
    return json.dumps(obj, ensure_ascii=False)


class Emitter:
    """M11 发射器：五个输出通道的所有者，签名冻结在 CONTRACTS.md §7.10。"""

    def __init__(self, cfg: "ResolvedConfig", engine: "SchemaEngine",
                 run_id: str, run_started_at: datetime):
        """构造发射器（只推导路径与初始化计数，不触碰磁盘）。

        :param cfg: 已解析配置。
        :param engine: M8 Schema 引擎（写前终检用）。
        :param run_id: 本次运行 id。
        :param run_started_at: 本次运行起始时刻（写进 ``_meta.run``）。
        """
        self._cfg = cfg
        self._engine = engine
        self._run_id = run_id
        self._run_started_at = run_started_at
        self._init_paths(cfg)
        self._init_counters()
        self._init_schemas(cfg)

    def _init_paths(self, cfg: "ResolvedConfig") -> None:
        """v1.17（SPEC-SP §5.1）：五个输出通道路径只消费 M1 冻结的 ``cfg.paths``。

        live/dry-run 的 report 命名已由 M1 一次裁决写进 ``paths.report``——此处
        不再按命令模式追加后缀，也不再从 ``run.output`` 字符串做 cwd 二次推导；
        未启用通道在 M1 侧为 None，``.part`` 暂存名仍由最终路径拼接派生。

        :param cfg: 已解析配置。
        :raises ValueError: ``cfg.paths`` 缺席（直接构造 ResolvedConfig 的旧
            fixture 面），或通道开闭与 M1 派生产物不一致。
        """
        if cfg.paths is None:
            raise ValueError("ResolvedConfig.paths is None: emitter consumes "
                             "M1-derived absolute paths only (no cwd fallback)")
        paths = cfg.paths
        self._output_path = Path(paths.output)
        self._output_part = Path(str(self._output_path) + ".part")
        self._report_path = Path(paths.report)
        if cfg.output.meta_mode == "sidecar" and paths.sidecar is None:
            raise ValueError("paths.sidecar is None but output.meta_mode == "
                             "'sidecar'")
        self._sidecar_path = self._with_part_source(paths.sidecar)
        self._sidecar_part = self._with_part(self._sidecar_path)
        if cfg.output.rejects != "none" and paths.rejects is None:
            raise ValueError("paths.rejects is None but output.rejects is on")
        self._rejects_path = self._with_part_source(paths.rejects)
        if cfg.generate_stream.enabled and paths.stream_artifact is None:
            raise ValueError("paths.stream_artifact is None but generate "
                             "stream form is enabled")
        self._artifact_path = self._with_part_source(paths.stream_artifact)
        self._artifact_part = self._with_part(self._artifact_path)
        self._main_fh = None
        self._sidecar_fh = None
        self._rejects_fh = None
        self._artifact_fh = None

    @staticmethod
    def _with_part_source(raw: str | None) -> Path | None:
        """把 M1 的通道路径串转成 Path（未启用通道保持 None）。

        :param raw: ``ResolvedPaths`` 上的通道路径串；None = 通道关闭。
        :returns: 通道最终路径；未启用通道为 None。
        """
        return Path(raw) if raw is not None else None

    @staticmethod
    def _with_part(path: Path | None) -> Path | None:
        """通道最终路径 → ``.part`` 暂存名（None 通道保持 None）。

        :param path: 通道最终路径。
        :returns: 追加了 ``.part`` 的暂存路径；未启用通道为 None。
        """
        return Path(str(path) + ".part") if path is not None else None

    def _init_counters(self) -> None:
        """初始化批间累计计数、通道状态标志与装配期注入的鸭子面。"""
        # v1.13：工件 run 摘要条目（路径/sha256/行数，主输出同款形态）——
        # write_stream_artifact 写入时冻结，M10 组报告时鸭子面读取；未写恒 None。
        self.artifact_summary: dict | None = None
        self._emitted_total = 0
        self._rejected_total = 0
        self._status_totals: dict[str, int] = {}
        self._reject_lines_written = 0     # 实际落进 rejects 文件的行数
        self._rejects_opened = False
        self._undeliverable = False        # 有通道写失败：绝不改名 .part
        self._progress_active = False
        # v1.12：帧计数通路（frame_annotate.failed / frame_annotate.discarded）——
        # M10 装配期注入 MetricsSink（Ingestor.metrics 同款装配期鸭子面，构造签名
        # 冻结不变）；单测直接构造时缺省 None ⇒ 仅不计数，行为不变。
        self.metrics = None

    def _init_schemas(self, cfg: "ResolvedConfig") -> None:
        """冻结写前终检要用的两张 Schema 面（帧 Schema 与按类标注 Schema 表）。

        :param cfg: 已解析配置。
        """
        # v1.12：写前帧校验的 Schema 入口（frame.annotate 关闭时恒 None，M1 保证
        # 开启时必有解析产物）。
        self._frame_schema = (dict(cfg.frame_schema)
                              if cfg.frame_schema is not None else None)
        # v1.13（裁决·按类标注 Schema）：按序列类的写前终检 Schema 表——键 = 类名，
        # 仅声明了覆盖的类入表（未声明的类缺席 ⇒ 终检走全局 output.schema 的既有
        # 路径）。M5 侧 annotate.class_annotate_schema 的最小镜像：算子间不新增
        # 依赖（spec §2.2），两侧取值语义必须保持一致。
        self._class_schemas = {name: dict(view.schema)
                               for name, view in cfg.class_views.items()
                               if view.schema is not None}

    # ── 通道生命周期 ──────────────────────────────────────────────────────

    def open(self) -> None:
        """创建/截断输出通道。

        :raises LabelKitError: 输出路径不可写（CLI 退出码 4）。
        """
        try:
            self._main_fh = open(self._output_part, "w", encoding="utf-8")
            if self._cfg.output.meta_mode == "sidecar":
                self._sidecar_fh = open(self._sidecar_part, "w", encoding="utf-8")
            if self._cfg.output.rejects != "none":
                self._rejects_fh = open(self._rejects_path, "w", encoding="utf-8")
                self._rejects_opened = True
        except OSError as exc:
            self._close_all()
            raise LabelKitError(f"output path unwritable: {exc}") from exc

    def emit_batch(self, batch: list[PipelineItem], batch_no: int) -> EmitResult:
        """按状态分发整批——四路（v1.9，spec 3.11.2）。

        active → 主输出；absorbed → 只计数（两通道都不进）；stitched → 只计数
        （v1.9 T21 第四路）；其余一切非 active 状态 → rejects。追加写 + flush。
        单条记录永不抛出——但通道写出的 OSError 是 run 级失败，以 LabelKitError
        上抛（spec 3.11.3 ④：``.part`` 里此刻可能已留半行）。

        :param batch: 待发射的一批信封。
        :param batch_no: 批号（进日志与 ``_meta.scores.batch_no``）。
        :returns: 本批的写出与判拒计数。
        :raises LabelKitError: 通道写出或 flush 失败（CLI 退出码 4）。
        """
        emitted = 0
        rejected = 0
        for item in batch:
            try:
                route = self._route_item(item, batch_no)
            except LabelKitError:
                raise  # 通道写失败——run 级，绝不降为记录级
            except Exception as exc:  # noqa: BLE001 —— 记录级隔离是绝对的
                # 栈只进 debug 级（§7.6）：str(exc) 可能内嵌记录内容，故 stderr 的
                # WARN 一行由 _divert_on_failure 只给异常类型。
                _log.debug("internal_error stack (record %s)", item.record.id,
                           exc_info=exc,
                           extra={"stage": "emitter", "batch": batch_no})
                self._divert_on_failure(item, batch_no, exc)
                route = "rejects"
            if route == "main":
                emitted += 1
            elif route == "rejects":
                rejected += 1

        self._flush()
        self._emitted_total += emitted
        self._rejected_total += rejected
        self._tally_batch(batch)
        _log.info(
            "batch %d flushed: main output +%d line(s) (total %d), "
            "rejects +%d (total %d)",
            batch_no, emitted, self._emitted_total, rejected, self._rejected_total,
            extra={"stage": "emitter", "batch": batch_no},
        )
        self._progress(batch_no)
        return EmitResult(emitted=emitted, rejected=rejected)

    def _route_item(self, item: PipelineItem, batch_no: int) -> str:
        """把单个信封分发到它该去的通道（v1.9 四路）。

        :param item: 待分发的信封。
        :param batch_no: 批号。
        :returns: ``"main"`` | ``"none"`` | ``"rejects"``——``"none"`` 即
            absorbed/stitched 两路：成员内容活在其 episode 的序列记录 / 其 thread
            的重绑记录里，两个通道都不进，只由下方的通用状态计数覆盖；壳落到
            rejects 兜底会以 internal_error 污染 rejects 并触发 --strict。
        :raises LabelKitError: 通道写出失败。
        """
        if item.status == "active":
            return self._route_active(item, batch_no)
        if item.status in ("absorbed", "stitched"):
            return "none"
        self._write_reject(item, batch_no)
        return "rejects"

    def _route_active(self, item: PipelineItem, batch_no: int) -> str:
        """active 信封的主输出路：不变式校验 → 写前终检 → 落主输出。

        :param item: 待写出的 active 信封。
        :param batch_no: 批号。
        :returns: ``"main"`` 或（终检不过改道后的）``"rejects"``。
        :raises LabelKitError: 通道写出失败。
        """
        annotate_on = self._cfg.annotate.enabled
        if annotate_on and item.annotation is None:
            # 不变式破裂：active 条目却没有标注。
            self._divert_internal(item, batch_no,
                                  ["active item has no annotation"],
                                  "active item has no annotation")
            self._write_reject(item, batch_no)
            return "rejects"
        user_obj = self._user_object(item)
        if annotate_on:
            violations = self._engine.validate_only(
                dict(user_obj), schema=self._row_schema(item))
            if violations:
                # 违规文本可能内嵌数据值：只进 rejects 通道（一条违规一个数组元素，
                # §9.2）；stderr 只得到去数据摘要（spec §7.1 ①）。
                self._divert_internal(
                    item, batch_no, list(violations),
                    "final validate_only failed: record "
                    f"{item.record.id}: {len(violations)} violation(s)",
                )
                self._write_reject(item, batch_no)
                return "rejects"
        self._write_main(item, user_obj, batch_no)
        return "main"

    def _divert_on_failure(self, item: PipelineItem, batch_no: int,
                           exc: BaseException) -> None:
        """记录级隔离兜底：发射单条时的意外异常一律改道 rejects。

        :param item: 出错的信封。
        :param batch_no: 批号。
        :param exc: 捕获到的意外异常。
        :raises LabelKitError: rejects 通道写出本身失败（run 级）。
        """
        # str(exc) 可能内嵌记录内容 → 只进 rejects 通道；stderr 日志只给异常类型
        # （栈由捕获点写进 debug 级，§7.6）。
        self._divert_internal(item, batch_no, [f"emitter failure: {exc}"],
                              f"emitter failure: {type(exc).__name__}")
        try:
            self._write_reject(item, batch_no)
        except LabelKitError:
            raise
        except Exception as reject_exc:  # noqa: BLE001 —— rejects 行装配本身失败
            _log.warning("rejects line assembly failed: record %s: %s",
                         item.record.id, type(reject_exc).__name__,
                         extra={"stage": "emitter", "batch": batch_no})

    def _tally_batch(self, batch: list[PipelineItem]) -> None:
        """批级状态计数 + v1.12 沉没成本记账。

        :param batch: 刚发射完的这一批信封。
        """
        discarded = 0
        for item in batch:
            self._status_totals[item.status] = self._status_totals.get(item.status, 0) + 1
            if item.status != "active" and item.member_annotations:
                # v1.12 沉没成本记账（spec §3.6）：终态非 active 的序列信封仍携带
                # 帧标注 ⇒ 已产出未交付，按非 None 条目数累计（仅计数，不落盘）。
                # 记账视角 = 首标签信封（终审缺陷修复：扇出克隆共享同一 dict，
                # 克隆终态不重复计——否则共享产物被计 k 次）。
                cls = item.classification
                if cls is not None and cls.labels and cls.label != cls.labels[0]:
                    continue
                discarded += sum(1 for ann in item.member_annotations.values()
                                 if ann is not None)
        if discarded and self.metrics is not None:
            self.metrics.count("frame_annotate.discarded", discarded)

    def write_stream_artifact(self, lines: Sequence[str]) -> None:
        """v1.13（裁决·时间流工件通道）：把交织序定稿的工件行写入
        ``{output_stem}.stream.jsonl.part``（写入 + flush；finalize 与主输出同批
        fsync + 原子改名；``_undeliverable`` 纪律共用）。dry-run 天然不触达
        （``_run_dry`` 不驱动生成、不开 emitter 通道）。同时冻结 run 摘要条目
        （路径/sha256/行数——sha256 按落盘字节计，config_digest 同款前缀形态）。"""
        if self._artifact_part is None:
            # M1 保证形态开启 ⇒ paths.stream_artifact 必非 None（构造期已核对）；
            # 此处只是绝不把 None 拼成 cwd 下的 "None.part" 的硬闸。
            raise LabelKitError("stream artifact channel not configured "
                                "(ResolvedPaths.stream_artifact is None)")
        try:
            self._artifact_fh = open(self._artifact_part, "w", encoding="utf-8")
        except OSError as exc:
            self._undeliverable = True
            raise LabelKitError(f"stream artifact channel unwritable: {exc}") from exc
        digest = hashlib.sha256()
        for line in lines:
            data = line + "\n"
            digest.update(data.encode("utf-8"))
            self._channel_write(self._artifact_fh, data, "stream artifact")
        try:
            self._artifact_fh.flush()
        except OSError as exc:
            self._undeliverable = True
            raise LabelKitError(f"stream artifact flush failed: {exc}") from exc
        self.artifact_summary = {"path": str(self._artifact_path),
                                 "sha256": "sha256:" + digest.hexdigest(),
                                 "lines": len(lines)}
        _log.info("stream artifact staged: %s (%d lines)",
                  self._artifact_part, len(lines),
                  extra={"stage": "emitter", "batch": 0})

    def finalize(self, report: Mapping, deliver: bool = True) -> None:
        """收尾：deliver=True 时 fsync + 原子改名；report.json 恒写。

        deliver=False 只出现在 dry-run（从未开过 ``.part``）；v1.6：熔断收尾传的
        是 deliver=True——已完成的批照常交付，由报告标记 run.partial_delivery
        （spec 3.10.3 熔断交付）。先前发生过通道写失败会强制 deliver=False：可能
        损坏的 ``.part`` 绝不会被改名成最终名（spec 3.11.3 ④）。v1.13：时间流工件
        通道（若已暂存）与主输出在同一次 finalize 里按同一规则交付。

        :param report: 待写出的 report 对象（counts-only）。
        :param deliver: 是否真正交付；False = 只关闭通道不改名。
        :raises LabelKitError: 交付失败或 report 写失败。
        """
        self._end_progress()
        deliver = deliver and not self._undeliverable
        self._deliver_channels(deliver)
        if deliver:
            _log.info(
                "finalize: fsync + rename  %s -> %s (%d lines)",
                self._output_part, self._output_path, self._emitted_total,
                extra={"stage": "emitter", "batch": "-"},
            )
        self._write_report(report)
        self._print_summary(report)

    def _deliver_channels(self, deliver: bool) -> None:
        """逐个交付已开通道（主输出 → 工件 → sidecar → rejects），末了统一收口。

        :param deliver: True = fsync + 原子改名；False = 仅关闭。
        :raises LabelKitError: 任一通道交付失败（CLI 退出码 4）。
        """
        try:
            self._deliver(self._main_fh, self._output_part, self._output_path, deliver)
            self._main_fh = None
            if self._artifact_fh is not None:
                self._deliver(self._artifact_fh, self._artifact_part,
                              self._artifact_path, deliver)
                self._artifact_fh = None
            if self._sidecar_fh is not None:
                self._deliver(self._sidecar_fh, self._sidecar_part, self._sidecar_path, deliver)
                self._sidecar_fh = None
            if self._rejects_fh is not None:
                self._rejects_fh.flush()
                self._rejects_fh.close()
                self._rejects_fh = None
        except OSError as exc:
            raise LabelKitError(f"output delivery failed: {exc}") from exc
        finally:
            self._close_all()

    def _write_report(self, report: Mapping) -> None:
        """写出 report.json，并打印 spec 3.11.3 ③ 的 run 收尾行。

        :param report: 待写出的 report 对象。
        :raises LabelKitError: report 写失败（CLI 退出码 1）。
        """
        try:
            self._report_path.write_text(
                json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
            )
        except OSError as exc:
            raise LabelKitError("report write failed") from exc

        # spec 3.11.3 ③ 逐字 run 收尾行：rejects 文件（实际行数，且仅在通道开过时
        # 出现）加 report 路径。
        if self._rejects_opened:
            _log.info(
                "wrote %s (%d lines) and %s",
                self._rejects_path, self._reject_lines_written, self._report_path,
                extra={"stage": "emitter", "batch": "-"},
            )
        else:
            _log.info("wrote %s", self._report_path,
                      extra={"stage": "emitter", "batch": "-"})

    # ── 主输出通道 ────────────────────────────────────────────────────────

    def _user_object(self, item: PipelineItem) -> Mapping:
        """取该行落盘的用户对象。

        :param item: 待写出的信封。
        :returns: annotate 开启时为标注产物，否则为记录原始载荷。
        """
        if self._cfg.annotate.enabled:
            return item.annotation.output  # type: ignore[union-attr]
        return _raw_payload(item.record)

    def _row_schema(self, item: PipelineItem) -> dict | None:
        """写前终检的按行 Schema（v1.13 裁决·按类标注 Schema）。

        :param item: 待发射的信封；``item.classification.label`` 是该行的序列类
            标签（multi 扇出的每个兄弟信封各带自己的标签，故按行天然对齐）。
        :returns: 该类声明的标注 Schema 覆盖；None = 无覆盖（未分类、未知类或
            该类未声明）⇒ ``validate_only`` 走全局 ``output.schema`` 的既有缺省
            路径，字节等价 v1.12。
        """
        cls = item.classification
        if cls is None:
            return None
        return self._class_schemas.get(cls.label)

    def _write_main(self, item: PipelineItem, user_obj: Mapping, batch_no: int) -> None:
        """把一行写进主输出（inline/sidecar/none 三种 meta 形态）。

        :param item: 待写出的信封。
        :param user_obj: 该行的用户对象。
        :param batch_no: 批号。
        :raises LabelKitError: 通道写出失败。
        """
        # 先把每一行整体装配并序列化完（记录级失败就停在记录级，绝不留下半行、也
        # 绝不打乱 sidecar 的冻结行对齐，spec 3.11.3 ①）；然后才写。
        mode = self._cfg.output.meta_mode
        sidecar_line: str | None = None
        if mode == "inline":
            line_obj = dict(user_obj)
            line_obj["_meta"] = self._assemble_meta(item, batch_no)
            main_line = _dumps(line_obj) + "\n"
        elif mode == "sidecar":
            main_line = _dumps(dict(user_obj)) + "\n"
            sidecar_line = _dumps({"_meta": self._assemble_meta(item, batch_no)}) + "\n"
        else:  # "none"
            main_line = _dumps(dict(user_obj)) + "\n"
        self._channel_write(self._main_fh, main_line, "main output")
        if sidecar_line is not None:
            self._channel_write(self._sidecar_fh, sidecar_line, "sidecar")

    def _channel_write(self, fh, line: str, channel: str) -> None:
        """向某个通道写一行；写失败即 run 级失败。

        写失败可能在通道文件里留下半行——因此标记本次运行不可交付并上抛。

        :param fh: 目标通道的文件句柄。
        :param line: 待写出的整行文本（含换行）。
        :param channel: 通道名（只进错误消息，不含数据内容）。
        :raises LabelKitError: 写出失败（CLI 退出码 4）。
        """
        try:
            fh.write(line)
        except OSError as exc:
            self._undeliverable = True
            raise LabelKitError(f"{channel} channel write failed: {exc}") from exc

    def _assemble_meta(self, item: PipelineItem, batch_no: int) -> dict:
        """装配 §6.3 的 `_meta` 对象——顶层键恒在场，关闭的阶段取 null。

        :param item: 该行的信封。
        :param batch_no: 批号。
        :returns: 顶层键序冻结的 `_meta` 对象。
        """
        rec = item.record
        return {
            "id": rec.id,
            "run": {
                "tool": TOOL_VERSION,
                "started_at": self._run_started_at.isoformat(),
                "project_file": self._cfg.project_path,
                "rubric": self._rubric_selector(),
                "seed": self._cfg.run.seed,
            },
            "source": self._source_block(rec, with_fields=True),
            # v1.8 恒在场键（§9.1）：segment 关闭时取 null；位置在 source 与
            # scores 之间，与链序一致。
            "stream": self._stream_block(item),
            "scores": self._scores_block(item, batch_no),
            "dedup": {"kind": item.dedup.kind} if item.dedup is not None else None,
            # v1.7 恒在场键（§9.1）：信封没带分类时取 null（classify 关闭，或根本
            # 没走到）——与其余阶段键同一惯例；位置在 dedup 与 annotation 之间，
            # 与链序一致。
            "classification": (
                {"label": item.classification.label,
                 "labels": list(item.classification.labels),
                 "source": item.classification.source}
                if item.classification is not None else None
            ),
            "annotation": self._annotation_block(item),
            "verification": self._verification_block(item),
        }

    def _rubric_selector(self) -> str:
        """`_meta.run.rubric` 的取值。

        :returns: 内联准则时取准则名，否则取 ``default:*`` 选择子。
        """
        sel = self._cfg.quality.rubric
        if sel == "inline":
            return self._cfg.rubric.name
        if sel in ("default:text", "default:ui", "default:trajectory"):
            return sel
        # "" 本应已被 M1 解析掉；此处镜像 loader 的解析规则（v1.8 S29：流模式把空
        # 选择子解析成轨迹准则，两种模态皆然；v1.13 裁决·轨迹准则自动解析扩展：
        # 时间流生成形态同样在给序列打分——条件扩为 segment.enabled ∨
        # generate_stream.enabled，与 loader 两侧对齐）。
        if self._cfg.segment.enabled or self._cfg.generate_stream.enabled:
            return "default:trajectory"
        return f"default:{self._cfg.run.modality}"

    def _source_block(self, rec: Record, *, with_fields: bool) -> dict:
        """装配 `_meta.source`（rejects 侧共用同一装配，只是不带 fields）。

        :param rec: 该行的记录。
        :param with_fields: True = 主输出形态（带 fields 与恒在场的 generator）。
        :returns: source 块。
        """
        ref = rec.ref
        src: dict = {"file": ref.source_file}
        # line_no / pair_index 恰有其一（§9.1）；生成记录两者皆 null，写出
        # "pair_index": null（CONTRACTS.md §12.20）。
        if ref.line_no is not None:
            src["line_no"] = ref.line_no
        else:
            src["pair_index"] = ref.pair_index
        src["generated_from"] = list(ref.generated_from)
        if with_fields:
            src["fields"] = self._passthrough(rec)
            src["generator"] = dict(ref.generator) if ref.generator is not None else None
        elif ref.generator is not None:  # rejects：generator 有才写，且不带 fields
            src["generator"] = dict(ref.generator)
        return src

    def _passthrough(self, rec: Record) -> dict:
        """按 ``output.passthrough_fields`` 从 raw 里摘出透传字段。

        :param rec: 该行的记录。
        :returns: 透传字段子集（raw 里缺席的字段直接不出现）。
        """
        raw = rec.raw or {}
        return {
            f: raw[f] for f in self._cfg.output.passthrough_fields if f in raw
        }

    def _scores_block(self, item: PipelineItem, batch_no: int) -> dict | None:
        """装配 `_meta.scores`。

        :param item: 该行的信封。
        :param batch_no: 批号（写进 batch_no 列）。
        :returns: scores 块；信封未评分时为 None。
        """
        if not item.scores:
            return None
        block: dict = {}
        mode: str | None = None
        for key, qs in item.scores.items():
            if key == "__aggregate__":
                continue
            block[key] = qs.score
            if mode is None:
                mode = qs.mode
        agg = item.scores.get("__aggregate__")
        block["__aggregate__"] = agg.score if agg is not None else None
        if agg is not None:
            mode = agg.mode
        block["mode"] = mode or (
            "pairwise_bt" if self._cfg.quality.mode == "pairwise" else "pointwise"
        )
        block["batch_no"] = batch_no
        if self._cfg.classify.enabled and item.classification is not None:
            # v1.7（§9.1）：该信封参与排序的评分池——仅 classify 开启时在场。
            block["pool"] = item.classification.label
        return block

    def _stream_block(self, item: PipelineItem) -> dict | None:
        """装配 v1.8 的 `_meta.stream` 值（§9.1 / spec §6.3）。

        segment 关闭时恒 null。流模式下每一行主输出都是一条 episode（序列记录）
        ——这里遇到非序列记录属防御性分支，同样给 null。session_split /
        stream_repaired / segment_degraded 以鸭子面信封标记传递，由 M10/M7/M14
        写入（S21/S26，§7.6）。v1.9（T16/m-11）：thread_id / fragments 与逐步的
        resumed 标志仅在 stitch 开启时在场——这是关闭态字节等价的条件。顶层
        order_span 保持信封跨度（§6.3 包络规则：多片段 thread 的跨度里可能夹着别
        的 thread 的帧——下游切片必须用 fragments[].order_span）。
        v1.13（裁决·members 呈现真值门）：门扩为 segment.enabled ∨
        generate_stream.enabled——直装行原样复用本块（order_span/member_sources
        指向工件路径与行号；session_split=false / repaired=false / degraded=null
        / steps=null 均由鸭子面缺省值落出；stitch 两键保持缺席）。

        :param item: 该行的信封。
        :returns: stream 块；非流模式或非序列记录时为 None。
        """
        rec = item.record
        stream_on = self._cfg.segment.enabled or self._cfg.generate_stream.enabled
        if not stream_on or rec.kind != "sequence":
            return None
        members = rec.members
        block: dict = {"episode_id": rec.id}
        if self._cfg.stitch.enabled:
            block["thread_id"] = item.thread_id       # == episode_id（T22）
        block.update({
            "session_id": item.session_id,
            "order_span": [_order_key_repr(members[0]), _order_key_repr(members[-1])],
            "member_count": len(members),
            "member_ids": [m.id for m in members],
            "member_sources": [_member_source(m) for m in members],
        })
        if (self._cfg.frame_classify.enabled or self._cfg.frame_annotate.enabled
                or self._cfg.generate_stream.enabled):
            # v1.12（spec §3.6）：members 数组仅在任一帧开关开启时在场，位置冻结在
            # member_sources 之后、session_split 之前；全关时块形态与 v1.11 字节等价。
            # v1.13（裁决·members 呈现真值门）：时间流生成形态同门在场——label 列
            # 承载帧类真值（member_classifications，inherited），无 annotation/
            # status 列（frame.annotate 与本形态 M1 互斥）。
            block["members"] = self._members_block(item)
        self._append_stream_tail(block, item)
        return block

    def _append_stream_tail(self, block: dict, item: PipelineItem) -> None:
        """就地补齐 `_meta.stream` 的尾部键（键序冻结：session_split / repaired /
        degraded[/ fragments] / steps）。

        :param block: 待补齐的 stream 块（就地修改）。
        :param item: 该行的信封。
        """
        stitch_on = self._cfg.stitch.enabled
        block.update({
            "session_split": bool(getattr(item, "session_split", False)),
            "repaired": bool(getattr(item, "stream_repaired", False)),
            "degraded": getattr(item, "segment_degraded", None),
        })
        if stitch_on:
            fragments = getattr(item, "stitch_fragments", None)
            block["fragments"] = ([dict(f) for f in fragments]
                                  if fragments is not None else None)
        block["steps"] = (None if item.transitions is None
                          else [_step_row(t, stitch_on) for t in item.transitions])

    def _members_block(self, item: PipelineItem) -> list[dict]:
        """v1.12（spec §3.6）：members 条目——逐成员按 rec.members 序，字段序冻结为
        index, id[, label][, annotation, status]。label 键仅 frame.classify 开启时
        在场（dict 为 None 或缺键 ⇒ null，覆盖降格跳过）；annotation/status 两键仅
        frame.annotate 开启时在场（三值判定见 _member_annotation）。v1.13：label
        列门扩 ∨ generate_stream（帧类真值列，裁决·members 呈现真值门）。"""
        classify_on = (self._cfg.frame_classify.enabled
                       or self._cfg.generate_stream.enabled)
        annotate_on = self._cfg.frame_annotate.enabled
        rows: list[dict] = []
        for index, member in enumerate(item.record.members):
            row: dict = {"index": index, "id": member.id}
            if classify_on:
                cls = (item.member_classifications or {}).get(member.id)
                row["label"] = cls.label if cls is not None else None
            if annotate_on:
                row["annotation"], row["status"] = self._member_annotation(
                    item, member.id)
            rows.append(row)
        return rows

    def _member_annotation(self, item: PipelineItem,
                           member_id: str) -> tuple[dict | None, str]:
        """v1.12（spec §3.6）：status 闭集三值判定 + 写前校验兜底。dict 为 None 或
        缺键 ⇒ (null, "skipped")；值 None ⇒ (null, "failed")；对象 ⇒ 写前
        validate_only(obj, schema=帧 Schema)——通过 ⇒ (对象, "annotated")，不通过 ⇒
        (null, "failed") 且 frame_annotate.failed 计数，非法帧对象零落盘。"""
        annotations = item.member_annotations
        if annotations is None or member_id not in annotations:
            return None, "skipped"
        annotation = annotations[member_id]
        if annotation is None:
            return None, "failed"
        obj = dict(annotation.output)
        violations = self._engine.validate_only(obj, schema=self._frame_schema)
        if violations:
            # 违规文本可能携带数据值：stderr 只给去数据摘要（§7.1 ①），成员失败
            # 不改信封状态、不写 item.errors（成员失败非信封失败，spec §3.3）。
            if self.metrics is not None:
                self.metrics.count("frame_annotate.failed")
            _log.warning(
                "frame annotation failed pre-write check: episode %s member %s:"
                " %d violation(s)", item.record.id, member_id, len(violations),
                extra={"stage": "emitter", "batch": "-"})
            return None, "failed"
        return obj, "annotated"

    def _annotation_block(self, item: PipelineItem) -> dict | None:
        """装配 `_meta.annotation`。

        :param item: 该行的信封。
        :returns: annotation 块；未标注时为 None。
        """
        ann = item.annotation
        if ann is None:
            return None
        block: dict = {"model": ann.model, "attempts": ann.attempts}
        if ann.sc is not None:
            block["sc"] = dict(ann.sc)
        return block

    def _verification_block(self, item: PipelineItem) -> dict | None:
        """装配 `_meta.verification`。

        :param item: 该行的信封。
        :returns: verification 块；未审校时为 None。
        """
        ver = item.verification
        if ver is None:
            return None
        block: dict = {"verdict": ver.verdict, "rounds": ver.rounds}
        if self._cfg.segment.enabled:
            # v1.8（§9.1）：流模式带恒在场的 defects 键（无缺陷时为 []）；非流模式
            # 的审校块永不带它。
            block["defects"] = list(ver.defects)
        return block

    # ── rejects 通道 ──────────────────────────────────────────────────────

    def _divert_internal(self, item: PipelineItem, batch_no: int, errors: list[str],
                         log_message: str) -> None:
        """大声失败但继续运行：把该条标为 failed 且 kind = internal_error。

        ``errors``（全文，可能内嵌数据值）挂到信封上——一条违规一个 StageError，
        使 rejects 的 ``errors`` 数组保持逐违规一元素（spec 3.11.3 ②）。
        ``log_message`` 必须去数据：stderr 运行日志永不携带数据内容（spec §7.1
        ①）；异常栈由捕获点按 §7.6 写进 debug 级，不在这里重复。

        :param item: 待改道的信封。
        :param batch_no: 批号。
        :param errors: 逐条违规全文（进信封与 rejects，不进 stderr）。
        :param log_message: 去数据的 stderr 摘要。
        """
        for message in errors:
            item.errors.append(StageError(
                stage="emitter",
                kind=ErrorKind.INTERNAL_ERROR.value,
                message=message,
                retryable=False,
            ))
        item.status = "failed"
        _log.warning("internal_error: %s", log_message,
                     extra={"stage": "emitter", "batch": batch_no})

    def _write_reject(self, item: PipelineItem, batch_no: int) -> None:
        """把一条判拒记录写进 rejects 通道（``rejects = "none"`` 时直接返回）。

        :param item: 被判拒的信封。
        :param batch_no: 批号（保留在签名里与其余写出面对齐）。
        :raises LabelKitError: rejects 通道写出失败。
        """
        if self._rejects_fh is None:
            return
        stage, reason = self._reject_stage_reason(item)
        meta: dict = {
            "id": item.record.id,
            "source": self._source_block(item.record, with_fields=False),
            "stage": stage,
            "reason": reason,
            "errors": [e.message for e in item.errors],  # 无错误时为 []（冻结）
        }
        if self._cfg.classify.enabled:
            # v1.7 R5（§9.2）：classify 开启时闭集五键变六键——`label` 用来区分共享
            # 同一记录 id 的扇出兄弟；条目在被分类之前就判拒时取 null。refs 与 full
            # 两档都带它（full 是 refs 的扩展）。classify 关闭保持五键形态逐字节
            # 不变。
            meta["label"] = (item.classification.label
                             if item.classification is not None else None)
        row: dict = {"_meta": meta}
        if self._cfg.output.rejects == "full":
            row["record"] = _raw_payload(item.record)
            if reason == ErrorKind.SCHEMA_VIOLATION.value:
                # 失败阶段挂了 raw_last_output 时它随信封传过来
                # （SchemaViolation.raw_last_output）；缺席 ⇒ null。
                row["raw_last_output"] = getattr(item, "raw_last_output", None)
        self._channel_write(self._rejects_fh, _dumps(row) + "\n", "rejects")
        self._reject_lines_written += 1

    def _reject_stage_reason(self, item: PipelineItem) -> tuple[str, str]:
        """按终态推导 rejects 行的 (stage, reason) 二元组（§9.2）。

        :param item: 被判拒的信封。
        :returns: (归因阶段, 判拒原因)。
        """
        if item.status == "dropped_dup":
            kind = item.dedup.kind if item.dedup is not None else "exact"
            return "dedup", kind
        if item.status == "dropped_lowq":
            reason = ("top_ratio" if self._cfg.quality.selection == "top_ratio"
                      else "below_threshold")
            return "quality", reason
        if item.status == "dropped_verify":
            return "verify", "verify_fail"
        if item.status == "dropped_noise":
            # v1.8（§9.2）：这些帧不带 item.errors 条目——归因读的是翻转阶段
            # （M14/M7）留下的鸭子面标记：("segment", "noise") |
            # ("segment", "below_min_len") | ("verify", "off_task_member")。
            attribution = getattr(item, "noise_attribution", None)
            return attribution if attribution else ("segment", "noise")
        # failed（含发射器自己改道的 internal error）
        if item.errors:
            first = item.errors[0]
            return first.stage, first.kind
        return "emitter", ErrorKind.INTERNAL_ERROR.value

    # ── 通道管路 ──────────────────────────────────────────────────────────

    def _flush(self) -> None:
        """把全部已开通道的缓冲刷到磁盘。

        :raises LabelKitError: flush 失败（缓冲可能已部分落盘，等同写失败）。
        """
        try:
            for fh in (self._main_fh, self._sidecar_fh, self._rejects_fh,
                       self._artifact_fh):
                if fh is not None:
                    fh.flush()
        except OSError as exc:
            # 缓冲数据可能已部分落盘 → 与写失败同等对待。
            self._undeliverable = True
            raise LabelKitError(f"output flush failed: {exc}") from exc

    @staticmethod
    def _deliver(fh, part: Path, target: Path, deliver: bool) -> None:
        """交付单个通道：flush → fsync → 关闭 → 原子改名。

        :param fh: 通道文件句柄；None = 该通道从未开过，直接返回。
        :param part: ``.part`` 暂存路径。
        :param target: 最终路径。
        :param deliver: False = 只 flush 与关闭，不 fsync 也不改名。
        :raises OSError: fsync 或改名失败（由调用方转成 LabelKitError）。
        """
        if fh is None:
            return
        fh.flush()
        if deliver:
            os.fsync(fh.fileno())
        fh.close()
        if deliver:
            os.rename(part, target)

    def _close_all(self) -> None:
        """关闭全部已开通道并复位句柄（清理路径，绝不掩盖首要错误）。"""
        for fh in (self._main_fh, self._sidecar_fh, self._rejects_fh,
                   self._artifact_fh):
            if fh is not None:
                try:
                    fh.close()
                except OSError as exc:
                    # 清理期关闭失败不改变交付结论：首要错误已在上抛路径上。
                    _log.warning("channel close failed during cleanup: %s",
                                 type(exc).__name__,
                                 extra={"stage": "emitter", "batch": "-"})
        self._main_fh = self._sidecar_fh = self._rejects_fh = None
        self._artifact_fh = None

    # ── stderr 进度与摘要（展示面，不是日志——spec §7.7）─────────────────

    def _progress(self, batch_no: int) -> None:
        """TTY 批级进度（spec §7.7）：当前批号 + 逐状态累计计数。

        总批数与运行开销只有 M10/M9 知道，不接进发射器（可接受的削减）。
        v1.10（U21）：rich 静态闸在最前（面板取代此行；运行中降级或 `q` 脱离由
        渲染器负责，打印同一条 ``console_format`` 行）；plain 路径与 v1.9 字节
        等价。

        :param batch_no: 刚落盘的批号。
        """
        if self._cfg.console.mode_resolved == "rich":
            return
        if not sys.stderr.isatty() or self._cfg.tool.log_format == "jsonl":
            return
        sys.stderr.write(console_format.format_progress_line(
            batch_no, self._emitted_total, self._status_totals))
        sys.stderr.flush()
        self._progress_active = True

    def _end_progress(self) -> None:
        """收尾进度行：写过就补一个换行，让最后一帧留在滚动区里。"""
        if self._progress_active:
            sys.stderr.write("\n")
            sys.stderr.flush()
            self._progress_active = False

    def _print_summary(self, report: Mapping) -> None:
        """打印 run 摘要。

        v1.10（U21）：与 ``_progress`` 同一道 rich 静态闸（rich 模式换成渲染器的
        表格版，取值同源于本 report）；plain 路径写出的 ``console_format`` 行与
        v1.9 逐字节一致。

        :param report: 本次运行的 report 对象（只读 counts 块）。
        """
        if self._cfg.console.mode_resolved == "rich":
            return
        counts = dict(report.get("counts", {}))
        sys.stderr.write(
            "\n".join(console_format.format_summary_lines(counts)) + "\n")
        sys.stderr.flush()


def _step_row(transition, stitch_on: bool) -> dict:
    """`_meta.stream.steps` 的一条步记录。

    :param transition: 该步的 Transition。
    :param stitch_on: stitch 是否开启（决定是否带 resumed 列）。
    :returns: 步记录对象。
    """
    row = {"index": transition.index, **transition.action}
    if stitch_on:
        # v1.9（T10）：resumed = 该步是线程接缝占位——由 detail.kind 推导，绝不看
        # action_type。
        row["resumed"] = transition.detail.get("kind") == "thread_seam"
    return row


def _raw_payload(rec: Record) -> Mapping:
    """记录内容载荷。

    text 模态 → Record.raw；UI 模态 → 序列化树 + 图片路径；v1.8 序列记录
    （S25，§9.2）→ 成员 id/来源引用（kind="single" 保持冻结的单记录形态）。由
    annotate 关闭时的主输出与 rejects 的 `full` 档共用（§9.1/§9.2）。

    :param rec: 待取载荷的记录。
    :returns: 该记录的内容载荷对象。
    """
    if rec.kind == "sequence":
        return {
            "kind": "sequence",
            "member_ids": [m.id for m in rec.members],
            "member_sources": [_member_source(m) for m in rec.members],
        }
    if rec.modality == "text":
        return rec.raw or {}
    return {
        "ui_tree": rec.ui_tree.serialize() if rec.ui_tree is not None else "",
        "image_path": str(rec.image.path) if rec.image is not None else "",
    }


def _member_source(member: Record) -> dict:
    """`_meta.stream.member_sources` 的一个条目（§9.1）。

    形态为 {"file", ...} 加 line_no / pair_index 恰有其一——即 §9.1 的 source 块
    惯例逐成员套用。

    :param member: 成员记录。
    :returns: 该成员的来源条目。
    """
    src: dict = {"file": member.ref.source_file}
    if member.ref.line_no is not None:
        src["line_no"] = member.ref.line_no
    else:
        src["pair_index"] = member.ref.pair_index
    return src


def _order_key_repr(member: Record) -> str | int | None:
    """`_meta.stream.order_span` 的一个元素（spec §6.3）。

    即该成员的排序键呈现形式——text 模态为 "file:line_no"，UI 模态为 pair_index。

    :param member: 成员记录。
    :returns: 排序键呈现值。
    """
    ref = member.ref
    if ref.line_no is not None:
        return f"{ref.source_file}:{ref.line_no}"
    return ref.pair_index
