"""Pydantic 请求/响应模型。"""
from __future__ import annotations

import re
from datetime import date as dt_date
from datetime import datetime, timezone
from typing import Annotated, Literal
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


def ensure_aware(dt: datetime) -> datetime:
    """不带时区的时间一律按 UTC 解释。"""
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


class ExhibitCreate(BaseModel):
    id: str | None = Field(default=None, min_length=1, max_length=200,
                           description="展品 ID，不填则自动生成")
    name: str = Field(min_length=1, max_length=200, description="展品名称")
    dose_limit_luxh: float = Field(gt=0, description="累计光照剂量上限（lux·h）")
    max_interval_seconds: float = Field(gt=0, description="最大采样间隔（秒）")


class ExhibitOut(BaseModel):
    requested_id: str = Field(description="请求中使用的 ID（可能是别名）")
    canonical_id: str = Field(description="规范档案 ID；归并后所有别名均指向它")
    name: str
    dose_limit_luxh: float
    max_interval_seconds: float
    created_at: str


class ReadingIn(BaseModel):
    id: str = Field(min_length=1, max_length=200, description="读数 ID，幂等键")
    ts: datetime = Field(description="采样时刻，ISO 8601；缺省时区按 UTC 处理")
    lux: float = Field(ge=0, description="照度（lux）")
    source_id: str | None = Field(
        default=None, min_length=1, max_length=200,
        description="来源标识（传感器/采集器）；缺省表示未声明来源，"
                    "同一时刻不同来源的读数全部留存，由来源优先级版本仲裁权威值",
    )

    @field_validator("ts")
    @classmethod
    def _aware(cls, v: datetime) -> datetime:
        return ensure_aware(v)


class ReadingBatch(BaseModel):
    readings: list[ReadingIn] = Field(min_length=1, max_length=10000)


class ReadingResult(BaseModel):
    id: str
    status: str  # inserted | duplicate | conflict


# ---------- 照度上报存证（收据链） ----------

class ReceiptReadingOut(BaseModel):
    """收据中记录的请求读数（规范化：ts 统一为 UTC、'Z' 结尾的 ISO 8601）。"""

    id: str
    ts: str
    lux: float
    source_id: str | None = Field(
        default=None,
        description="上报时声明的来源标识；未声明来源的旧式上报不出现该键（收据中为 null）",
    )


class ReceiptRequestOut(BaseModel):
    readings: list[ReceiptReadingOut] = Field(description="本次上报请求中的读数（按提交顺序）")


class ReceiptOutcomeOut(BaseModel):
    inserted: int
    duplicates: int
    conflicts: int
    results: list[ReadingResult] = Field(description="每条读数的处理结果（与请求顺序一致）")


class ReceiptContentOut(BaseModel):
    """收据内容：计算 content_hash 的规范序列化对象。"""

    chain_id: str = Field(description="链标识：写入时的规范档案 ID")
    seq: int = Field(description="链内序号，从 1 连续递增")
    receipt_id: str
    requested_id: str = Field(description="上报请求中使用的展品 ID（可能是别名）")
    created_at: str
    request: ReceiptRequestOut
    outcome: ReceiptOutcomeOut


class ReceiptOut(BaseModel):
    chain_id: str = Field(description="链标识：写入时的规范档案 ID")
    seq: int = Field(description="链内序号，从 1 连续递增")
    receipt_id: str
    prev_hash: str = Field(description="上一张收据的链哈希；首张为 64 个 0（创世）")
    content_hash: str = Field(description="SHA-256(规范序列化内容)")
    chain_hash: str = Field(description="SHA-256(prev_hash + content_hash)")
    created_at: str
    content: ReceiptContentOut
    voids: list[ReceiptVoidOut] = Field(
        default_factory=list,
        description="该收据已完成的批次更正（作废标记）；更正不改写收据内容、序号与链哈希，"
                    "故标记放在 content 之外。一张收据至多有一条成功更正",
    )


class ReceiptListOut(BaseModel):
    requested_id: str = Field(description="请求中使用的展品 ID（可能是别名）")
    canonical_id: str = Field(description="规范档案 ID")
    chain_id: str = Field(description="收据链标识，即规范档案 ID")
    receipts: list[ReceiptOut]


class ChainReceiptsOut(BaseModel):
    chain_id: str
    receipts: list[ReceiptOut]


class ChainFailureOut(BaseModel):
    seq: int = Field(description="首个校验失败的收据序号")
    reason: str = Field(
        description="seq_gap（序号不连续）/ prev_hash_mismatch（断链）/ "
                    "content_hash_mismatch（内容不符）/ chain_hash_mismatch（链哈希不符）",
    )
    detail: str


class ChainVerifyOut(BaseModel):
    chain_id: str = Field(description="链标识（写入时的规范档案 ID）")
    ok: bool = Field(description="整链是否通过校验")
    checked: int = Field(description="已检查且通过的收据数量；失败时即首个失败位置之前的数量")
    total: int = Field(description="链上收据总数")
    first_failure: ChainFailureOut | None = Field(
        description="首个断链或内容不符的位置；通过时为 null",
    )


class IngestResponse(BaseModel):
    requested_id: str = Field(description="请求中使用的展品 ID（可能是别名）")
    canonical_id: str = Field(description="读数实际写入的规范档案 ID")
    inserted: int
    duplicates: int
    conflicts: int
    results: list[ReadingResult]
    receipt: ReceiptOut = Field(description="本次上报的存证收据（与读数同一事务生成）")


# ---------- 照度上报批次更正（作废） ----------

class ReadingCorrectionRequest(BaseModel):
    receipt_id: str = Field(min_length=1, max_length=200,
                            description="被更正批次的存证收据 receipt_id（上报响应中返回）")
    request_key: str = Field(min_length=1, max_length=200,
                             description="更正幂等键：同键重放首次结果，换目标（收据/档案/原因）→ 409")
    reason: str = Field(min_length=1, max_length=2000, description="作废原因")


class VoidedReadingOut(BaseModel):
    """一条被更正作废的读数（内容取自被更正收据记录的插入结果）。"""

    id: str = Field(description="读数 ID")
    ts: str = Field(description="采样时刻（UTC，ISO 8601，Z 结尾）")
    lux: float = Field(description="作废时读数记录的照度（lux）")


class ReceiptVoidOut(BaseModel):
    """收据上的作废标记：查询收据时附在 content 之外（content 与哈希永不改变）。"""

    request_key: str = Field(description="完成本次作废的更正 request_key")
    reason: str = Field(description="作废原因")
    voided_count: int = Field(description="该收据中被作废的读数条数")
    voided_readings: list[VoidedReadingOut] = Field(description="被作废的读数明细（按首次更正请求顺序去重）")
    voided_at: str = Field(description="作废时刻（UTC，ISO 8601，Z 结尾）")


