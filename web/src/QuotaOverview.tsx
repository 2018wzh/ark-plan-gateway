import {Progress,Tooltip} from 'antd';
import {Account,formatTime,quotaColor} from './accounts';
import {quotaSummary} from './quotaSummary';
const amount=(n:number)=>n.toLocaleString('zh-CN',{maximumFractionDigits:4});
export default function QuotaOverview({accounts}:{accounts:Account[]}) {
  return <section aria-label="额度聚合" className="quota-overview">
    <div className="quota-overview-heading"><strong>额度聚合</strong><span>已启用且未过期的额度主体 · 共享密钥去重 · 已知剩余比例等权平均</span></div>
    <div className="quota-overview-grid">{quotaSummary(accounts).map(row=><article className="quota-overview-card" key={row.key}>
      <div className="quota-overview-value"><Progress type="circle" size={64} strokeWidth={7} percent={row.percent??0} strokeColor={row.percent===null?'#91a0a6':quotaColor(row.percent)} format={()=>row.percent===null?'未知':`${row.percent.toFixed(3)}%`}/><div><strong>{row.label}</strong><span>平均剩余</span></div></div>
      <div className="quota-overview-details">
        <span>已知 {row.known} · 未知 {row.unknown} · 耗尽 {row.exhausted}</span>
        {row.afpTotal>0&&<Tooltip title="AFP 仅汇总提供绝对额度的主体；其他主体仅参与剩余比例平均。"><span>AFP 剩余 {amount(row.afpRemaining)} / {amount(row.afpTotal)}</span></Tooltip>}
        <span>{row.key==='daily'?'未提供每日窗口的套餐不计入此项':'未知额度不按满额计算'}</span>
        <Tooltip title={formatTime(row.nextReset)}><span>最近重置：{row.nextReset?formatTime(row.nextReset):'未知'}</span></Tooltip>
        <span>更新：{row.checkedAt?formatTime(row.checkedAt):'未知'}{row.stale?` · ${row.stale} 个旧值`:''}</span>
      </div>
    </article>)}</div>
  </section>;
}
