import { useEffect, useMemo, useRef, useState } from 'react'
import { createRoot } from 'react-dom/client'
import { AreaSeries, LineSeries, createChart } from 'lightweight-charts'
import {
  Activity, BarChart3, ChartNoAxesCombined, ChevronRight, CircleAlert,
  CircleCheck, CirclePause, Database, FlaskConical, Layers3, ListOrdered, LockKeyhole, Menu,
  Play, Power, Radio, RefreshCw, Shield, SlidersHorizontal, Terminal, TrendingUp, Wallet, Zap,
} from 'lucide-react'
import './style.css'

const names = ['SAFE', 'BALANCED', 'MAX GROWTH']
const routes = [
  ['Dashboard', BarChart3, 'dashboard'],
  ['Tournament', ChartNoAxesCombined, 'tournament'],
  ['Strategy Lab', FlaskConical, 'lab'],
  ['Backtests', Layers3, 'backtests'],
  ['Bot Control', SlidersHorizontal, 'control'],
  ['Logs & Alerts', Terminal, 'logs'],
]
const descriptions = {
  dashboard: 'Monitor account equity, execution, and portfolio risk.',
  tournament: 'Compare both strategies on unseen Binance data after trading costs.',
  lab: 'Search parameters and inspect their walk-forward results.',
  backtests: 'Review saved results and compare two runs.',
  control: 'Manage execution and review the live trading gate.',
  logs: 'Review exchange fills, engine events, and alerts.',
}
const en = new Intl.NumberFormat('en-US', { maximumFractionDigits: 2 })
const money = new Intl.NumberFormat('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 })
const price = new Intl.NumberFormat('en-US', { maximumFractionDigits: 7 })
const isValue = (n) => typeof n === 'number' && Number.isFinite(n)
const fmt = (n) => isValue(n) ? en.format(n) : '—'
const usd = (n) => isValue(n) ? money.format(n) : '—'
const sign = (n) => n > 0 ? '+' : n < 0 ? '−' : ''
const signed = (n) => isValue(n) ? sign(n) + usd(Math.abs(n)) : '—'
const pct = (n, withSign = false) => isValue(n) ? `${withSign ? sign(n) : n < 0 ? '−' : ''}${fmt(Math.abs(n) * 100)}%` : '—'
const day = (s) => new Date(s * 1000).toISOString().slice(0, 10)
const datetime = (s) => s ? new Date(s * 1000).toLocaleString() : '—'
const tone = (n) => n > 0 ? 'positive' : n < 0 ? 'negative' : ''

async function api(path, method = 'GET', body) {
  const response = await fetch('/api' + path, {
    method, ...(body === undefined ? {} : { headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) }),
  })
  const result = await response.json()
  if (!response.ok) {
    const detail = Array.isArray(result.detail) ? result.detail.map((entry) => entry.msg).join(' · ') : result.detail
    throw new Error(detail || 'Request failed')
  }
  return result
}

function Badge({ children, variant = '' }) {
  return <span className={`badge ${variant}`}>{children}</span>
}

function Empty({ icon: Icon = Database, title, children }) {
  return <div className="empty"><div className="empty-icon"><Icon size={22} strokeWidth={1.6} /></div><strong>{title}</strong>
    {children && <span>{children}</span>}</div>
}

function Card({ title, detail, icon: Icon, aside, children, className = '' }) {
  return <section className={`card ${className}`}>
    {(title || aside) && <header className="card-head"><div>{title && <h2>{Icon && <Icon size={16} />}{title}</h2>}{detail && <p>{detail}</p>}</div>{aside}</header>}
    {children}
  </section>
}

function EquityChart({ primary, overlays, empty = 'Awaiting account data' }) {
  const ref = useRef(null)
  useEffect(() => {
    if (!ref.current || !primary?.length) return
    const chart = createChart(ref.current, {
      autoSize: true,
      layout: { background: { color: '#101821' }, textColor: '#8c9bac', fontFamily: 'system-ui' },
      grid: { vertLines: { color: '#1b2530' }, horzLines: { color: '#1b2530' } },
      crosshair: { vertLine: { color: '#62798d' }, horzLine: { color: '#62798d' } },
      rightPriceScale: { borderColor: '#263441' },
      timeScale: { borderColor: '#263441', timeVisible: primary.at(-1)[0] - primary[0][0] < 3 * 86400 },
    })
    const points = (values) => values.map(([time, value]) => ({ time, value }))
    const series = chart.addSeries(AreaSeries, {
      lineColor: '#50d8ad', topColor: '#2eb68b55', bottomColor: '#2eb68b05', lineWidth: 2,
    })
    series.setData(points(primary))
    for (const [color, values] of overlays || []) {
      chart.addSeries(LineSeries, { color, lineWidth: 1, priceLineVisible: false }).setData(points(values))
    }
    chart.timeScale().fitContent()
    return () => chart.remove()
  }, [primary, overlays])
  return <div className="chart-wrap">{primary?.length ? <div className="chart" ref={ref} /> :
    <div className="chart-grid"><Empty icon={ChartNoAxesCombined} title={empty}>Verified balances will appear here after the engine connects.</Empty></div>}</div>
}

