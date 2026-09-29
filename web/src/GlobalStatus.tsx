import {Badge, Card, Space, Tag, Typography} from 'antd';
import {Account, accountStatus} from './accounts';
import {poolCooldown,formatWait} from './cooldown';

export default function GlobalStatus({accounts,now}:{accounts:Account[],now:number}){
  const active=accounts.filter(a=>accountStatus(a,now).key==='active');
  const enabled=accounts.filter(a=>!!a.enabled);
  const models=new Set(enabled.flatMap(a=>a.models));
  const available=new Set(active.flatMap(a=>a.models));
  const fullyAvailable=enabled.length>0&&active.length===enabled.length;
  const label=!accounts.length?'尚未配置':!enabled.length?'全部停用':!active.length?'暂无可用账号':fullyAvailable?'运行正常':'部分账号不可用';
  const color=!enabled.length?'#91a0a6':!active.length?'#c94343':fullyAvailable?'#16a085':'#c28a26';
  const quotaGroups=new Set(accounts.map(a=>a.quota_group));
  const queriedGroups=new Set(accounts.filter(a=>Object.keys(a.usage).length>0).map(a=>a.quota_group));
  const counts={active:0,cooling:0,error:0,disabled:0};
  accounts.forEach(a=>counts[accountStatus(a,now).key as keyof typeof counts]++);
  const cooldown=poolCooldown(accounts,now);
  return <Card className="global-status" title="全局状态" extra={<Badge color={color} text={label}/>}>
    <div className="global-summary"><Space wrap size={[12,8]}>
      <span>可用模型 <b>{available.size} / {models.size}</b></span>
      <span>已获取额度 <b>{queriedGroups.size} / {quotaGroups.size}</b> 个额度主体</span>
      {accounts.some(a=>a.quota_error)&&<Tag color="warning">{accounts.filter(a=>a.quota_error).length} 个账号额度查询异常</Tag>}
    </Space><Typography.Text type="secondary">基于当前账号配置与最近状态</Typography.Text></div>
    <div className="global-pool"><Space wrap size={[20,12]}>
      <span>共 {accounts.length} 个账号</span><Badge color="#16a085" text={`活跃 ${counts.active}`}/><Badge color="#d76542" text={`冷却 / 限流 ${counts.cooling}`}/><Badge color="#c94343" text={`失效 ${counts.error}`}/><Badge color="#91a0a6" text={`停用 ${counts.disabled}`}/>
    </Space>{cooldown.count>0&&<div className="pool-cooldown"><Typography.Text type="warning">{cooldown.until===null?'冷却恢复时间未知':`${cooldown.exact?'最短剩余冷却':'最早已知剩余冷却'}：${formatWait(cooldown.until,now)}`}</Typography.Text>
      {!cooldown.exact&&cooldown.until!==null&&<Typography.Text type="secondary">部分账号恢复时间未知，此时间不是全池精确最短值。</Typography.Text>}
      <Typography.Text type="secondary">{active.length?'其他活跃账号仍可处理请求。':'恢复后由后续请求或额度刷新确认可用。'}</Typography.Text>
    </div>}</div>
  </Card>;
}
