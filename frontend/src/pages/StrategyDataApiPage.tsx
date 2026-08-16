import { CalendarDays, Database, Landmark, ShieldCheck } from "lucide-react";

const DAILY_BARS_EXAMPLE = `bars = context.market.daily_bars(
    codes=["510300.SH", "159919.SZ"],
    fields=["open", "high", "low", "close", "vol", "amount"],
    start_date=date(2024, 8, 16),
)`;

const FACTORS_EXAMPLE = `factors = context.market.adjustment_factors(
    codes=["510300.SH"],
    lookback_sessions=60,
)`;

const SESSIONS_EXAMPLE = `sessions = context.calendar.sessions(
    lookback_sessions=20,
)`;

const UNIVERSE_EXAMPLE = `candidates = context.universe.eligible_etfs(
    exchanges=["SSE", "SZSE"],
    min_history_sessions=60,
    require_bar_on_cutoff=True,
)`;

function CodeBlock({ children }: { children: string }) {
  return <pre className="strategy-data-docs__code"><code>{children}</code></pre>;
}

export function StrategyDataApiPage() {
  return <section className="strategy-data-docs" aria-labelledby="strategy-data-title">
    <div className="page-heading">
      <div>
        <h2 id="strategy-data-title">策略数据接口</h2>
        <p>策略通过受时间边界约束的 Context 读取 ETF 数据，不直接访问数据库。</p>
      </div>
      <span className="strategy-data-docs__version">首期 · ETF 日频</span>
    </div>

    <section className="strategy-data-docs__notice" aria-label="数据可见性规则">
      <ShieldCheck aria-hidden="true" />
      <div>
        <strong>统一时间边界</strong>
        <p>所有接口读取数据库当前最新数据，但查询终点不得晚于策略当前可见时点；请求未来日期会直接失败，不会被静默截断。</p>
      </div>
    </section>

    <section className="strategy-data-docs__section" aria-labelledby="visibility-title">
      <div className="strategy-data-docs__section-heading">
        <span>时间规则</span>
        <h3 id="visibility-title">决策时点决定可见数据</h3>
      </div>
      <div className="strategy-data-docs__table-wrap">
        <table className="strategy-data-docs__table">
          <thead><tr><th>策略计算时点</th><th>最新可见日线</th><th>接口行为</th></tr></thead>
          <tbody>
            <tr><td>D 日开盘前</td><td>D-1 日</td><td>自动使用前一个已完成交易日作为查询上限</td></tr>
            <tr><td>D 日收盘后</td><td>D 日</td><td>允许读取 D 日完整日线、复权因子和交易日历</td></tr>
          </tbody>
        </table>
      </div>
    </section>

    <section className="strategy-data-docs__section" aria-labelledby="methods-title">
      <div className="strategy-data-docs__section-heading">
        <span>公开能力</span>
        <h3 id="methods-title">首期接口</h3>
      </div>
      <div className="strategy-data-docs__method-grid">
        <article className="strategy-data-docs__method">
          <div><Database aria-hidden="true" /><code>context.session_date</code></div>
          <p>只读属性，返回当前策略决策所在的交易日。</p>
        </article>
        <article className="strategy-data-docs__method">
          <div><Database aria-hidden="true" /><code>context.market.daily_bars()</code></div>
          <p>读取指定 ETF 的原始日线 OHLCV 与成交额，结果按日期升序返回。</p>
        </article>
        <article className="strategy-data-docs__method">
          <div><Database aria-hidden="true" /><code>context.market.adjustment_factors()</code></div>
          <p>读取指定 ETF 的复权因子，与日线使用同一可见截止日。</p>
        </article>
        <article className="strategy-data-docs__method">
          <div><CalendarDays aria-hidden="true" /><code>context.calendar.sessions()</code></div>
          <p>读取已发生的交易日，按日期升序返回。</p>
        </article>
        <article className="strategy-data-docs__method">
          <div><Landmark aria-hidden="true" /><code>context.universe.eligible_etfs()</code></div>
          <p>返回当前时点具有上市及日线证据的 ETF 候选标的。</p>
        </article>
      </div>
    </section>

    <section className="strategy-data-docs__section" aria-labelledby="usage-title">
      <div className="strategy-data-docs__section-heading">
        <span>调用示例</span>
        <h3 id="usage-title">按需要选择日期窗口</h3>
      </div>
      <div className="strategy-data-docs__examples">
        <article><h4>日线行情</h4><CodeBlock>{DAILY_BARS_EXAMPLE}</CodeBlock></article>
        <article><h4>复权因子</h4><CodeBlock>{FACTORS_EXAMPLE}</CodeBlock></article>
        <article><h4>交易日历</h4><CodeBlock>{SESSIONS_EXAMPLE}</CodeBlock></article>
        <article><h4>可选 ETF 标的</h4><CodeBlock>{UNIVERSE_EXAMPLE}</CodeBlock></article>
      </div>
    </section>

    <section className="strategy-data-docs__section strategy-data-docs__section--rules" aria-labelledby="rules-title">
      <div className="strategy-data-docs__section-heading">
        <span>参数与限制</span>
        <h3 id="rules-title">查询窗口规则</h3>
      </div>
      <ul>
        <li><code>start_date</code> 可由策略指定；<code>end_date</code> 只能不晚于当前可见截止日。</li>
        <li><code>end_date</code> 省略时，自动使用当前可见截止日；<code>start_date</code> 与 <code>lookback_sessions</code> 不能同时传入。</li>
        <li>缺失行情不会被自动补齐或前向填充，策略应显式处理返回结果中的数据缺口。</li>
        <li>候选 ETF 不依据当前 <code>list_status</code>、名称、管理人、指数或费率筛选，避免将当前基础信息带回历史。</li>
        <li>策略接口不提供数据库 Session、ORM Repository、任意 SQL 或覆盖数据可见截止日的能力。</li>
      </ul>
    </section>
  </section>;
}