function Table({ headers, rows, empty = 'Nothing to show yet', detail }) {
  if (!rows.length) return <Empty icon={ListOrdered} title={empty}>{detail}</Empty>
  return <div className="table-scroll"><table><thead><tr>{headers.map((key) => <th key={key}>{key}</th>)}</tr></thead>
    <tbody>{rows.map((row, i) => <tr key={i}>{row.map((value, j) => <td key={j}>{value}</td>)}</tr>)}</tbody></table></div>
}

function Stats({ items }) {
  return <div className="stats">{items.map(([label, value, icon, detail, variant]) => {
    const Icon = icon
    return <section className="card stat" key={label}><div><span>{label}</span><Icon size={17} /></div>
      <strong className={variant}>{value}</strong><small>{detail}</small></section>
  })}</div>
}

function Dashboard({ status, go }) {
  const risk = status.risk
  const drawdown = risk && isValue(status.equity) && risk.peak > 0 ? Math.max(0, 1 - status.equity / risk.peak) : null
  const regime = status.regime === true ? 'Risk-on' : status.regime === false ? 'Risk-off' : 'Awaiting feed'
  const pnl = status.pnl || {}
  return <>
    <Stats items={[
      ['Account equity', usd(status.equity), Wallet, 'Strategy allocation · USDT'],
      ['P&L today', signed(pnl.daily), Activity, 'Since 00:00 UTC', tone(pnl.daily)],
      ['P&L last 7 days', signed(pnl.weekly), TrendingUp, 'Rolling 7 days · USDT', tone(pnl.weekly)],
      ['Market regime', regime, Radio, 'BTC trend and funding', status.regime ? 'positive' : ''],
    ]} />
    <div className="dashboard-grid">
      <Card icon={ChartNoAxesCombined} title="Account equity" detail="Verified strategy equity in USDT"
        aside={<Badge>Last 28 days</Badge>}><EquityChart primary={status.curve} /></Card>
      <Card icon={Shield} title="Portfolio risk" detail="Limits enforced by the engine">
        <div className="risk-panel">
          <div className="risk-status"><span className="risk-symbol"><Shield size={21} /></span><div>
            <strong>{status.killed || risk?.halted ? 'Engine locked' : risk?.daily_locked ? 'Daily limit reached' : status.running ? 'Monitoring' : 'Engine stopped'}</strong>
            <p>{status.strategy?.preset || 'MAX GROWTH'}</p>
          </div></div>
          <div className="risk-meter"><div><span>Equity drawdown</span><b>{pct(drawdown)} / 20%</b></div>
            <progress value={drawdown ?? 0} max={0.20} aria-label="Drawdown used" /></div>
          <dl className="mini-dl">
            <div><dt>Drawdown size reduction</dt><dd>10%</dd></div>
            <div><dt>Losing streak</dt><dd>{risk?.streak ?? '—'}</dd></div>
            <div><dt>Same-side positions</dt><dd>Max 3</dd></div>
          </dl>
          <button className="text-button" onClick={() => go('control')}>Open bot controls <ChevronRight size={15} /></button>
        </div>
      </Card>
    </div>
    <div className="dashboard-grid">
      <Card title="Open positions" icon={ListOrdered} aside={<Badge>{status.positions?.length || 0} open</Badge>}>
        <Table headers={['Symbol', 'Side', 'Size', 'Entry', 'Stop', 'Target', 'P&L']} empty="No open positions"
          detail="Exchange positions appear here after reconciliation."
          rows={(status.positions || []).map((p) => [<b>{p.symbol}/USDT</b>, <Badge variant={p.side > 0 ? 'green' : 'red'}>{p.side > 0 ? 'Long' : 'Short'}</Badge>,
            price.format(p.size), price.format(p.entry), p.stop == null ? '—' : price.format(p.stop),
            p.target == null ? '—' : price.format(p.target), <span className={tone(p.pnl)}>{signed(p.pnl)}</span>])} />
      </Card>
      <Card title="Open orders" icon={Layers3} aside={<Badge>{status.orders?.length || 0} working</Badge>}>
        <Table headers={['Symbol', 'Type', 'Trigger']} empty="No working orders"
          detail="Includes exchange-native stop and take-profit orders."
          rows={(status.orders || []).map((o) => [<b>{o.symbol}</b>, o.kind?.replaceAll('_', ' '), o.price ? price.format(o.price) : 'Market'])} />
      </Card>
    </div>
  </>
}