class ReadingCorrectionOut(BaseModel):
    requested_id: str = Field(description="请求中使用的展品 ID（可能是别名）")
    canonical_id: str = Field(description="解析后的规范档案 ID（即被更正收据的链标识）")
    receipt_id: str = Field(description="被更正的存证收据 receipt_id")
    chain_id: str = Field(description="被更正收据的链标识（写入时的规范档案 ID）")
    seq: int = Field(description="被更正在其链内的序号")
    request_key: str = Field(description="本次更正的幂等键")
    reason: str
    voided_count: int = Field(description="本次实际作废的读数条数")
    voided_readings: list[VoidedReadingOut] = Field(
        description="本次作废的读数：仅该收据实际插入、作废时仍归属该档案且内容未变的读数",
    )
    voided_at: str = Field(description="作废时刻（UTC，ISO 8601，Z 结尾）")
    already_committed: bool = Field(
        default=False,
        description="false 表示本次首次落库；true 表示同一 request_key 重放，返回首次结果",
    )


class ReadingOut(BaseModel):
    id: str
    ts: str
    lux: float
    received_at: str
    source_id: str | None = Field(
        default=None, description="上报时声明的来源标识；未声明来源为 null",
    )


class ReadingListOut(BaseModel):
    requested_id: str
    canonical_id: str
    readings: list[ReadingOut]


# ---------- 照度采集质量诊断 ----------

class DiagnoseRequest(BaseModel):
    start: datetime = Field(description="诊断窗口起点（含），ISO 8601；缺省时区按 UTC 处理")
    end: datetime = Field(description="诊断窗口终点（含），ISO 8601；缺省时区按 UTC 处理")
    max_rate_lux_per_second: float = Field(
        gt=0,
        description="照度最大允许变化速率（lux/秒）；相邻读数间隔合规但 |Δlux|/Δt 超限记为 jump",
    )
    stuck_tolerance_lux: float = Field(
        ge=0,
        description="卡死容差（lux）：相邻读数照度差不超过该值才可能并入同一段卡死链",
    )
    min_stuck_seconds: float = Field(
        gt=0,
        description="卡死事件最短持续时长（秒）：平稳链首末读数累计时长达到该值才记为 stuck",
    )

    @field_validator("start", "end")
    @classmethod
    def _aware(cls, v: datetime) -> datetime:
        return ensure_aware(v)


class DiagnosisEventOut(BaseModel):
    type: Literal["gap", "jump", "stuck", "timestamp_conflict"]
    start: str = Field(description="事件起点（ISO 8601）；timestamp_conflict 起点与终点相同")
    end: str = Field(description="事件终点（ISO 8601），始终为事件在完整时间线上的原始边界")
    reading_ids: list[str] = Field(description="相关读数 ID：事件边界读数（stuck 为整段全部读数）")
    interval_seconds: float | None = Field(
        default=None,
        description="gap / jump：两条相邻读数的间隔（秒）",
    )
    rate_lux_per_second: float | None = Field(
        default=None,
        description="jump：照度变化速率 |Δlux|/Δt（lux/秒）",
    )
    duration_seconds: float | None = Field(
        default=None,
        description="stuck：首末读数累计持续时长（秒）",
    )
    lux_span: float | None = Field(
        default=None,
        description="stuck：整段照度最大值减最小值；timestamp_conflict：同时刻不同读数的照度跨度",
    )


class DiagnoseResponse(BaseModel):
    requested_id: str = Field(description="请求中使用的展品 ID（可能是别名）")
    canonical_id: str = Field(description="诊断所基于的规范档案 ID")
    start: str
    end: str
    max_interval_seconds: float = Field(description="展品配置的最大采样间隔（秒）：相邻读数间隔超过它记为 gap")
    max_rate_lux_per_second: float
    stuck_tolerance_lux: float
    min_stuck_seconds: float
    events: list[DiagnosisEventOut] = Field(
        description="与查询窗口相交的质量事件，按 (起点, 类型) 稳定排序；"
                    "事件边界始终基于规范档案的完整时间线，不随窗口缩窄而改变",
    )


class DoseResponse(BaseModel):
    requested_id: str = Field(description="请求中使用的展品 ID（可能是别名）")
    canonical_id: str = Field(description="剂量核算所基于的规范档案 ID")
    start: str
    end: str
    dose_limit_luxh: float
    known_dose_luxh: float = Field(description="所查窗口内的已知剂量（lux·h），缺测段未计入")
    remaining_luxh: float = Field(description="截至窗口末的全历史剩余额度（lux·h），越线时为负")
    exceeded: bool = Field(description="截至窗口末，全历史累计剂量是否已越线")
    first_exceeded_at: str | None = Field(description="全历史首次越线时刻（ISO 8601），不随查询窗口变化；截至窗口末未越线为 null")
    unknown_seconds: float = Field(description="窗口内未被有效积分段覆盖的时长（秒），不插值")
    covered_seconds: float = Field(description="窗口内被有效积分段覆盖的时长（秒）")
    reading_count: int = Field(description="窗口内（含端点）的读数条数，不含窗口外参与裁剪的相邻点")
    closed_seconds: float | None = Field(
        default=None,
        description="仅 calendar=display 时返回：窗口内处于闭馆时段的时长（秒），"
                    "与 covered_seconds、unknown_seconds 之和等于窗口时长",
    )


# ---------- 多来源读数仲裁（来源优先级版本 + 权威时间线） ----------

class SourcePriorityVersionIn(BaseModel):
    """发布一版来源优先级配置（追加式，版本只增不改）。"""

    effective_at: datetime = Field(
        description="生效时刻，ISO 8601；缺省时区按 UTC 处理。"
                    "对采样时刻 t 仲裁时取 effective_at <= t 的最新版本",
    )
    priorities: list[Annotated[str, Field(min_length=1, max_length=200)]] = Field(
        max_length=1000,
        description="来源标识按优先级从高到低排列；未列入的来源（含未声明来源）并列最低；"
                    "空列表表示全部来源平权",
    )
    expected_version: int = Field(
        ge=0,
        description="期望的当前最新版本号（尚无版本时为 0）；与服务器不一致返回 409",
    )

    @field_validator("effective_at")
    @classmethod
    def _aware(cls, v: datetime) -> datetime:
        return ensure_aware(v)

    @model_validator(mode="after")
    def _unique_priorities(self) -> SourcePriorityVersionIn:
        if len(set(self.priorities)) != len(self.priorities):
            raise ValueError("优先级列表中的来源标识重复")
        return self


class SourcePriorityVersionEntry(BaseModel):
    """一版已发布的来源优先级配置。"""

    version: int
    effective_at: str = Field(description="生效时刻（UTC，ISO 8601，Z 结尾）")
    priorities: list[str]
    created_at: str


