from pydantic import BaseModel, Field
from typing import Dict, List, Literal, Optional
from datetime import datetime


class YearlyData(BaseModel):
    model_config = {"from_attributes": True}

    fiscal_year: int
    revenue: Optional[int] = None
    cost_of_sales: Optional[int] = None
    gross_profit: Optional[int] = None
    sga: Optional[int] = None
    operating_profit: Optional[int] = None
    net_income: Optional[int] = None
    total_assets: Optional[int] = None
    total_liabilities: Optional[int] = None
    equity: Optional[int] = None
    cash: Optional[int] = None
    # 손익 세부 항목 (Phase 96) — 미수집 시 None
    other_income: Optional[int] = None
    other_expense: Optional[int] = None
    finance_income: Optional[int] = None
    finance_cost: Optional[int] = None
    income_before_tax: Optional[int] = None
    income_tax_expense: Optional[int] = None
    operating_cash_flow: Optional[int] = None
    # Financial-sector metrics collected in Phase 90. Non-financial rows stay None.
    net_interest_income: Optional[int] = None
    loan_loss_provision: Optional[int] = None
    insurance_liability: Optional[int] = None
    sector_detail: Optional[str] = None
    source_report: Optional[str] = None
    # 차트용 계산 지표 (단위: %)
    operating_margin: Optional[float] = None      # 영업이익률
    net_margin: Optional[float] = None            # 순이익률
    revenue_growth_rate: Optional[float] = None   # 전년 대비 매출 성장률


GrowthStatus = Literal["양호", "보합", "부진", "데이터 부족"]
StabilityStatus = Literal["우수", "주의", "위험", "데이터 부족"]
ProfitabilityStatus = Literal["개선", "유지", "부진", "데이터 부족"]


class Metrics(BaseModel):
    """최신 연도 기준 계산된 재무 지표 (단위: %)"""
    revenue_growth_rate: Optional[float] = None   # 전년 대비 매출 성장률(%)
    net_income_growth_rate: Optional[float] = None  # 전년 대비 순이익 성장률(%)
    operating_margin: Optional[float] = None       # 영업이익률(%)
    net_margin: Optional[float] = None             # 순이익률(%)
    roe: Optional[float] = None                    # 자기자본이익률(%)
    roa: Optional[float] = None                    # 총자산이익률(%)
    debt_ratio: Optional[float] = None             # 부채비율(%)


class InterpretationPoint(BaseModel):
    type: str
    severity: str
    title: str
    message: str
    metric_refs: Dict[str, Optional[float]] = {}


class SectorRank(BaseModel):
    revenue_rank: Optional[int] = None
    operating_margin_rank: Optional[int] = None
    total_companies: int = 0


class CompanySummary(BaseModel):
    company_name: str
    stock_code: str
    fiscal_year: int
    sector: Optional[str] = None
    is_financial_sector: bool
    # 프론트 표기용 메타 필드 (Phase 96)
    industry_type: str = "general"                # general | bank | insurance | securities | financial
    revenue_label: Optional[str] = None           # 비금융 "매출액", 금융업 None
    operating_profit_label: Optional[str] = None  # 비금융 "영업이익", 금융업 None
    summary_text: str
    growth_status: GrowthStatus
    stability_status: StabilityStatus
    profitability_status: ProfitabilityStatus
    metrics: Optional[Metrics] = None  # _build_company_summary() always sets compute_metrics().
    warning_flags: List[str] = []
    interpretation_points: List[InterpretationPoint] = []
    history: List[YearlyData]
    sector_avg: Optional[Metrics] = None
    sector_rank: Optional[SectorRank] = None


class TrendResponse(BaseModel):
    stock_code: str
    company_name: str
    unit: str
    history: List[YearlyData]


class CompanyDetailResponse(BaseModel):
    summary: CompanySummary
    trend: TrendResponse
    collection_status: Optional[str] = None
    collection_updated_at: Optional[datetime] = None


class AIAnalysisResponse(BaseModel):
    stock_code: str
    company_name: str
    prediction: float = Field(..., description="다음해 예측값 (원. 비금융=영업이익, 금융=당기순이익)")
    base_year: int = Field(..., description="예측의 기준이 된 최신 실제 사업연도")
    forecast_year: int = Field(..., description="예측 대상 연도 (base_year + 1)")
    summary: Optional[str] = Field(
        None,
        description="GPT 자연어 요약 (3줄 요약·장점3·위험3). 요약 미생성 시 null.",
    )
    summary_available: bool = Field(
        True,
        description="요약 생성 성공 여부. False면 OpenAI 미설정/호출 실패로 예측만 제공.",
    )
    is_financial: bool = Field(
        False,
        description="금융사 여부. True면 prediction은 당기순이익, False면 영업이익",
    )
    prediction_label: str = Field(
        "영업이익",
        description="prediction 라벨. 금융=당기순이익, 비금융=영업이익",
    )
    prediction_display_text: str = Field(
        ..., description="예측값 표시 문구 — AI 프롬프트에 그대로 전달한 문자열",
    )
    prediction_sentence: str = Field(
        ..., description="요약 없이도 쓸 수 있는 결정적 예측 문장",
    )


class CompanySearchItem(BaseModel):
    company_name: str
    stock_code: str
    sector: Optional[str] = None
    is_collected: bool = False


class CollectStartResponse(BaseModel):
    status: Literal["started", "already_processing"]
    resolved_code: str


class CollectRefreshResponse(BaseModel):
    status: Literal["refreshing", "already_processing"]
    resolved_code: str


class CollectStatusResponse(BaseModel):
    status: Literal["none", "pending", "processing", "completed", "failed"]
    message: Optional[str] = None
    started_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None