const strategyNames = { A: 'Slow trend following', B: 'Volatility mean reversion' }
const families = ['A', 'B']
const factor = (metrics) => metrics.pf_unbounded ? '∞' : fmt(metrics.profit_factor)

function EdgeBadge({ verdict = 'UNDER REVIEW' }) {
  return <Badge variant={verdict === 'EDGE CONFIRMED' ? 'green' : verdict === 'NO EDGE FOUND' ? 'red' : 'amber'}>{verdict}</Badge>
}

function Presets({ selected, onChange, settings, report }) {
  return <div className="presets">{names.map((name) => {
    const result = report?.presets[name]
    const chance = result?.strategies
    return <button className={`card preset ${name === selected ? 'selected' : ''}`} key={name}
      aria-pressed={name === selected} onClick={() => onChange(name)}>
      <div><b>{name}</b><span className="radio-dot" /></div>
      <div className="preset-cap"><Shield size={14} /> {pct(settings?.[name]?.risk)} per trade</div>
      <dl><div><dt>A · Reshuffled ruin</dt><dd>{pct(chance?.A.monte_carlo.ruin_probability)}</dd></div>
        <div><dt>B · Reshuffled ruin</dt><dd>{pct(chance?.B.monte_carlo.ruin_probability)}</dd></div></dl>
    </button>
  })}</div>
}

function FeePanel({ title, metrics, aside, costs }) {
  return <Card title={title} icon={Wallet} aside={aside}>
    <dl className="metric-grid fee-metrics">
      <div><dt>Gross return</dt><dd className={tone(metrics.gross_return)}>{pct(metrics.gross_return, true)}</dd></div>
      <div><dt>Taker fees · USDT</dt><dd>{usd(metrics.fees)}</dd></div>
      <div><dt>Slippage · USDT</dt><dd>{usd(metrics.slippage)}</dd></div>
      <div><dt>Funding · USDT</dt><dd className={tone(metrics.funding)}>{signed(metrics.funding)}</dd></div>
      <div><dt>Net return</dt><dd className={tone(metrics.net_return)}>{pct(metrics.net_return, true)}</dd></div>
      <div><dt>Fees / gross P&L</dt><dd>{pct(metrics.fee_to_gross)}</dd></div>
    </dl>
    <div className="inline-note">Fee ratio is shown when gross P&L is positive. Each entry and exit includes {pct(costs.fee_per_side)} taker fee and {pct(costs.slippage_per_side)} slippage.</div>
  </Card>
}