class SourcePriorityVersionOut(SourcePriorityVersionEntry):
    """PUT 发布成功后的新版本。"""

    requested_id: str = Field(description="请求中使用的展品 ID（可能是别名）")
    canonical_id: str = Field(description="配置所属的规范档案 ID")


class SourcePriorityOut(BaseModel):
    """展品当前的来源优先级配置（全部版本）。"""

    requested_id: str = Field(description="请求中使用的展品 ID（可能是别名）")
    canonical_id: str = Field(description="配置所属的规范档案 ID")
    current_version: int = Field(description="当前最新版本号；尚无版本时为 0")
    versions: list[SourcePriorityVersionEntry]


class TimelineEntryOut(BaseModel):
    """权威时间线上的一个采样时刻：仲裁成功（ok）或保持冲突（conflict）。"""

    ts: str = Field(description="采样时刻（UTC，ISO 8601，Z 结尾）")
    status: Literal["ok", "conflict"]
    lux: float | None = Field(
        default=None, description="status=ok：该时刻的权威照度（lux）",
    )
    reading_ids: list[str] = Field(
        description="status=ok：胜出集合的读数 ID；status=conflict：并列最高优先级的读数 ID",
    )
    source_ids: list[str | None] = Field(
        description="与 reading_ids 一一对应的来源标识（未声明来源为 null）",
    )
    lux_span: float | None = Field(
        default=None, description="status=conflict：并列最高优先级读数的照度跨度",
    )
    priority_version: int | None = Field(
        default=None,
        description="该时刻生效的优先级版本号；无生效版本（平权仲裁）为 null",
    )


class TimelineOut(BaseModel):
    """权威时间线查询结果。"""

    requested_id: str = Field(description="请求中使用的展品 ID（可能是别名）")
    canonical_id: str = Field(description="时间线所属的规范档案 ID")
    start: str
    end: str
    arbitrated: bool = Field(
        description="展品是否已配置来源优先级版本（false 时全部读数平权，与旧口径一致）",
    )
    entries: list[TimelineEntryOut] = Field(
        description="窗口内（含端点）的仲裁结果，按采样时刻升序；"
                    "status=conflict 的时刻不进入剂量与诊断时间线",
    )


# ---------- 重复档案归并 ----------

class MergePreviewRequest(BaseModel):
    source_id: str = Field(min_length=1, max_length=200, description="被归并的来源档案 ID")
    target_id: str = Field(min_length=1, max_length=200, description="保留的目标档案 ID（名称与剂量配置以它为准）")


class MergeMetaDiff(BaseModel):
    field: str
    source: object = None
    target: object = None


class MergeCollision(BaseModel):
    reading_id: str
    source_ts: str
    source_lux: float
    target_ts: str
    target_lux: float
    kind: Literal["duplicate", "conflict"] = Field(
        description="duplicate：时间、照度一致，提交时自动去重；conflict：内容不同，必须逐条裁决",
    )


class MergePreviewOut(BaseModel):
    token: str = Field(description="版本令牌：绑定预演时刻两边元数据与读数集合，提交时校验")
    requested_source_id: str
    requested_target_id: str
    source_id: str = Field(description="解析后的规范来源 ID")
    target_id: str = Field(description="解析后的规范目标 ID")
    metadata: dict[str, MergeMetaDiff] = Field(description="名称与剂量配置差异，逐字段对比")
    source_reading_count: int
    target_reading_count: int
    collisions: list[MergeCollision] = Field(description="两边同读数 ID 的碰撞明细")
    auto_duplicate_count: int = Field(description="时间、照度一致、提交时自动去重的条数")
    conflict_count: int = Field(description="内容不同、必须逐条裁决的碰撞条数")
    source_only_count: int = Field(description="仅来源存在、将随档案搬迁的读数条数")
    source_priority_conflict: bool = Field(
        default=False,
        description="双方都已配置来源优先级版本且内容不一致：归并提交将被拒绝（409），"
                    "冲突不会静默覆盖；仅一方配置或双方一致时自动合并",
    )
    source_handover_count: int = Field(
        default=0,
        description="来源档案已固化的跨轮区域交接笔数",
    )
    target_handover_count: int = Field(
        default=0,
        description="目标档案已固化的跨轮区域交接笔数",
    )
    area_handover_conflict: bool = Field(
        default=False,
        description="双方都存在跨轮区域交接链：归并提交将被拒绝（409），"
                    "不改动任何交接记录；仅单侧有链时归并后整链以目标 ID 或别名可查",
    )
    ready_to_commit: bool = Field(description="是否不存在待裁决冲突（true 才可提交）")


class MergeResolution(BaseModel):
    reading_id: str = Field(min_length=1, max_length=200)
    action: Literal["keep_target", "keep_source", "rename_source"] = Field(
        description="keep_target：保留目标读数；keep_source：用来源内容覆盖目标；"
                    "rename_source：为来源读数指定新 ID 后保留",
    )
    new_id: str | None = Field(default=None, min_length=1, max_length=200,
                               description="action=rename_source 时必填，来源读数搬迁到目标档案后使用的新 ID")

    @model_validator(mode="after")
    def _require_new_id(self) -> MergeResolution:
        if self.action == "rename_source" and not self.new_id:
            raise ValueError("action=rename_source 时必须提供 new_id")
        return self


class MergeCommitRequest(BaseModel):
    token: str = Field(min_length=1, max_length=200)
    resolutions: list[MergeResolution] = Field(default_factory=list, description="逐条冲突裁决")


class MergeResolutionResult(BaseModel):
    reading_id: str
    action: str
    new_id: str | None = None


class MergeCommitOut(BaseModel):
    token: str
    source_id: str = Field(description="被归并的来源规范 ID（归并后成为 target_id 的永久别名）")
    target_id: str = Field(description="归并后保留的规范档案 ID")
    name: str = Field(description="目标档案名称（保持不变）")
    dose_limit_luxh: float = Field(description="目标档案剂量上限（保持不变）")
    max_interval_seconds: float = Field(description="目标档案最大采样间隔（保持不变）")
    moved_count: int = Field(description="仅来源存在、直接搬迁的读数条数")
    auto_deduplicated_count: int = Field(description="同 ID 同内容自动去重（丢弃来源副本）的条数")
    resolutions_applied: list[MergeResolutionResult]
    final_reading_count: int = Field(description="归并后目标档案读数总数")
    aliases: list[str] = Field(description="目标档案当前的全部别名（含本次来源 ID）")
    receipt_chains: list[str] = Field(
        default_factory=list,
        description="归并后原样保留的存证收据链标识（来源与目标的既有链），"
                    "可用 GET /receipts/chains/{chain_id}/verify 分别校验",
    )
    handover_chain_carried: bool = Field(
        default=False,
        description="true 表示单侧已有的跨轮区域交接链已整链改挂到目标规范档案，"
                    "归并后用目标 ID 或来源别名均可查询同一条链",
    )
    already_committed: bool = Field(description="true 表示本次为同令牌重放，返回的是首次提交结果")