# ── 비교 기능 스키마 ───────────────────────────────────────────────────────────


class CompanyCompareItem(BaseModel):
    company_name: str
    stock_code: str
    fiscal_year: int
    sector: Optional[str] = None
    is_financial_sector: bool
    metrics: Metrics
    history: List[YearlyData]


class CompareResponse(BaseModel):
    companies: List[CompanyCompareItem]
    compared_at: datetime


class CompareInsightResponse(BaseModel):
    companies: List[CompanyCompareItem]
    insight: Optional[str] = Field(None, description="AI comparative insight. Null when unavailable.")
    insight_available: bool = Field(..., description="Whether AI insight generation succeeded.")
    compared_at: datetime


class CompareShareResponse(BaseModel):
    share_id: str
    expires_at: datetime


class SimilarCompanyItem(BaseModel):
    stock_code: str
    company_name: str
    sector: Optional[str] = None
    fiscal_year: int
    revenue: Optional[int] = None
    operating_profit: Optional[int] = None
    operating_margin: Optional[float] = None
    growth_status: str
    profitability_status: str
    stability_status: str
    match_count: int


class RecommendedCompanyItem(BaseModel):
    stock_code: str
    company_name: str
    sector: Optional[str] = None
    fiscal_year: int
    revenue: Optional[int] = None
    operating_profit: Optional[int] = None
    operating_margin: Optional[float] = None
    growth_status: str
    profitability_status: str
    stability_status: str
    healthy_count: int


class CollectedCompany(BaseModel):
    company_name: str
    stock_code: str
    fiscal_year: int
    sector: Optional[str] = None
    collected_at: Optional[datetime] = None


class PopularCompany(BaseModel):
    company_name: str
    stock_code: str
    search_count: int


# ── 인기 검색 (window 기반) 스키마 ────────────────────────────────────────────

PopularWindow = Literal["realtime", "daily", "weekly"]


class PopularCompanyItem(BaseModel):
    keyword: str
    count: int
    rank: int
    updated_at: Optional[datetime] = None


class PopularSearchResponse(BaseModel):
    items: List[PopularCompanyItem]
    window: PopularWindow
    updated_at: Optional[datetime] = None
    generated_at: Optional[datetime] = None


# ── 일괄 수집 스키마 ──────────────────────────────────────────────────────────

class BatchCollectItem(BaseModel):
    keyword: str
    status: Literal["started", "already_processing", "error"]
    resolved_code: Optional[str] = None
    detail: Optional[str] = None


class BatchCollectResponse(BaseModel):
    results: List[BatchCollectItem]


# ── 삭제 스키마 ──────────────────────────────────────────────────────────────

class DeleteCompanyResponse(BaseModel):
    status: Literal["deleted"]
    stock_code: str


# ── 배치 상태 조회 스키마 ─────────────────────────────────────────────────────

class BatchStatusItem(BaseModel):
    keyword: str
    resolved_code: Optional[str] = None
    status: Literal["none", "pending", "processing", "completed", "failed", "error"]
    message: Optional[str] = None
    started_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None
    detail: Optional[str] = None  # 기업 조회 실패 시 오류 내용


# ── AI 해석 API 스키마 (Phase 97) ─────────────────────────────────────────────
# evidence 필드명은 프론트 모듈(analyzeFinancialInsights)의 camelCase를 변환 없이 수용한다.

class InsightEvidenceItem(BaseModel):
    label: str = Field(..., max_length=50)
    previousValue: Optional[float] = None
    currentValue: Optional[float] = None
    changeRate: Optional[float] = None


class InsightFlag(BaseModel):
    id: str = Field(..., max_length=60, description="rule ID (11종 화이트리스트, 목록 외 400)")
    title: str = Field(..., max_length=200)
    year: int
    evidence: List[InsightEvidenceItem] = Field(default_factory=list, max_length=10)


class InsightInterpretRequest(BaseModel):
    stock_code: str = Field(..., pattern=r"^\d{6}$")
    company: str = Field(..., max_length=50)
    latest_year: int
    warning_flags: List[InsightFlag] = Field(default_factory=list, max_length=8)
    positive_flags: List[InsightFlag] = Field(default_factory=list, max_length=8)


class InsightInterpretation(BaseModel):
    headline: str = Field(..., description="가장 먼저 볼 변화 요약 (1~2문장)")
    positive: Optional[str] = Field(None, description="긍정적으로 볼 흐름. 신호 없으면 null")
    caution: Optional[str] = Field(None, description="주의해서 볼 흐름. 신호 없으면 null")
    check_items: List[str] = Field(default_factory=list, description="추가 확인이 필요한 재무 항목")


class InsightInterpretResponse(BaseModel):
    stock_code: str
    interpretation: Optional[InsightInterpretation] = Field(
        None, description="AI 해석. interpretation_available=false면 null",
    )
    interpretation_available: bool = Field(
        ..., description="false면 OpenAI 미설정/호출 실패/일일 한도 초과 — 프론트 폴백 UI 처리",
    )


# ── 공시 검색 스키마 (Phase 98) ───────────────────────────────────────────────

class DisclosureItem(BaseModel):
    title: str = Field(..., description="공시 제목 (report_nm)")
    date: str = Field(..., description="접수일 YYYY-MM-DD")
    submitter: str = Field(..., description="제출인")
    viewer_url: str = Field(..., description="DART 원문 뷰어 링크")
    category: Literal["periodic", "performance", "major", "other"]


class DisclosureListResponse(BaseModel):
    stock_code: str
    company: str
    rule_id: Optional[str] = Field(None, description="요청 rule_id echo")
    disclosures: List[DisclosureItem] = Field(default_factory=list, description="최신순 최대 15건")