function Detail({ family, result, benchmarks, label, costs }) {
  const m = result.aggregate
  const shuffle = result.monte_carlo
  const overlays = useMemo(() => [['#78a4ee', benchmarks.BTC.curve], ['#d2a7ff', benchmarks.ETH.curve]], [benchmarks])
  return <div className="report">
    <FeePanel title={`${label} · Cost breakdown`} metrics={m} costs={costs} aside={<EdgeBadge verdict={result.edge} />} />
    <Card title={`${label} · Unseen equity`} icon={ChartNoAxesCombined}>
      <div className="chart-legend"><span className="leg-strategy">Strategy</span><span className="leg-btc">BTC spot buy and hold</span>
        <span className="leg-eth">ETH spot buy and hold</span></div>
      <EquityChart primary={result.equity} overlays={overlays} />
      <div className="inline-note">Net return {pct(m.net_return, true)} · Max drawdown {pct(m.max_drawdown)} · {m.trades} trades ·
        {' '}{fmt(m.average_hold_days)} average hold days · Peak rolling count {m.max_rolling_round_trips}</div>
      {m.hard_stop && <div className="alert warn"><CircleAlert size={17} /> The 20% drawdown stop halted this simulation. Equity and trade budgets carry across folds.</div>}
      {family === 'A' && isValue(m.average_hold_days) && m.average_hold_days < 5 &&
        <div className="inline-note">Average holding time was below the 5–30 day target.</div>}
    </Card>
    <Card title="Walk-forward folds" icon={FlaskConical} detail="Settings were selected using the preceding 12 months. Each row reports the following six months.">
      {result.overfit && <div className="alert warn"><CircleAlert size={17} /> OVERFIT · Training profit factor exceeded unseen profit factor by more than 2× in at least one fold.</div>}
      <Table headers={['Unseen dates (UTC)', 'Train PF', 'OOS PF', 'Gross return', 'Fees (USDT)', 'Fees / gross', 'Net return', 'Drawdown', 'Trades', 'Status']}
        rows={result.folds.map(({ test_start, test_end, train, test, overfit }) => [
          `${day(test_start)} → ${day(test_end)}`, factor(train), factor(test), pct(test.gross_return, true), usd(test.fees),
          pct(test.fee_to_gross), <b className={tone(test.net_return)}>{pct(test.net_return, true)}</b>, pct(test.max_drawdown),
          test.trades, overfit ? <Badge variant="amber">OVERFIT</Badge> : '—',
        ])} />
      <details className="fold-choices"><summary>Training selections</summary>
        <Table headers={['Training window', 'Chosen settings', 'Training net', 'Training trades']}
          rows={result.folds.map((f) => [`${day(f.train_start)} → ${day(f.test_start)}`,
            Object.entries(f.parameters).map(([key, value]) => `${key.replaceAll('_', ' ')} ${fmt(value)}`).join(' · '),
            pct(f.train.net_return, true), f.train.trades])} />
      </details>
    </Card>
    <div className="dashboard-grid edge-detail-grid">
      <Card title="Historical stress tests" icon={Shield} detail="Earlier windows replay the final selected parameters.">
        <Table headers={['Segment', 'Gross', 'Fees (USDT)', 'Fees / gross', 'Net', 'Drawdown', 'Trades', 'BTC', 'ETH']}
          rows={result.stress.map(({ name, metrics, benchmarks: spot }) => [
            name, pct(metrics.gross_return, true), usd(metrics.fees), pct(metrics.fee_to_gross), pct(metrics.net_return, true),
            pct(metrics.max_drawdown), metrics.trades, pct(spot.BTC, true), pct(spot.ETH, true),
          ])} />
      </Card>
      <Card title="Monte Carlo drawdown" icon={Activity} detail="Trade-order shuffles model closed-trade paths. Equity drawdowns during open trades can be larger.">
        <dl className="mini-dl risk-panel">
          <div><dt>Shuffles</dt><dd>{fmt(shuffle.shuffles)}</dd></div>
          <div><dt>Sample trades</dt><dd>{shuffle.trades}</dd></div>
          <div><dt>Probability of a 20% drawdown</dt><dd>{pct(shuffle.ruin_probability)}</dd></div>
          <div><dt>5th / median / 95th drawdown</dt><dd>{shuffle.drawdown_percentiles?.map((v) => pct(v)).join(' / ') || '—'}</dd></div>
        </dl>
      </Card>
    </div>
  </div>
}

function Tournament({ report, settings, selectedPreset }) {
  const [preset, setPreset] = useState(selectedPreset || 'MAX GROWTH')
  const [family, setFamily] = useState('A')
  if (!report) return <Card title="Tournament results" aside={<EdgeBadge />}>
    <Empty title="Awaiting tournament results">Run the baseline or test a variation in Strategy Lab.</Empty></Card>
  const result = report.presets[preset]
  const selected = result.selected
  return <>
    <Presets selected={preset} onChange={setPreset} settings={settings} report={report} />
    <Card title="Out-of-sample scoreboard" icon={BarChart3}
      aside={<Badge variant={report.qualification ? 'green' : 'amber'}>{report.qualification ? 'Baseline' : 'Exploratory'}</Badge>}
      detail={`${day(report.oos_start)} to ${day(report.oos_end)} (exclusive) · ${fmt(report.hourly_bars)} hourly bars per asset · ${fmt(report.seconds)} s`}>
      <div className="inline-note">Survival requires a positive unseen net return and profit factor of at least 1.20.</div>
      <Table headers={['Strategy', 'Gross return', 'Fees (USDT)', 'Fees / gross', 'Net return', 'Profit factor', 'Drawdown', 'Trades / year', 'Verdict']}
        rows={families.map((key) => {
          const row = result.strategies[key]
          const m = row.aggregate
          return [<button className="text-button" onClick={() => setFamily(key)}>{key} · {strategyNames[key]}</button>,
            pct(m.gross_return, true), usd(m.fees), pct(m.fee_to_gross), <b className={tone(m.net_return)}>{pct(m.net_return, true)}</b>,
            factor(m), pct(m.max_drawdown), fmt(m.trades_year), <EdgeBadge verdict={row.edge} />]
        })} />
      <div className="inline-note">Selected {selected.length ? selected.join(' + ') : 'none'} · Daily return correlation {fmt(result.daily_correlation)}
        {' '}· BTC spot {pct(report.benchmarks.BTC.return, true)} · ETH spot {pct(report.benchmarks.ETH.return, true)}</div>
      {!selected.length && <div className="alert warn"><CircleAlert size={17} /> NO EDGE FOUND for this preset.</div>}
      {selected.length === 2 && <div className="inline-note">The paper plan runs both strategies at half risk each.
        {' '}Combined simulation fills · A {result.portfolio.trades_by_family.A} · B {result.portfolio.trades_by_family.B}</div>}
      {!report.qualification && <div className="inline-note">Search results are exploratory. Execution uses only the baseline qualification.</div>}
    </Card>
    {result.portfolio && <FeePanel title="Combined half-risk portfolio" metrics={result.portfolio.aggregate} costs={report}
      aside={<Badge>PF {factor(result.portfolio.aggregate)} · DD {pct(result.portfolio.aggregate.max_drawdown)}</Badge>} />}
    <div className="family-selector" role="group" aria-label="Strategy details">{families.map((key) => <button key={key}
      className={`button ${family === key ? 'primary' : 'secondary'}`} aria-pressed={family === key} onClick={() => setFamily(key)}>
      {key} · {strategyNames[key]}</button>)}</div>
    <Detail family={family} result={result.strategies[family]} label={`${family} · ${strategyNames[family]}`} benchmarks={report.benchmarks} costs={report} />
  </>
}

