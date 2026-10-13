import { ArrowRight, Check, Clock3, Code2, Database, FileCheck2, RefreshCw, TrendingUp, TriangleAlert } from "lucide-react";
import { Link } from "react-router-dom";
import type { OverviewSnapshot } from "../api/overview";
import { overviewDuration, overviewTime, RUN_LABELS, runTone, taskTypeLabel } from "./overviewPresentation";

const count = (value: number | null | undefined) => value == null ? "—" : value.toLocaleString("zh-CN");

function Timestamp({ value }: { value: string | null | undefined }) {
  return <time dateTime={value ?? undefined} title={value ? `${overviewTime(value, true)}（上海时间）` : "暂无记录"}>{overviewTime(value)}</time>;
}

/** Rendering is separate from loading so unknown, stale, empty and failed
 * regions can be exercised without manufacturing a production API response. */
export function OverviewContent({ snapshot, loading, errors, refreshed, onRefresh }: {
  snapshot: OverviewSnapshot; loading: boolean; errors: string[]; refreshed: boolean; onRefresh: () => void;
}) {
  const operations = snapshot.operations;
  const metrics = operations?.metrics;
  const market = snapshot.market, asset = market.data;
  const failures = [...errors, ...(market.error ? ["正式目录摘要"] : [])];
  const pending = loading ? "正在加载…" : "数据暂不可用，请刷新重试";
  return <div className="qfo-content" aria-busy={loading}>
    <div className="qfo-page-head">
      <div><div className="qfo-eyebrow">Operations Overview</div><h1>数据运营总览</h1><p className="qfo-page-desc">查看数据采集、运行任务和数据资产的当前状态。</p></div>
      <div className="qfo-page-actions"><button className="qfo-secondary-btn" type="button" disabled={loading} onClick={onRefresh}><RefreshCw aria-hidden="true" /><span>{loading ? "刷新中" : "刷新状态"}</span></button><Link className="qfo-primary-btn" to="/admin/tasks"><span>采集任务</span><ArrowRight aria-hidden="true" /></Link></div>
    </div>
    {failures.length > 0 && <div className="qfo-review-error" role="alert">{failures.join("、")}读取失败。各区块单独说明旧值或限制；未知数量以“—”显示。</div>}
    <div className="qfo-sr-only" role="status">{loading ? "正在刷新总览" : failures.length ? "部分数据未能刷新" : refreshed ? "总览状态已刷新" : ""}</div>
    <section className="qfo-metric-band" aria-label="运营指标">
      <div className="qfo-metric"><div className="qfo-metric-label"><Database aria-hidden="true" />已配置数据源</div><div className="qfo-metric-line"><div className="qfo-metric-value">{count(metrics?.configured_sources)}</div><div className="qfo-metric-unit">/ {count(metrics?.total_sources)}</div><div className="qfo-metric-note">{metrics ? "配置状态汇总" : "等待数据"}</div></div></div>
      <div className="qfo-metric"><div className="qfo-metric-label"><Clock3 aria-hidden="true" />启用采集任务</div><div className="qfo-metric-line"><div className="qfo-metric-value">{count(metrics?.active_tasks)}</div><div className="qfo-metric-note">{metrics ? `${count(metrics.queued_runs)} 项等待 · ${count(metrics.running_runs)} 项运行中` : "等待数据"}</div></div></div>
      <div className="qfo-metric" title={operations ? `上海时间 ${overviewTime(operations.day_start, true)} 至 ${overviewTime(operations.day_end, true)}（不含结束时刻），按实际开始时间统计` : "上海时区，按实际开始时间统计"}><div className="qfo-metric-label"><TrendingUp aria-hidden="true" />今日运行<span className="qfo-sr-only">（上海时区，按实际开始）</span></div><div className="qfo-metric-line"><div className="qfo-metric-value">{count(metrics?.today_runs)}</div><div className="qfo-metric-unit">次</div><div className="qfo-metric-note qfo-ok">{metrics ? `${count(metrics.today_succeeded)} 次成功` : "等待数据"}</div></div></div>
      <div className="qfo-metric"><div className="qfo-metric-label"><TriangleAlert aria-hidden="true" />需要处理</div><div className="qfo-metric-line"><div className="qfo-metric-value">{count(metrics?.attention_tasks)}</div><div className="qfo-metric-unit">项</div><div className={`qfo-metric-note${metrics?.attention_tasks ? " qfo-warn" : ""}`}>{metrics ? metrics.attention_tasks ? "采集任务异常" : "暂无待处理" : "等待数据"}</div></div></div>
    </section>
    <div className="qfo-overview-grid">
      <section className="qfo-sheet qfo-sources" aria-labelledby="overview-sources-title">
        <div className="qfo-sheet-head"><h2 className="qfo-sheet-title" id="overview-sources-title">数据源状态</h2><div className="qfo-sheet-meta">{count(metrics?.total_sources)} 个数据源 · 配置状态汇总</div><Link className="qfo-sheet-link" to="/admin/data-sources">管理数据源<ArrowRight aria-hidden="true" /></Link></div>
        <div className="qfo-source-table">{operations ? operations.sources.length ? operations.sources.map(source => <div className="qfo-source-row" key={source.key}>
          <div className="qfo-source-main"><div className="qfo-source-mark">{source.key === "tushare" ? "TS" : "DS"}</div><div><div className="qfo-source-name">{source.name}</div><div className="qfo-source-sub">连接配置与采集运行概况</div></div></div>
          <div className="qfo-source-stat"><span>配置状态</span><div className="qfo-source-status"><i aria-hidden="true" />{source.configured ? "已配置" : "未配置"}</div></div>
          <div className="qfo-source-stat"><span>启用任务</span><b>{count(source.active_tasks)}</b></div>
          <div className="qfo-source-stat"><span>最近成功同步</span><b><Timestamp value={source.last_success_at} /></b></div>
        </div>) : <div className="qfo-empty-body">暂无数据源</div> : <div className="qfo-empty-body">{pending}</div>}</div>
        <p className="qfo-source-extra">已配置不代表连接可用；来源配置与采集结果不决定已有合格本地数据的读取许可。正式数据逐请求核查。</p>
      </section>
      <section className="qfo-sheet qfo-attention" aria-labelledby="overview-attention-title">
        <div className="qfo-sheet-head"><h2 className="qfo-sheet-title" id="overview-attention-title">需要处理</h2><Link className="qfo-sheet-link" to="/admin/tasks">查看任务<ArrowRight aria-hidden="true" /></Link></div>
        <div className="qfo-attention-body">{!operations ? <div className="qfo-empty-body">{pending}</div> : <>
          {operations.attention.length ? <div className="qfo-issue-list">{operations.attention.map(({ run, retrying }) => <div className="qfo-issue" key={run.id}><div className="qfo-issue-icon"><TriangleAlert aria-hidden="true" /></div><div><div className="qfo-issue-title" title={taskTypeLabel(run)}>{run.task_name || taskTypeLabel(run)} · {RUN_LABELS[run.status]}</div><div className="qfo-issue-desc">最近实际执行于 <Timestamp value={run.started_at} />。{retrying ? "已有后续执行排队或运行中，等待执行结果。" : "查看运行记录定位原因，成功执行后解除提示。"}</div><Link className="qfo-issue-action" to="/admin/tasks">前往采集任务 →</Link></div></div>)}</div> : <div className="qfo-issue"><div className="qfo-issue-icon"><Check aria-hidden="true" /></div><div><div className="qfo-issue-title">暂无需要处理的采集任务</div><div className="qfo-issue-desc">各任务最新实际执行结果中，未发现失败、中断、超时或状态不确定的记录。</div></div></div>}
          <div className="qfo-all-good"><Check aria-hidden="true" /><span>{metrics && metrics.attention_tasks > operations.attention.length ? `展示 ${operations.attention.length} / ${count(metrics.attention_tasks)} 项；` : ""}按任务最新执行结果统计，不累计历史失败次数</span></div>
        </>}</div>
      </section>
      <section className="qfo-sheet qfo-runs" aria-labelledby="overview-runs-title">
        <div className="qfo-sheet-head"><h2 className="qfo-sheet-title" id="overview-runs-title">最近运行</h2><div className="qfo-sheet-meta">{operations ? <>{errors.includes("运营指标、数据源与运行记录") ? "上次快照" : "快照"} <Timestamp value={operations.generated_at} /></> : "等待快照"}</div><Link className="qfo-sheet-link" to="/admin/tasks">全部运行<ArrowRight aria-hidden="true" /></Link></div>
        <div className="qfo-table-wrap" tabIndex={0} role="region" aria-label="最近运行表格，可横向滚动"><table className="qfo-run-table"><thead><tr><th scope="col">任务</th><th scope="col">数据源</th><th scope="col">状态</th><th scope="col">耗时</th><th scope="col">完成时间</th></tr></thead><tbody>
          {operations?.recent_runs.length ? operations.recent_runs.map(run => <tr key={run.id}><td className="qfo-run-name" title={`${run.task_name} · ${taskTypeLabel(run)}`}><Link to="/admin/tasks">{run.task_name || taskTypeLabel(run)}</Link></td><td>{operations.sources.find(source => source.key === run.source_key)?.name ?? "—"}</td><td><span className={`qfo-badge qfo-${runTone(run.status)}`}>{RUN_LABELS[run.status]}</span></td><td className="qfo-mono" title={run.duration_seconds === null ? undefined : `${run.duration_seconds} 秒`}>{overviewDuration(run.duration_seconds)}</td><td className="qfo-mono"><Timestamp value={run.finished_at} /></td></tr>) : <tr><td colSpan={5}><div className="qfo-empty-body">{operations ? "暂无运行记录" : pending}</div></td></tr>}
        </tbody></table></div>
      </section>
      <section className="qfo-sheet qfo-side-summary" aria-label="数据资产与快速入口">
        <div className="qfo-summary-section"><div className="qfo-summary-head"><h2 className="qfo-summary-title">数据资产</h2><button className="qfo-summary-link" type="button" aria-disabled="true" title="资产汇总页尚未开放，请使用下方资产入口">查看全部</button></div>
          <Link className="qfo-asset-row" to="/admin/data/etf-basics" aria-busy={market.pending}><div><div className="qfo-asset-name">当前正式 ETF 目录</div><div className="qfo-asset-meta">{market.stale ? "同条件旧摘要 · 不能判断当前可读" : market.error ? market.error.message : market.pending ? "正在读取正式摘要…" : asset ? "本次目录摘要已读取 · 行情另行核查" : "等待读取"}</div></div><div className="qfo-asset-value"><b>{count(asset?.total_instruments)}</b><span>个目录对象</span></div></Link>
          {asset && <div className="qfo-asset-meta" aria-label="正式目录范围"><p>来源：{asset.meta.source_labels.join(" · ")}。来源目录业务对象，非物理行数或全市场证券数。上市日期：{asset.first_list_date ?? "未知"} → {asset.latest_list_date ?? "未知"}；不代表行情覆盖。</p><p>业务截至：{asset.meta.business_as_of ?? "未知"} · 读取于 <Timestamp value={asset.meta.read_at} />。{asset.meta.coverage.verified ? asset.meta.coverage.note : "覆盖未核验。"}</p></div>}
          <Link className="qfo-asset-row" to="/admin/data/trading-calendar"><div><div className="qfo-asset-name">正式交易日期事实</div><div className="qfo-asset-meta">按交易所与有界日期查看 · 全局开市数量未声明</div></div><div className="qfo-asset-value"><b>—</b><span>待选择范围</span></div></Link>
        </div>
        <div className="qfo-summary-section"><div className="qfo-summary-head"><h2 className="qfo-summary-title">快速入口</h2></div><div className="qfo-shortcut-row">
          <Link className="qfo-shortcut" to="/admin/strategies"><div className="qfo-shortcut-icon"><Code2 aria-hidden="true" /></div><div><div className="qfo-shortcut-title">策略工作台</div><div className="qfo-shortcut-desc">策略研究与回测</div></div></Link>
          <Link className="qfo-shortcut" to="/admin/backtest-runs"><div className="qfo-shortcut-icon"><FileCheck2 aria-hidden="true" /></div><div><div className="qfo-shortcut-title">回测工作台</div><div className="qfo-shortcut-desc">创建、管理与分析回测</div></div></Link>
        </div></div>
      </section>
    </div>
  </div>;
}
