import {useEffect,useRef,useState} from 'react';
import {Alert, Button, Card, Dropdown, Empty, Input, message, Modal, Progress, Select, Space, Statistic, Switch, Table, Tag, Tooltip, Typography} from 'antd';
import {ClockCircleOutlined, MoreOutlined, SearchOutlined, TeamOutlined, CheckCircleOutlined, PauseCircleOutlined, ThunderboltOutlined} from '@ant-design/icons';
import {api,errorText} from './api';
import {Account,accountStatus,formatTime,quotaColor,remaining,StatusBadge,windowName} from './accounts';
import GlobalStatus from './GlobalStatus';
import {formatWait,effectiveCooldown} from './cooldown';

export default function AccountsPanel({accounts,onEdit,reload,updatedAt}:{accounts:Account[],onEdit:(account:Account)=>void,reload:()=>Promise<void>,updatedAt:number|null}){
  const [now,setNow]=useState(()=>Date.now()/1000);
  useEffect(()=>{const timer=setInterval(()=>setNow(Date.now()/1000),1000);return()=>clearInterval(timer)},[]);
  const [query,setQuery]=useState(''),[plan,setPlan]=useState<string>(),[status,setStatus]=useState<string>();
  const [pending,setPending]=useState<string[]>([]);
  const running=useRef(new Set<string>());
  const [msg,context]=message.useMessage(),[modal,modalContext]=Modal.useModal();
  const filtered=accounts.filter(a=>(!plan||a.plan===plan)&&(!status||accountStatus(a).key===status)&&`${a.label} ${a.models.join(' ')}`.toLowerCase().includes(query.toLowerCase().trim()));
  const act=async(account:Account,action:'refresh'|'resume'|'toggle',enabled?:boolean)=>{
    if(running.current.has(account.id))return;
    running.current.add(account.id);setPending([...running.current]);
    try{
      const result:Account=await api(`/accounts/${account.id}${action==='toggle'?'':`/${action}`}`,action==='toggle'?'PATCH':'POST',action==='toggle'?{enabled}:undefined);
      if(action==='refresh'&&result.quota_error)msg.warning(result.quota_error);
      else msg.success(action==='refresh'?'额度已更新':action==='resume'?'已恢复，下一次请求将重新尝试':enabled?'账号已启用':'账号已停用');
      await reload();
    }catch(error){msg.error(errorText(error))}finally{running.current.delete(account.id);setPending([...running.current])}
  };
  const details=(account:Account)=><div className="account-details">
    {account.cooldown_code&&<Alert type="warning" showIcon message={`上游状态：${account.cooldown_code}`}/>}
    {account.model_blocks?.map(b=><Alert key={b.model} type="warning" showIcon message={`${b.model}：${b.code} · ${b.retry_at===null?'需处理后手动恢复':b.retry_at>now?`剩余 ${formatWait(b.retry_at,now)}`:'等待单请求试探'}`}/>)}
    {account.quota_error&&<Alert type="warning" showIcon message={`额度可能已过期：${account.quota_error}`}/>}
    {!Object.keys(account.usage).length&&<Typography.Text type="secondary">尚无官方额度数据。{account.has_ak_sk?'点击「刷新额度」重新查询。':'编辑账号并配置 AK/SK 可启用额度查询。'}</Typography.Text>}
    <div className="detail-meta"><span>更新时间：{account.quota_checked_at?formatTime(account.quota_checked_at):'尚未查询'}</span><span>额度主体：{accounts.find(a=>a.id===account.quota_group)?.label||'未找到'}</span>
      <span>查询凭据：{account.has_ak_sk?'已配置':'未配置'}</span>{account.cooldown_kind&&<span>恢复时间：{formatTime(account.cooldown_until)}</span>}</div>
  </div>;
  const metrics=[{label:'全部账号',value:accounts.length,icon:<TeamOutlined/>},{label:'活跃账号',value:accounts.filter(a=>accountStatus(a).key==='active').length,icon:<CheckCircleOutlined/>,color:'#158f98'},{label:'冷却账号',value:accounts.filter(a=>accountStatus(a).key==='cooling').length,icon:<PauseCircleOutlined/>,color:'#d76542'},{label:'当前并发',value:accounts.reduce((n,a)=>n+(a.inflight||0),0),icon:<ThunderboltOutlined/>}];
  return <div className="page-stack">{context}{modalContext}
    <div className="metric-band">{metrics.map(metric=><Statistic key={metric.label} title={<Space>{metric.icon}{metric.label}</Space>} value={metric.value} valueStyle={{color:metric.color}}/>)}</div>
    <GlobalStatus accounts={accounts} now={now}/>
    <Card className="accounts-card" styles={{body:{padding:0}}}>
      <div className="table-toolbar"><Input prefix={<SearchOutlined/>} aria-label="搜索账号或模型" placeholder="搜索账号或模型" value={query} onChange={e=>setQuery(e.target.value)} allowClear className="account-search"/>
        <Select aria-label="套餐筛选" placeholder="全部套餐" allowClear value={plan} onChange={setPlan} options={[{value:'agent',label:'Agent Plan'},{value:'coding',label:'Coding Plan'}]}/>
        <Select aria-label="状态筛选" placeholder="全部状态" allowClear value={status} onChange={setStatus} options={[{value:'active',label:'活跃'},{value:'cooling',label:'冷却 / 限流'},{value:'error',label:'失效 / 需处理'},{value:'disabled',label:'已停用'}]}/>
        <span className="table-count">共 {filtered.length} 个账号</span></div>
      <Table<Account> rowKey="id" dataSource={filtered} scroll={{x:940}} pagination={filtered.length>10?{pageSize:10}:false}
        locale={{emptyText:<Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description={accounts.length?'没有匹配的账号':'添加账号后开始管理额度'}/>}}
        expandable={{expandedRowRender:details,columnWidth:36}} columns={[
          {title:'账号',width:210,render:(_,a)=><div className="account-name"><strong>{a.label}</strong><span><Tag bordered={false} color={a.plan==='agent'?'cyan':'blue'}>{a.plan==='agent'?'Agent':'Coding'}</Tag><code>{a.api_key_mask}</code></span></div>},
          {title:'支持模型',width:180,render:(_,a)=><div className="model-list">{a.models.map(model=><span key={model}>{model}</span>)}</div>},
          {title:'可用额度',width:340,render:(_,a)=>Object.keys(a.usage).length?<div className="quota-rows">{Object.entries(a.usage).map(([name,usage])=>{
            const value=remaining(usage);
            const used=a.plan==='coding'?`${usage.used.toLocaleString('zh-CN',{maximumFractionDigits:4})}% 已用`:`${usage.used.toLocaleString('zh-CN',{maximumFractionDigits:4})} / ${usage.quota.toLocaleString()} AFP`;
            return <div className="quota-row" key={name}>
              <Progress type="circle" size={40} strokeWidth={8} aria-label={`${a.label} ${windowName(name)}剩余额度`} percent={value??0} format={()=>value===null?'未知':`${value===0||value===100?value:value.toFixed(3)}%`} strokeColor={value===null?'#91a0a6':quotaColor(value)}/>
              <div className="quota-row-body"><div className="quota-row-heading"><span>{windowName(name)}</span></div>
              <div className="quota-row-meta"><Tooltip title={used}><span>{used}</span></Tooltip><Tooltip title={formatTime(usage.reset_time)}><span>{usage.reset_time&&usage.reset_time>0?`${new Date(usage.reset_time*1000).toLocaleString('zh-CN',{month:'numeric',day:'numeric',hour:'2-digit',minute:'2-digit',second:'2-digit',hour12:false})} 重置`:'重置时间未知'}</span></Tooltip></div></div>
            </div>;
          })}{a.quota_error&&<Typography.Text type="warning">查询失败，显示上次额度</Typography.Text>}</div>:<Typography.Text type="secondary">未知{!a.has_ak_sk?' · 未配置 AK/SK':''}</Typography.Text>},
          {title:'状态',width:115,render:(_,a)=><div className="account-state"><StatusBadge account={a}/>{accountStatus(a,now).key==='cooling'&&<small>{effectiveCooldown(a,now)?.until?`剩余 ${formatWait(effectiveCooldown(a,now)!.until!,now)}`:'恢复时间未知'}</small>}</div>},
          {title:'操作',fixed:'right',width:165,render:(_,a)=><Space size={8}><Tooltip title={a.enabled?'停用账号':'启用账号'}><Switch size="small" aria-label={`${a.label} 启停`} checked={!!a.enabled} loading={pending.includes(a.id)} onChange={enabled=>act(a,'toggle',enabled)}/></Tooltip>
            <Button size="small" onClick={()=>onEdit(a)} disabled={pending.includes(a.id)}>编辑</Button>
            <Dropdown trigger={['click']} menu={{items:[{key:'refresh',label:'刷新额度'},{key:'resume',label:'手动恢复',disabled:!a.cooldown_kind&&!a.auth_failed&&!a.model_blocks?.length}],onClick:({key})=>key==='refresh'?void act(a,'refresh'):modal.confirm({title:'恢复此额度主体？',content:'将清除同一额度主体的冷却、模型隔离和鉴权失败状态；下一次请求会重新尝试上游。',okText:'恢复',cancelText:'取消',onOk:()=>act(a,'resume')})}}><Button size="small" aria-label={`${a.label} 更多操作`} icon={<MoreOutlined/>} disabled={pending.includes(a.id)}/></Dropdown></Space>},
        ]}/>
      <div className="table-footer"><ClockCircleOutlined/> 每 15 秒自动更新 <span>上次更新：{updatedAt?new Date(updatedAt).toLocaleTimeString('zh-CN',{hour12:false}):'—'}</span></div>
    </Card>
    <Typography.Text className="page-note" type="secondary">各行显示对应限制窗口的剩余额度；展开账号可查看查询状态和共享额度主体。</Typography.Text>
  </div>;
}