const parameterLabels = { breakout_days: 'Donchian breakout lookback (days)', volume_ratio: 'Daily volume multiple',
  trail_atr: 'Trend stop (4h ATR)', mean_bars: 'Reversion mean (4h bars)', deviation_atr: 'Deviation (4h ATR)',
  rsi_low: 'Long RSI threshold (short uses 100 minus this)', protection_atr: 'Reversion protection (4h ATR)' }

function Lab({ defaults, activeRun, latest, running, submit, busy }) {
  const [draft, setDraft] = useState(null)
  const [universe, setUniverse] = useState(null)
  if (!defaults) return <Card><Empty title="Loading parameter search" /></Card>
  const spec = draft || defaults.backtest
  const symbols = universe ?? spec.universe.join(', ')
  const update = (key, value) => setDraft({ ...spec, [key]: value })
  const reset = () => { setDraft(null); setUniverse(null) }
  return <>
    <form onSubmit={(e) => {
      e.preventDefault()
      submit({ ...spec, universe: symbols.toUpperCase().split(/[,\s]+/).filter(Boolean) })
    }}>
      <Card title="Parameter search" icon={SlidersHorizontal}
        detail="Evaluate variations on the same walk-forward method. The baseline alone qualifies a plan for paper execution."
        aside={<button className="button secondary" type="button" onClick={reset}>Restore baseline</button>}>
        <div className="form-grid">
          <label className="wide">Candidate assets<input required value={symbols} onChange={(e) => setUniverse(e.target.value)} /></label>
          {['trend', 'reversion'].flatMap((key) => {
            const definitions = defaults.schema.$defs[key === 'trend' ? 'Trend' : 'Reversion'].properties
            return Object.entries(spec[key]).map(([name, value]) => <label key={name}>{parameterLabels[name]}
              <input required type="number" min={definitions[name].minimum} max={definitions[name].maximum}
                step={definitions[name].type === 'integer' ? 1 : 'any'} value={value}
                onChange={(e) => update(key, { ...spec[key], [name]: e.target.value === '' ? '' : Number(e.target.value) })} /></label>)
          })}
        </div>
      </Card>
      <Card title="Research window" icon={Database} detail="Choose month boundaries 30–60 months apart in six-month increments. End dates are exclusive."
        aside={<button className="button primary" disabled={busy || running}><Play size={15} /> {running ? 'Running…' : 'Run tournament'}</button>}>
        <div className="form-grid">
          <label>Start<input required type="date" value={spec.start} onChange={(e) => update('start', e.target.value)} /></label>
          <label>End<input required type="date" value={spec.end} onChange={(e) => update('end', e.target.value)} /></label>
          <label>Starting capital (USDT)<input required type="number" min="10" max="1000000" step="0.01" value={spec.capital}
            onChange={(e) => update('capital', e.target.value === '' ? '' : Number(e.target.value))} /></label>
        </div>
        {activeRun && <div className={`inline-note ${activeRun.status === 'failed' ? 'negative' : ''}`} role="status">{activeRun.progress}</div>}
      </Card>
    </form>
    {latest?.report && <Tournament report={latest.report} settings={defaults.presets} />}
  </>
}