# ---------- 展陈开放日历 ----------

_TIME_RE = re.compile(r"^(\d{2}):(\d{2})(?::(\d{2}))?$")


def parse_local_time(value: str) -> int:
    """把 "HH:MM" 或 "HH:MM:SS" 解析为本地当日秒偏移 [0, 86400)。"""
    m = _TIME_RE.match(value)
    if m is None:
        raise ValueError(f"时间格式须为 HH:MM 或 HH:MM:SS: {value!r}")
    hh, mm, ss = int(m.group(1)), int(m.group(2)), int(m.group(3) or 0)
    if hh > 23 or mm > 59 or ss > 59:
        raise ValueError(f"非法本地时刻: {value!r}")
    return hh * 3600 + mm * 60 + ss


def format_local_time(sec: int) -> str:
    """秒偏移格式化为 HH:MM（整分钟）或 HH:MM:SS。"""
    hh, rem = divmod(sec, 3600)
    mm, ss = divmod(rem, 60)
    return f"{hh:02d}:{mm:02d}" if ss == 0 else f"{hh:02d}:{mm:02d}:{ss:02d}"


class CalendarWindow(BaseModel):
    """一个开放时段；end 不晚于 start 表示跨午夜到次日同一本地时刻。"""

    start: str = Field(description="开始本地时刻，HH:MM 或 HH:MM:SS", examples=["09:00"])
    end: str = Field(description="结束本地时刻，HH:MM 或 HH:MM:SS；不晚于 start 表示跨午夜",
                     examples=["17:00"])

    @field_validator("start", "end")
    @classmethod
    def _valid_time(cls, v: str) -> str:
        parse_local_time(v)
        return v


class CalendarWeeklyWindow(CalendarWindow):
    weekday: int = Field(ge=1, le=7, description="本地星期几，ISO 8601：1=周一 … 7=周日")


class CalendarExceptionDay(BaseModel):
    """例外日：整体替换当日每周规则；windows 为空表示当日闭馆。"""

    date: dt_date = Field(description="本地日期，YYYY-MM-DD")
    windows: list[CalendarWindow] = Field(default_factory=list,
                                          description="当日开放时段；空列表表示当日闭馆")


class CalendarConfigIn(BaseModel):
    """整体设置展陈开放日历（PUT 语义：全量替换）。"""

    timezone: str = Field(min_length=1, max_length=100,
                          description="IANA 时区名，如 Asia/Shanghai、America/New_York")
    weekly: list[CalendarWeeklyWindow] = Field(default_factory=list,
                                               description="每周开放时段，可多个；重叠区间在展开时合并")
    exceptions: list[CalendarExceptionDay] = Field(
        default_factory=list,
        description="按本地日期替换当周规则的例外；同一日期只能出现一次",
    )

    @field_validator("timezone")
    @classmethod
    def _valid_timezone(cls, v: str) -> str:
        try:
            ZoneInfo(v)
        except Exception:
            raise ValueError(f"未知 IANA 时区: {v!r}")
        return v

    @model_validator(mode="after")
    def _unique_exception_dates(self) -> CalendarConfigIn:
        dates = [e.date for e in self.exceptions]
        if len(set(dates)) != len(dates):
            raise ValueError("例外日期重复：同一本地日期只能配置一次")
        return self


class CalendarWindowOut(BaseModel):
    start: str
    end: str


class CalendarWeeklyWindowOut(CalendarWindowOut):
    weekday: int


class CalendarExceptionDayOut(BaseModel):
    date: str
    windows: list[CalendarWindowOut]


class CalendarOut(BaseModel):
    requested_id: str = Field(description="请求中使用的展品 ID（可能是别名）")
    canonical_id: str = Field(description="日历所属的规范档案 ID")
    timezone: str
    weekly: list[CalendarWeeklyWindowOut]
    exceptions: list[CalendarExceptionDayOut]
    updated_at: str


# ---------- SQLite 在线快照 ----------

class SnapshotOut(BaseModel):
    """一份已发布的在线快照清单。"""

    seq: int = Field(description="快照编号，从 1 连续递增（仅对已发布快照）")
    created_at: str = Field(description="快照创建时间（UTC，ISO 8601，Z 结尾）")
    sha256: str = Field(description="快照数据库文件的 SHA-256（十六进制）")
    pages: int = Field(description="快照数据库页数（PRAGMA page_count）")
    bytes: int = Field(description="快照数据库文件字节数")
    download_url: str = Field(description="快照文件下载地址")


class SnapshotListOut(BaseModel):
    snapshots: list[SnapshotOut] = Field(
        description="按编号升序的已发布快照；失败或未完成的文件不会出现",
    )


class SnapshotCheckOut(BaseModel):
    """单项校验结果。"""

    name: Literal["sha256", "bytes", "page_count", "integrity_check", "foreign_key_check"]
    ok: bool = Field(description="该项是否通过")
    detail: dict[str, object] = Field(
        default_factory=dict,
        description="该项的明细：哈希/页数/字节数给出 expected 与 actual；"
                    "integrity_check 给出 result；foreign_key_check 给出 violations",
    )


class SnapshotVerifyOut(BaseModel):
    seq: int
    ok: bool = Field(description="所有校验项是否全部通过")
    snapshot: SnapshotOut
    checks: list[SnapshotCheckOut] = Field(
        description="逐项校验结果：sha256 / bytes / page_count / "
                    "integrity_check / foreign_key_check",
    )


# ---------- RFID 馆藏盘点对账 ----------

class InventoryExpectedAreaIn(BaseModel):
    """展品在本轮盘点中应当出现的区域。"""

    exhibit_id: str = Field(min_length=1, max_length=200,
                            description="展品 ID 或归并别名；提交时解析为规范档案 ID")
    area: str = Field(min_length=1, max_length=100, description="预期区域标识（如 A 区展柜）")


class InventoryReaderEventIn(BaseModel):
    """读头事件：某展品（RFID 标签）在某区域被读到一次及信号强度 RSSI。"""

    exhibit_id: str = Field(min_length=1, max_length=200,
                            description="被读到的展品 ID 或归并别名；提交时解析为规范档案 ID")
    area: str = Field(min_length=1, max_length=100, description="读头所属区域标识")
    ts: datetime = Field(description="读头事件时刻，ISO 8601；缺省时区按 UTC 处理。"
                                     "早于盘点开始时刻的事件不参与定位")
    rssi: float = Field(description="信号强度（dBm，数值越大越强）")

    @field_validator("ts")
    @classmethod
    def _aware(cls, v: datetime) -> datetime:
        return ensure_aware(v)