function Backtests({ runs, latest, selectLatest, settings }) {
  const [a, setA] = useState('')
  const [b, setB] = useState('')
  const [comparison, setComparison] = useState(null)
  const [preset, setPreset] = useState('MAX GROWTH')
  const [error, setError] = useState('')
  const complete = runs.filter((run) => run.status === 'complete' && run.spec.trend)
  async function compare() {
    try { setError(''); setComparison(await Promise.all([api('/backtests/' + a), api('/backtests/' + b)])) }
    catch (e) { setError(e.message) }
  }
  const options = (id, setter) => <select value={id} onChange={(e) => setter(e.target.value)}>
    <option value="">Select a run</option>{complete.map((run) => <option value={run.id} key={run.id}>
      {datetime(run.at)} · {run.spec.start} → {run.spec.end} · {run.id.slice(0, 8)}</option>)}</select>
  return <>
    <Card title="Run history" icon={Layers3}>
      <Table headers={['Started', 'Window', 'Type', 'Status', 'Report']} empty="No saved runs" detail="Run a tournament from Strategy Lab."
        rows={runs.map((run) => [datetime(run.at), `${run.spec.start} → ${run.spec.end}`, run.spec.trend ? 'Tournament' : 'v1 archive',
          <span title={run.progress}><Badge variant={run.status === 'complete' ? 'green' : run.status === 'failed' ? 'red' : 'amber'}>{run.status}</Badge></span>,
          run.status !== 'complete' ? '—' : run.spec.trend ? <button className="text-button" onClick={() => selectLatest(run.id)}>
            View <ChevronRight size={14} /></button> : <a className="text-button" href={`/api/backtests/${run.id}`} target="_blank" rel="noreferrer">JSON</a>])} />
    </Card>
    <Card title="Compare tournament runs" icon={ChartNoAxesCombined}
      aside={<button className="button secondary" disabled={!a || !b || a === b} onClick={compare}>Compare</button>}>
      <div className="form-grid"><label>Run A{options(a, setA)}</label><label>Run B{options(b, setB)}</label>
        <label>Preset<select value={preset} onChange={(e) => setPreset(e.target.value)}>{names.map((name) => <option key={name}>{name}</option>)}</select></label></div>
      {error && <div className="alert warn">{error}</div>}
      {comparison && <Table headers={['Run', 'Strategy', 'Gross return', 'Fees (USDT)', 'Fees / gross', 'Net return', 'Profit factor', 'Drawdown', 'Verdict']}
        rows={comparison.flatMap((run, index) => families.map((key) => {
          const row = run.report.presets[preset].strategies[key]
          const m = row.aggregate
          return [index === 0 ? 'Run A' : 'Run B', strategyNames[key], pct(m.gross_return, true), usd(m.fees),
            pct(m.fee_to_gross), pct(m.net_return, true), factor(m), pct(m.max_drawdown), <EdgeBadge verdict={row.edge} />]
        }))} />}
    </Card>
    {latest?.report && <Tournament report={latest.report} selectedPreset={preset} settings={settings} />}
  </>
}

function Control({ status, runAction, setPreset, busy }) {
  const [mode, setMode] = useState('paper')
  const checks = status.gate?.checks || []
  const blocked = !status.edge_available || !status.paper_configured || (mode === 'live' && !status.live_available) ||
    status.killed || status.risk?.halted || status.flatten_pending
  return <div className="control-grid">
    <Card title="Execution mode" icon={Radio} detail="Use the local environment file for exchange credentials.">
      <div className="risk-panel">
        <div className="mode-picker" role="group" aria-label="Execution mode">
          <button aria-pressed={mode === 'paper'} onClick={() => setMode('paper')}><FlaskConical size={16} /> Paper</button>
          <button aria-pressed={mode === 'live'} disabled={!status.live_available} onClick={() => setMode('live')}>
            {status.live_available ? <Zap size={16} /> : <LockKeyhole size={16} />} Live</button>
        </div>
        <label className="risk-preset-field">Risk preset<select value={status.strategy?.preset || 'MAX GROWTH'}
          disabled={busy || status.running || status.flatten_pending || !!status.positions?.length}
          onChange={(e) => setPreset(e.target.value)}>{names.map((name) => <option key={name}>{name}</option>)}</select></label>
        <dl className="mini-dl"><div><dt>Tournament edge</dt><dd><EdgeBadge verdict={status.edge_status} /></dd></div>
          <div><dt>Qualified families</dt><dd>{status.strategy?.families?.join(' + ') || '—'}</dd></div>
          <div><dt>Connected engine</dt><dd>{status.mode?.toUpperCase() || 'PAPER'}</dd></div>
          <div><dt>Execution</dt><dd>{status.running ? 'Active' : 'Stopped'}</dd></div>
          <div><dt>Paper credentials</dt><dd>{status.paper_configured ? 'Configured' : 'Missing'}</dd></div></dl>
        <div className="action-row">
          <button className="button primary" disabled={busy || blocked || status.running} onClick={() => runAction('start', { mode })}><Play size={15} /> Start engine</button>
          <button className="button secondary" disabled={busy || !status.running} onClick={() => runAction('stop')}><CirclePause size={16} /> Stop entries</button>
        </div>
        <p className="muted">Stopping entries keeps monitoring existing positions and their exchange orders.</p>
      </div>
    </Card>
    <Card title="Go-live gate" icon={LockKeyhole} detail="Evidence is scoped to the exact paper account, strategy configuration, and capital allocation."
      aside={<Badge variant={status.gate?.passed ? 'green' : 'amber'}>{status.gate?.passed ? 'Paper gate passed' : 'Locked'}</Badge>}>
      <ul className="checklist">{checks.map((check) => <li key={check.label}><span className={check.passed ? 'positive' : 'muted'}>
        {check.passed ? <CircleCheck size={17} /> : <CirclePause size={17} />}</span><div><strong>{check.label}</strong>
        <small>Required {check.target}</small></div><b>{isValue(check.value) ? fmt(check.value) : check.value ?? '—'}</b></li>)}</ul>
    </Card>
    <Card title="Emergency controls" icon={Shield} className="emergency">
      <div className="risk-panel">
        <p>Kill switch disables entries, attempts to close exchange positions, and cancels working orders. Completion requires exchange confirmation.</p>
        {status.flatten_pending && <div className="alert warn"><CircleAlert size={16} /> Flatten pending. Verify exposure on the exchange.</div>}
        <div className="action-row">
          <button className="button danger" disabled={busy || !status.paper_configured} onClick={() => runAction('kill')}><Power size={16} /> Kill switch</button>
          <button className="button secondary" disabled={busy || status.running || status.flatten_pending || !(status.killed || status.risk?.halted)}
            onClick={() => runAction('reset')}><RefreshCw size={15} /> Reset latched stop</button>
        </div>
      </div>
    </Card>
  </div>
}

function Logs({ logs }) {
  return <>
    <Card title="Trade log" icon={Activity}>
      <Table headers={['Closed', 'Mode', 'Symbol', 'Direction', 'Fees', 'Funding', 'Net P&L']} empty="No verified round trips"
        rows={(logs?.trades || []).map((t) => [datetime(t.closed), t.mode, t.symbol, t.side > 0 ? 'Long' : 'Short',
          fmt(t.fees), signed(t.funding), <span className={tone(t.pnl)}>{signed(t.pnl)}</span>])} />
    </Card>
    <Card title="Engine events" icon={Terminal} detail="Telegram alerts are optional and configured locally.">
      <Table headers={['Time', 'Level', 'Message']} empty="No engine events yet"
        rows={(logs?.events || []).map((e) => [datetime(e.at), <Badge variant={e.level === 'alert' || e.level === 'error' ? 'red' : ''}>{e.level}</Badge>, e.message])} />
    </Card>
  </>
}