class ReconcileRequest(BaseModel):
    start_at: datetime = Field(description="盘点开始时刻，ISO 8601；缺省时区按 UTC 处理。"
                                           "固定窗口从该时刻起划分")
    window_seconds: float = Field(gt=0, description="固定时间窗长度（秒）；窗口下标 "
                                                    "floor((ts - start_at)/window_seconds)")
    rssi_delta_threshold: float = Field(
        ge=0,
        description="信号差阈值：最后一个有事件窗口内出现多个区域时，最强与次强 RSSI 之差 "
                    "不足该值（严格小于）记为位置不明；恰好等于仍可定位",
    )
    expected: list[InventoryExpectedAreaIn] = Field(
        default_factory=list, max_length=10000,
        description="展品预期区域清单；别名解析后同一规范档案出现多次 → 422",
    )
    events: list[InventoryReaderEventIn] = Field(
        default_factory=list, max_length=10000, description="读头事件列表（顺序不影响结果）",
    )

    @field_validator("start_at")
    @classmethod
    def _start_aware(cls, v: datetime) -> datetime:
        return ensure_aware(v)


class InventoryExpectationOut(BaseModel):
    requested_id: str = Field(description="请求中使用的展品 ID（可能是别名）")
    canonical_id: str = Field(description="解析后的规范档案 ID")
    expected_area: str


class InventoryMissingOut(BaseModel):
    canonical_id: str
    expected_area: str = Field(description="应当出现的区域；盘点全程无有效读头事件")


class InventoryMisplacedOut(BaseModel):
    canonical_id: str
    expected_area: str
    actual_area: str = Field(description="按最后一个有事件窗口判定出的实际区域")


class InventoryUnexpectedOut(BaseModel):
    canonical_id: str
    actual_area: str = Field(description="不在预期清单中但被读到，且位置可判定的区域")


class InventoryUnknownPositionOut(BaseModel):
    canonical_id: str
    expected_area: str | None = Field(
        description="预期区域；该展品不在预期清单中时为 null",
    )


class InventoryRoundOut(BaseModel):
    """一轮盘点对账的固化结果（GET 取回的形态）。"""

    round_id: str
    request_digest: str = Field(description="规范化请求摘要（SHA-256）：轮次与该摘要一一绑定")
    start_at: str = Field(description="规范化为 UTC、'Z' 结尾的 ISO 8601")
    window_seconds: float
    rssi_delta_threshold: float
    expected_count: int
    observed_count: int = Field(description="在盘点时间范围内出现过有效事件的规范档案数")
    event_count: int = Field(description="请求中的读头事件总数")
    events_used_count: int = Field(description="不早于开始时刻、参与定位的事件数")
    events_before_start_count: int = Field(description="早于盘点开始时刻、被忽略的事件数")
    expectations: list[InventoryExpectationOut] = Field(
        description="解析后的预期区域清单（按请求顺序）",
    )
    missing: list[InventoryMissingOut] = Field(description="缺失：预期但全程无有效事件，按规范 ID 排序")
    misplaced: list[InventoryMisplacedOut] = Field(description="错位：判定区域与预期区域不同，按规范 ID 排序")
    unexpected: list[InventoryUnexpectedOut] = Field(
        description="意外出现：不在预期清单中、位置可判定的展品，按规范 ID 排序",
    )
    unknown_position: list[InventoryUnknownPositionOut] = Field(
        description="位置不明：最后窗口多区域且最强/次强信号差不足阈值，按规范 ID 排序；"
                    "不在预期清单中的歧义展品也列入本组、expected_area 为 null",
    )
    reconciled_at: str = Field(description="结果固化时刻（UTC，ISO 8601，Z 结尾）")


class ReconcileResponse(InventoryRoundOut):
    already_committed: bool = Field(
        description="false 表示本次首次固化；true 表示相同规范化请求重放，返回的是首次结果",
    )


# ---------- 展品应急撤离编排 ----------

class EvacuationClosureIn(BaseModel):
    """一条通道的半开封闭时段：通道在 ``[start, end)`` 内不可进入。"""

    model_config = ConfigDict(populate_by_name=True)

    start: int = Field(ge=0, description="封闭开始相对秒（含）")
    end: int = Field(ge=0, description="封闭结束相对秒（不含）；end 秒整点起可进入")

    @model_validator(mode="after")
    def _start_before_end(self) -> EvacuationClosureIn:
        if self.end <= self.start:
            raise ValueError("封闭时段必须满足 start < end（半开 [start, end)）")
        return self


class EvacuationEdgeIn(BaseModel):
    """有向通道：from→to 的单向通行；通行秒数与并发容量均为正整数。"""

    model_config = ConfigDict(populate_by_name=True)

    from_node: str = Field(
        alias="from", validation_alias="from", serialization_alias="from",
        min_length=1, max_length=200, description="通道起点节点",
    )
    to_node: str = Field(
        alias="to", validation_alias="to", serialization_alias="to",
        min_length=1, max_length=200, description="通道终点节点",
    )
    travel_seconds: int = Field(
        validation_alias="travel_seconds", serialization_alias="travel_seconds",
        ge=1, description="通过该通道需要的整数秒数（进入 + 该秒数 = 离开）",
    )
    capacity: int = Field(
        ge=1, description="通道并发容量：同一时刻最多容纳的展品数；按 [进入, 离开) 计数",
    )
    closures: list[EvacuationClosureIn] = Field(
        default_factory=list,
        description="半开封闭时段列表（相对计划起点的整数秒区间 [start, end)）；"
                    "重叠或相接自动合并",
    )


class EvacuationExhibitIn(BaseModel):
    """待编排展品：ID 先经别名解析为规范档案；优先级越大越早编排。"""

    exhibit_id: str = Field(
        min_length=1, max_length=200,
        description="展品 ID 或归并别名；提交时解析为规范档案 ID",
    )
    start_node: str = Field(min_length=1, max_length=200, description="该展品所在起点节点")
    priority: int = Field(
        description="优先级（整数，越大越先编排）：按优先级降序、规范 ID 升序逐件编排",
    )