function App() {
  const currentRoute = () => routes.some((r) => r[2] === location.hash.slice(1)) ? location.hash.slice(1) : 'dashboard'
  const [route, setRoute] = useState(currentRoute)
  const [status, setStatus] = useState({})
  const [defaults, setDefaults] = useState(null)
  const [runs, setRuns] = useState([])
  const [latest, setLatest] = useState(null)
  const [baseline, setBaseline] = useState(null)
  const [selected, setSelected] = useState(null)
  const [logs, setLogs] = useState(null)
  const [pending, setPending] = useState(null)
  const [error, setError] = useState('')
  const [network, setNetwork] = useState('')
  const [menu, setMenu] = useState(false)
  const active = runs.find((run) => run.status === 'running' || run.status === 'queued')
  const completed = runs.find((run) => run.status === 'complete' && run.spec.trend)
  const displayId = (route === 'backtests' && selected) || completed?.id
  const topRun = runs.find((run) => run.spec.trend)
  const [refreshIndex, setRefreshIndex] = useState(0)
  function go(next) { location.hash = next; setMenu(false) }
  useEffect(() => {
    const handle = () => { setRoute(currentRoute()); setError('') }
    window.addEventListener('hashchange', handle)
    api('/config').then(setDefaults).catch((e) => setNetwork(e.message))
    return () => window.removeEventListener('hashchange', handle)
  }, [])
  useEffect(() => {
    let disposed = false
    async function refresh() {
      try {
        const [nextStatus, nextRuns, nextLogs] = await Promise.all([
          api('/status'), api('/backtests'), route === 'logs' ? api('/logs') : Promise.resolve(null),
        ])
        if (!disposed) {
          setNetwork(''); setStatus(nextStatus); setRuns(nextRuns)
          if (nextLogs) setLogs(nextLogs)
        }
      } catch (e) { if (!disposed) setNetwork(e.message) }
    }
    refresh()
    const timer = setInterval(refresh, 4000)
    return () => { disposed = true; clearInterval(timer) }
  }, [route, refreshIndex])
  useEffect(() => {
    let disposed = false
    if (displayId && (route === 'lab' || route === 'backtests')) {
      api('/backtests/' + displayId).then((record) => { if (!disposed) setLatest(record) })
        .catch((e) => { if (!disposed) setError(e.message) })
    }
    return () => { disposed = true }
  }, [displayId, route])
  useEffect(() => {
    let disposed = false
    if (route === 'tournament') {
      api('/tournament').then((record) => { if (!disposed) setBaseline(record) })
        .catch((e) => { if (!disposed) setError(e.message) })
    }
    return () => { disposed = true }
  }, [route, completed?.id])
  async function change(id, work) {
    setPending(id); setError('')
    try { await work(); setRefreshIndex((value) => value + 1) }
    catch (e) { setError(e.message) }
    finally { setPending(null) }
  }
  const activeTitle = routes.find((r) => r[2] === route)[0]
  return <div className="shell">
    <aside className={`sidebar ${menu ? 'open' : ''}`}>
      <button className="brand" onClick={() => go('dashboard')}><span className="brand-mark"><Activity size={24} strokeWidth={2.4} /></span>
        <span>Supertrade<small>Trading terminal</small></span></button>
      <nav aria-label="Main navigation">{routes.map(([title, Icon, id]) =>
        <button className={id === route ? 'active' : ''} aria-current={id === route ? 'page' : undefined}
          key={id} onClick={() => go(id)}><Icon size={18} />{title}{id === route && <ChevronRight className="nav-current" size={15} />}</button>)}</nav>
      <div className="sidebar-bottom"><span className="workspace-icon"><Database size={16} /></span><div><strong>Local workspace</strong><small>Binance USDⓈ-M Futures</small></div></div>
    </aside>
    <main className="main">
      <div className="topbar"><div className="top-path"><button className="icon-button menu-button" aria-label="Toggle navigation"
        aria-expanded={menu} onClick={() => setMenu(!menu)}><Menu size={20} /></button><span>Workspace</span><ChevronRight size={14} /><b>{activeTitle}</b></div>
        <div className="top-status"><Badge variant={status.mode === 'live' ? 'red' : ''}>{status.mode === 'live' ? 'Live' : 'Paper'} mode</Badge>
          <span className={`connection ${status.running ? 'online' : ''}`}><i />{status.running ? 'Engine active' : 'Engine stopped'}</span></div></div>
      <div className="content">
        <div className="page-heading"><div><h1>{activeTitle}</h1><p>{descriptions[route]}</p></div>
          {route === 'dashboard' && <button className="button primary" onClick={() => go('control')}>Bot controls <ChevronRight size={16} /></button>}</div>
        {(network || error || status.error) && <div className="alert warn" role="alert"><CircleAlert size={18} />{network || error || status.error}</div>}
        {route === 'dashboard' && <Dashboard status={status} go={go} />}
        {route === 'tournament' && <Tournament report={baseline?.report} settings={defaults?.presets} selectedPreset={status.strategy?.preset} />}
        {route === 'lab' && <Lab defaults={defaults} latest={latest?.id === displayId ? latest : null}
          activeRun={active || topRun} running={!!active} busy={!!pending}
          submit={(spec) => change('backtest', () => api('/backtests', 'POST', spec))} />}
        {route === 'backtests' && <Backtests runs={runs} latest={latest?.id === displayId ? latest : null}
          settings={defaults?.presets} selectLatest={setSelected} />}
        {route === 'control' && <Control status={status} busy={!!pending}
          setPreset={(preset) => change('preset', async () => setStatus(await api('/strategy', 'PUT', { preset })))}
          runAction={(id, body) => change(id, async () => setStatus(await api('/bot/' + id, 'POST', body)))} />}
        {route === 'logs' && <Logs logs={logs} />}
        <footer><Shield size={14} /> Past backtests do not guarantee future results.
          <a href="https://www.tradingview.com/" target="_blank" rel="noreferrer">Charts by TradingView</a>
          {status.verified_at && <span>Last broker check {datetime(status.verified_at)}</span>}</footer>
      </div>
    </main>
  </div>
}

createRoot(document.getElementById('root')).render(<App />)