class EvacuationRequest(BaseModel):
    """展品应急撤离编排请求：一次给出计划图与全部待撤离展品。"""

    start_at: datetime = Field(
        description="计划起点（绝对时刻，ISO 8601）；缺省时区按 UTC 处理。"
                    "图中的封闭时段均为相对该时刻的秒偏移",
    )
    nodes: list[Annotated[str, Field(min_length=1, max_length=200)]] = Field(
        min_length=1, max_length=10000, description="全部节点标识（起点、通道端点与出口）",
    )
    edges: list[EvacuationEdgeIn] = Field(
        default_factory=list, max_length=100000, description="有向通道列表",
    )
    exits: list[Annotated[str, Field(min_length=1, max_length=200)]] = Field(
        min_length=1, max_length=10000, description="出口节点列表（必须全部存在于 nodes）",
    )
    exhibits: list[EvacuationExhibitIn] = Field(
        min_length=1, max_length=10000, description="待撤离展品列表",
    )

    @field_validator("start_at")
    @classmethod
    def _aware(cls, v: datetime) -> datetime:
        return ensure_aware(v)

    @model_validator(mode="after")
    def _validate_graph(self) -> EvacuationRequest:
        # 节点不可重复
        seen_nodes: set[str] = set()
        dup_nodes: set[str] = set()
        for n in self.nodes:
            if n in seen_nodes:
                dup_nodes.add(n)
            seen_nodes.add(n)
        if dup_nodes:
            raise ValueError(f"节点重复: {sorted(dup_nodes)}")
        node_set = seen_nodes
        # 出口必须是已知节点且不可重复
        if len(self.exits) != len(set(self.exits)):
            raise ValueError("出口节点重复")
        unknown_exits = sorted(set(self.exits) - node_set)
        if unknown_exits:
            raise ValueError(f"出口不在节点列表中: {unknown_exits}")
        # 通道端点必须已知；同一有向通道（from, to）只能定义一次
        seen_edges: set[tuple[str, str]] = set()
        for e in self.edges:
            if e.from_node not in node_set:
                raise ValueError(f"通道起点不在节点列表中: {e.from_node}")
            if e.to_node not in node_set:
                raise ValueError(f"通道终点不在节点列表中: {e.to_node}")
            if (e.from_node, e.to_node) in seen_edges:
                raise ValueError(f"有向通道重复定义: {e.from_node} -> {e.to_node}")
            seen_edges.add((e.from_node, e.to_node))
        # 展品起点必须已知
        for x in self.exhibits:
            if x.start_node not in node_set:
                raise ValueError(f"展品 {x.exhibit_id} 的起点不在节点列表中: {x.start_node}")
        return self


class EvacuationLegOut(BaseModel):
    """一段通道的编排结果：``enter_at`` 进入、``leave_at`` 离开（=进入+通行秒数）。"""

    model_config = ConfigDict(populate_by_name=True)

    from_node: str = Field(alias="from", serialization_alias="from")
    to_node: str = Field(alias="to", serialization_alias="to")
    travel_seconds: int
    enter_offset_seconds: int = Field(description="进入时刻相对计划起点的整数秒")
    leave_offset_seconds: int = Field(description="离开时刻相对计划起点的整数秒（半开占用右端点）")
    enter_at: str = Field(description="进入时刻（UTC，ISO 8601，Z 结尾）")
    leave_at: str = Field(description="离开时刻（UTC，ISO 8601，Z 结尾）；此时刻起通道释放一份容量")
    wait_seconds: int = Field(
        description="进入该通道前的等待秒数（到达通道起点的最早时刻到实际进入之间）",
    )


class EvacuationExhibitOut(BaseModel):
    """单件展品的撤离编排结果。"""

    requested_id: str = Field(description="请求中使用的展品 ID（可能是别名）")
    canonical_id: str = Field(description="别名解析后的规范档案 ID")
    start_node: str
    priority: int
    exit_node: str = Field(description="时变最早到达搜索选出的出口")
    path: list[str] = Field(description="完整节点路径（含起点与出口）")
    legs: list[EvacuationLegOut] = Field(description="逐段通道编排（与 path 相邻节点对应）")
    wait_seconds: int = Field(description="全路径累计等待秒数")
    completion_offset_seconds: int = Field(
        description="总完成时间：到达出口时刻相对计划起点的整数秒",
    )
    completed_at: str = Field(description="到达出口时刻（UTC，ISO 8601，Z 结尾）")


class EvacuationResponse(BaseModel):
    """应急撤离编排响应。

    编排为纯只读计算：不写照度读数、不写日历、不写盘点轮次，也不生成或改写
    存证收据链。
    """

    start_at: str = Field(description="计划起点（规范化为 UTC，ISO 8601，Z 结尾）")
    results: list[EvacuationExhibitOut] = Field(
        description="逐件编排结果，按优先级降序、规范 ID 升序排列",
    )
    total_completion_offset_seconds: int = Field(
        description="全部展品撤离完成的总完成时间（最晚一件到达出口的相对秒）",
    )


# ---------- 照度异常处置单 ----------

class IlluminationTicketCreate(BaseModel):
    """创建照度异常处置单：处置时间窗 + 诊断阈值 + 创建幂等键。"""

    start: datetime = Field(description="处置窗口起点（含），ISO 8601；缺省时区按 UTC 处理")
    end: datetime = Field(description="处置窗口终点（含），ISO 8601；缺省时区按 UTC 处理")
    max_rate_lux_per_second: float = Field(
        gt=0, description="照度最大允许变化速率（lux/秒），与诊断接口同口径",
    )
    stuck_tolerance_lux: float = Field(
        ge=0, description="卡死容差（lux），与诊断接口同口径",
    )
    min_stuck_seconds: float = Field(
        gt=0, description="卡死事件最短持续时长（秒），与诊断接口同口径",
    )
    request_key: str = Field(
        min_length=1, max_length=200,
        description="创建幂等键：相同 request_key 且目标一致重放首次结果；换目标 → 409",
    )

    @field_validator("start", "end")
    @classmethod
    def _aware(cls, v: datetime) -> datetime:
        return ensure_aware(v)


class TicketWindowOut(BaseModel):
    start: str
    end: str


class TicketThresholdsOut(BaseModel):
    max_rate_lux_per_second: float
    stuck_tolerance_lux: float
    min_stuck_seconds: float


class TicketDoseOut(BaseModel):
    """快照中按 calendar=display 口径重算的剂量。"""

    dose_limit_luxh: float
    known_dose_luxh: float = Field(description="窗口内已知剂量（lux·h）")
    remaining_luxh: float = Field(description="截至窗口末、按开放历史累计的剩余额度（lux·h）")
    exceeded: bool = Field(description="截至窗口末开放历史累计是否已越线")
    first_exceeded_at: str | None = Field(description="全历史首次越线时刻；未越线为 null")
    unknown_seconds: float
    covered_seconds: float
    closed_seconds: float = Field(description="窗口内闭馆时长（秒）")
    reading_count: int


class TicketChainTailOut(BaseModel):
    """固化时点的存证收据链末端。"""

    chain_id: str = Field(description="链标识，即规范档案 ID")
    seq: int = Field(description="末端收据序号；链为空为 0")
    chain_hash: str = Field(description="末端收据链哈希；链为空为 64 个 0（创世衔接值）")


class TicketSnapshotOut(BaseModel):
    """处置单创建时固化的快照（不可变）；digest 即对本对象规范序列化的 SHA-256。"""

    canonical_id: str
    window: TicketWindowOut
    max_interval_seconds: float
    thresholds: TicketThresholdsOut
    events: list[DiagnosisEventOut] = Field(
        description="按未作废权威时间线重算、与处置窗口相交的诊断事件（与诊断接口同口径）",
    )
    dose: TicketDoseOut = Field(description="calendar=display 口径重算的剂量")
    receipt_chain: TicketChainTailOut = Field(description="收据链末端哈希")
    readings_fingerprint: str = Field(description="未作废读数集合指纹：读数或作废变化即变")
    source_priority_fingerprint: str = Field(description="来源优先级版本配置指纹")
    calendar_fingerprint: str = Field(description="展陈开放日历配置指纹")


class TicketEvidenceOut(BaseModel):
    """resolved 结案时登记的涉及收据 / 批次更正引用。"""

    receipt_ids: list[str] = Field(description="涉及的存证收据 receipt_id（本规范档案链上）")
    correction_request_keys: list[str] = Field(description="涉及的批次更正 request_key（本规范档案）")


class IlluminationTicketOut(BaseModel):
    ticket_id: str
    request_key: str = Field(description="创建幂等键")
    requested_id: str = Field(description="创建请求中使用的展品 ID（可能是别名）")
    canonical_id: str = Field(description="创建时解析出的规范档案 ID")
    start: str = Field(description="处置窗口起点（UTC，ISO 8601，Z 结尾）")
    end: str = Field(description="处置窗口终点（UTC，ISO 8601，Z 结尾）")
    max_rate_lux_per_second: float
    stuck_tolerance_lux: float
    min_stuck_seconds: float
    status: Literal["open", "resolved", "accepted"] = Field(
        description="处置单状态：只能从 open 结案为 resolved 或 accepted",
    )
    digest: str = Field(description="创建时固化快照的摘要（SHA-256），不可变")
    snapshot: TicketSnapshotOut = Field(description="创建时固化的快照，落库后绝不改写")
    created_at: str
    decision: Literal["resolved", "accepted"] | None = Field(default=None, description="结案决定")
    closed_at: str | None = Field(default=None, description="结案时刻（UTC，ISO 8601，Z 结尾）")
    close_note: str | None = Field(default=None, description="结案说明；accepted 时即保留事件的理由")
    evidence: TicketEvidenceOut | None = Field(
        default=None, description="resolved 结案登记的涉及收据 / 批次更正引用",
    )
    closed_digest: str | None = Field(
        default=None,
        description="结案时重算核对的摘要；与结案请求 expected_digest 一致才允许结案",
    )
    current_digest: str | None = Field(
        default=None,
        description="仅 GET 单张且状态为 open 时返回：按当前状态实时重算的摘要，"
                    "用作结案的 expected_digest；无法重算（如日历已删除）为 null",
    )
    already_committed: bool = Field(
        default=False,
        description="true 表示相同 request_key 重放，返回的是首次创建的原件",
    )


class IlluminationTicketListOut(BaseModel):
    requested_id: str
    canonical_id: str
    tickets: list[IlluminationTicketOut]


class IlluminationTicketCloseRequest(BaseModel):
    """结案请求：只能从 open 结案为 resolved 或 accepted。"""

    expected_digest: str = Field(
        min_length=1, max_length=64,
        description="结案时核对的摘要：须与服务端当前重算结果一致，"
                    "读数/作废/来源优先级/日历/收据链变化导致不一致 → 409 不写入",
    )
    decision: Literal["resolved", "accepted"] = Field(
        description="resolved：异常已处置（须举证）；accepted：保留事件并记录理由",
    )
    note: str = Field(min_length=1, max_length=2000,
                      description="结案说明；accepted 时即保留事件的理由")
    receipt_ids: list[Annotated[str, Field(min_length=1, max_length=200)]] = Field(
        default_factory=list, max_length=1000,
        description="resolved 必填其一：涉及的存证收据 receipt_id（须属于本规范档案的收据链）",
    )
    correction_request_keys: list[Annotated[str, Field(min_length=1, max_length=200)]] = Field(
        default_factory=list, max_length=1000,
        description="resolved 必填其一：涉及的批次更正 request_key（须属于本规范档案）",
    )


class IlluminationTicketCloseOut(IlluminationTicketOut):
    events_cleared: bool | None = Field(
        default=None,
        description="resolved 结案证明：重算后原诊断事件是否全部消失（结案要求为 true）",
    )
    dose_exceeded: bool | None = Field(
        default=None,
        description="resolved 结案证明：重算后 calendar=display 剂量是否越限（结案要求为 false）",
    )


# ---------- 展厅开放批次合规核验 ----------

class ComplianceCheckRequest(BaseModel):
    """展厅开放批次合规核验请求：时间窗 + 盘点轮次 + 展品清单 + 诊断阈值 + 幂等键。"""

    request_key: str = Field(
        min_length=1, max_length=200,
        description="核验幂等键：仅在规范化输入与当时证据摘要都一致时重放首次凭证；"
                    "任一不同 → 409；核验失败不落库、不占用该键",
    )
    round_id: str = Field(min_length=1, max_length=200,
                          description="已固化的 RFID 盘点轮次 ID（须已完成对账）")
    start: datetime = Field(description="核验窗口起点（含），ISO 8601；缺省时区按 UTC 处理")
    end: datetime = Field(description="核验窗口终点（含），ISO 8601；缺省时区按 UTC 处理")
    exhibit_ids: list[Annotated[str, Field(min_length=1, max_length=200)]] = Field(
        min_length=1, max_length=10000,
        description="待核验展品 ID 清单（可用归并别名，提交时解析为规范档案；"
                    "解析后同一规范档案出现多次 → 422）",
    )
    max_rate_lux_per_second: float = Field(
        gt=0, description="照度最大允许变化速率（lux/秒），与诊断接口同口径",
    )
    stuck_tolerance_lux: float = Field(
        ge=0, description="卡死容差（lux），与诊断接口同口径",
    )
    min_stuck_seconds: float = Field(
        gt=0, description="卡死事件最短持续时长（秒），与诊断接口同口径",
    )

    @field_validator("start", "end")
    @classmethod
    def _aware(cls, v: datetime) -> datetime:
        return ensure_aware(v)


class ComplianceLocationOut(BaseModel):
    """一件展品在盘点轮次固化结果中的定位结论。"""

    status: Literal["located", "missing", "misplaced", "unknown_position",
                    "unexpected", "not_covered"] = Field(
        description="located：定位到预期区域（唯一合规）；missing：缺失；misplaced：错位；"
                    "unknown_position：位置不明；unexpected：不在预期清单但被读到；"
                    "not_covered：轮次既无预期也无观测记录",
    )
    expected_area: str | None = Field(description="轮次中的预期区域；无预期记录为 null")
    actual_area: str | None = Field(description="轮次判定的实际区域；定位成功（含错位）时给出")


class ComplianceReasonOut(BaseModel):
    """一条不合规原因（逐件给出，一件可有多条）。"""

    code: Literal["inventory_missing", "inventory_misplaced",
                  "inventory_unknown_position", "inventory_unexpected",
                  "inventory_not_covered", "calendar_missing", "dose_exceeded",
                  "diagnosis_events", "open_tickets"] = Field(
        description="原因代码：盘点定位 / 未配置日历 / 开放剂量越限 / "
                    "窗口内 gap-jump-stuck 事件 / 未结处置单",
    )
    detail: str = Field(description="原因说明（含具体区域、事件统计、处置单 ID 等）")


class ComplianceDoseOut(BaseModel):
    """截至窗口末按 calendar=display 口径重算的剂量证据摘要。"""

    dose_limit_luxh: float
    known_dose_luxh: float = Field(description="窗口内已知剂量（lux·h）")
    remaining_luxh: float = Field(description="截至窗口末、按开放历史累计的剩余额度（lux·h）")
    exceeded: bool = Field(description="截至窗口末开放历史累计是否已越线（合规要求 false）")
    first_exceeded_at: str | None = Field(description="全历史首次越线时刻；未越线为 null")
    unknown_seconds: float
    covered_seconds: float
    closed_seconds: float = Field(description="窗口内闭馆时长（秒）")
    reading_count: int


class ComplianceExhibitOut(BaseModel):
    """一件展品的核验结果与证据摘要。"""

    requested_id: str = Field(description="请求清单中使用的展品 ID（可能是别名）")
    canonical_id: str = Field(description="别名解析后的规范档案 ID")
    compliant: bool = Field(description="该件是否通过全部核验项")
    reasons: list[ComplianceReasonOut] = Field(description="不合规原因（合规时为空）")
    location: ComplianceLocationOut = Field(description="盘点轮次固化结果中的定位结论")
    dose: ComplianceDoseOut | None = Field(
        description="截至窗口末的 calendar=display 剂量证据；未配置展陈日历为 null",
    )
    events: list[DiagnosisEventOut] = Field(
        description="完整时间线上与窗口相交的诊断事件（与诊断接口同口径）；"
                    "合规判定只拦截 gap / jump / stuck，timestamp_conflict 仅记录",
    )
    open_ticket_ids: list[str] = Field(
        description="该规范档案当前未结（open）的照度异常处置单 ID（合规要求为空）",
    )


class ComplianceCheckOut(BaseModel):
    """批次合规核验结果；合规时即固化的合规凭证。"""

    request_key: str = Field(description="核验幂等键")
    round_id: str = Field(description="核验依据的盘点轮次 ID")
    round_digest: str = Field(description="该轮次固化时绑定的规范化请求摘要（SHA-256）")
    start: str = Field(description="核验窗口起点（UTC，ISO 8601，Z 结尾）")
    end: str = Field(description="核验窗口终点（UTC，ISO 8601，Z 结尾）")
    status: Literal["compliant", "non_compliant"] = Field(
        description="compliant：全部通过且凭证已固化；non_compliant：存在不合规项，未写入任何记录",
    )
    credential_id: str | None = Field(description="合规凭证 ID；non_compliant 为 null")
    digest: str | None = Field(
        description="凭证摘要：规范化输入 + 各项证据摘要的规范序列化 SHA-256；"
                    "non_compliant 为 null",
    )
    exhibits: list[ComplianceExhibitOut] = Field(
        description="逐件核验结果与证据摘要（按请求清单顺序）",
    )
    created_at: str | None = Field(description="凭证固化时刻（UTC，ISO 8601，Z 结尾）；non_compliant 为 null")
    already_committed: bool = Field(
        default=False,
        description="true 表示相同 request_key 且规范化输入与当时摘要一致的重放，"
                    "返回的是首次固化的凭证",
    )


# ---------- 跨盘点轮次的馆藏区域交接 ----------

class AreaHandoverRequest(BaseModel):
    """跨盘点轮次的馆藏区域交接请求。"""

    exhibit_id: str = Field(min_length=1, max_length=200,
                            description="交接展品 ID 或归并别名；提交时解析为规范档案")
    source_round_id: str = Field(min_length=1, max_length=200,
                                 description="来源盘点轮次 ID（须已固化对账）")
    target_round_id: str = Field(min_length=1, max_length=200,
                                 description="接收盘点轮次 ID（须已固化对账，且起始时间晚于来源轮）")
    source_area: str = Field(min_length=1, max_length=100,
                             description="展品在来源轮中明确定位的区域（须与该轮预期/定位一致）")
    target_area: str = Field(min_length=1, max_length=100,
                             description="展品在接收轮中明确定位的区域（须与该轮预期/定位一致）")


class AreaHandoverOut(BaseModel):
    """一笔已固化的跨轮区域交接（交接链中的一环）。"""

    seq: int = Field(description="交接链内序号，从 1 连续递增；链按盘点轮次时间向前续接")
    source_round_id: str
    target_round_id: str
    source_area: str = Field(description="来源轮明确定位的区域（交接证据）")
    target_area: str = Field(description="接收轮明确定位的区域（交接证据）")
    source_round_start_at: str = Field(description="来源轮盘点起始时刻（UTC，ISO 8601，Z 结尾）")
    target_round_start_at: str = Field(description="接收轮盘点起始时刻（UTC，ISO 8601，Z 结尾）")
    source_round_digest: str = Field(description="来源轮固化时绑定的规范化请求摘要（SHA-256）")
    target_round_digest: str = Field(description="接收轮固化时绑定的规范化请求摘要（SHA-256）")
    created_at: str = Field(description="交接固化时刻（UTC，ISO 8601，Z 结尾）")


class AreaHandoverResponse(AreaHandoverOut):
    requested_id: str = Field(description="请求中使用的展品 ID（可能是别名）")
    canonical_id: str = Field(description="别名解析后的规范档案 ID")


class AreaHandoverChainOut(BaseModel):
    """展品当前区域与按轮次时间排序的交接历史。"""

    requested_id: str = Field(description="请求中使用的展品 ID（可能是别名）")
    canonical_id: str = Field(description="别名解析后的规范档案 ID")
    current_round_id: str | None = Field(
        description="当前去向所在的接收轮 ID；从未交接为 null",
    )
    current_area: str | None = Field(
        description="当前区域（最后一笔交接的接收轮定位区域）；从未交接为 null",
    )
    history: list[AreaHandoverOut] = Field(
        description="交接历史，按交接序号（= 盘点轮次时间）升序；首次交接以来源轮建链",
    )
