import {useEffect, useState} from 'react';
import {Alert, Card, Progress, Select, Space, Statistic, Table, Typography} from 'antd';

type Account = {id:string,label:string,plan:string};
type Daily = {day:string,outcome:string,requests:number,input_tokens:number,output_tokens:number,latency_ms:number,equivalent_cny:number,unpriced_input_tokens:number,unpriced_output_tokens:number};
type Quota = {quota_group:string,plan:string,window:string,quota:number,used:number,reset_time:number|null,observed_at:number|null,quota_error?:string|null};
type Data = {daily:Daily[],quota_current:Quota[],quota_history:Quota[]};
type Api = (path:string)=>Promise<Data>;

const fmt = (value:number|null) => value ? new Date(value*1000).toLocaleString('zh-CN') : '—';
const money = (value:number) => `¥${value.toFixed(4)}`;
const progress = (row:Quota) => {const used=row.quota>0?Math.min(100,Math.max(0,row.used/row.quota*100)):0;return <Progress percent={used} size="small" strokeColor={used>=85?'#cf3c3c':used>=60?'#d99820':'#2f9e69'} format={()=>`${(100-used).toFixed(1)}% 剩余`}/>};
const outcomeNames:Record<string,string> = {
  success:'成功', quota:'套餐额度耗尽', rate:'RPM/TPM 限流', auth:'鉴权失败',
  rejected:'上游拒绝', transport_error:'传输失败', response_error:'响应失败',
  stream_error:'流中失败', client_disconnected:'客户端断开',
};

export default function Statistics({accounts,api}:{accounts:Account[],api:Api}) {
  const [accountId,setAccountId] = useState<string|undefined>();
  const [days,setDays] = useState(30);
  const [data,setData] = useState<Data>({daily:[],quota_current:[],quota_history:[]});
  const [error,setError] = useState('');
  useEffect(() => {
    let alive = true;
    const load = () => api(`/statistics?days=${days}${accountId?`&account_id=${encodeURIComponent(accountId)}`:''}`)
      .then(value => {if(alive){setData(value);setError('')}})
      .catch(reason => {if(alive)setError(String(reason))});
    load();
    const timer = setInterval(load, 15000);
    return () => {alive=false;clearInterval(timer)};
  },[accountId,days,api]);

  const totals = data.daily.reduce((sum,row) => ({
    requests:sum.requests+row.requests,success:sum.success+(row.outcome==='success'?row.requests:0),
    input:sum.input+row.input_tokens,output:sum.output+row.output_tokens,
    equivalent:sum.equivalent+row.equivalent_cny,unpriced:sum.unpriced+row.unpriced_input_tokens+row.unpriced_output_tokens,
  }),{requests:0,success:0,input:0,output:0,equivalent:0,unpriced:0});
  const current = new Map<string,Quota&{groups:number}>();
  for(const item of data.quota_current){
    const key = `${item.plan}/${item.window}`;
    const prior = current.get(key);
    if(prior){prior.quota+=item.quota;prior.used+=item.used;prior.groups++;prior.reset_time=null;prior.observed_at=Math.min(prior.observed_at||0,item.observed_at||0);prior.quota_error=prior.quota_error||item.quota_error}
    else current.set(key,{...item,groups:1});
  }
  const quotaRows = [...current.values()].map((row,index) => ({...row,key:index}));

  return <Space direction="vertical" size={18} style={{width:'100%'}}>
    <Alert type="info" showIcon message="统计范围" description="请求数表示网关对上游的尝试次数；发生账号切换时，一次下游请求可能计入多个账号。Token 数仅累计上游响应提供的 usage。官方额度仅来自成功的管理接口查询，不用请求数估算剩余额度；全局合计也不代表任意模型都能使用这些额度。"/>
    <Space wrap>
      <Select style={{width:230}} value={accountId||'all'} onChange={value=>setAccountId(value==='all'?undefined:value)} options={[{value:'all',label:'全部账号'},...accounts.map(a=>({value:a.id,label:`${a.label} · ${a.plan==='agent'?'Agent':'Coding'}`}))]}/>
      <Select style={{width:130}} value={days} onChange={setDays} options={[7,30,90].map(value=>({value,label:`近 ${value} 天`}))}/>
    </Space>
    {error && <Alert type="error" message={error}/>}
    <div className="stats stats-four"><Card><Statistic title="上游尝试" value={totals.requests}/></Card><Card><Statistic title="成功" value={totals.success}/></Card><Card><Statistic title="输入 Token（已报告）" value={totals.input}/></Card><Card><Statistic title="输出 Token（已报告）" value={totals.output}/></Card></div>
    <div className="stats"><Card><Statistic title="等效价格（人民币）" value={money(totals.equivalent)}/></Card><Card><Statistic title="未定价 Token" value={totals.unpriced}/></Card></div>
    <Card title="当前官方额度" extra={<Typography.Text type="secondary">共享额度主体已去重；未知额度不计入</Typography.Text>}>
      <Table size="small" pagination={false} scroll={{x:800}} dataSource={quotaRows} locale={{emptyText:'尚无官方额度数据'}} columns={[
        {title:'套餐',dataIndex:'plan',render:(value:string)=>value==='agent'?'Agent Plan':'Coding Plan'},
        {title:'窗口',dataIndex:'window'}, {title:'额度主体',dataIndex:'groups'},
        {title:'已用 / 总量',render:(_:unknown,row:Quota)=>row.plan==='coding'?`${(row.used/(row as Quota&{groups:number}).groups).toFixed(1)}% 已用（平均）`:`${row.used} / ${row.quota}`},
        {title:'剩余比例',render:(_:unknown,row:Quota)=>progress(row)},
        {title:'重置时间',dataIndex:'reset_time',render:fmt},
        {title:'更新时间',dataIndex:'observed_at',render:fmt},
        {title:'查询状态',dataIndex:'quota_error',render:(value:string|null)=>value?<Typography.Text type="warning">旧值 · {value}</Typography.Text>:'正常'},
      ]}/>
    </Card>
    <Card title="每日请求统计"><Table size="small" rowKey={row=>`${row.day}/${row.outcome}`} dataSource={data.daily} pagination={{pageSize:12}} locale={{emptyText:'尚无网关请求记录'}} columns={[
      {title:'日期（UTC）',dataIndex:'day'}, {title:'结果',dataIndex:'outcome',render:(value:string)=>outcomeNames[value]||value},
      {title:'次数',dataIndex:'requests'}, {title:'输入 Token',dataIndex:'input_tokens'},
      {title:'输出 Token',dataIndex:'output_tokens'},
      {title:'等效价格',dataIndex:'equivalent_cny',render:money},
      {title:'未定价 Token',render:(_:unknown,row:Daily)=>row.unpriced_input_tokens+row.unpriced_output_tokens},
      {title:'平均耗时',render:(_:unknown,row:Daily)=>`${Math.round(row.latency_ms/row.requests)} ms`},
    ]}/></Card>
    <Card title="额度查询历史"><Table size="small" rowKey={row=>`${row.quota_group}/${row.window}/${row.observed_at}`} dataSource={data.quota_history} pagination={{pageSize:12}} locale={{emptyText:'尚无额度查询记录；Agent Plan 需先配置 AK/SK'}} scroll={{x:900}} columns={[
      {title:'查询时间',dataIndex:'observed_at',render:fmt},
      {title:'额度主体',dataIndex:'quota_group',render:(value:string)=>accounts.find(account=>account.id===value)?.label||<code>{value.slice(0,8)}</code>},
      {title:'套餐',dataIndex:'plan',render:(value:string)=>value==='agent'?'Agent Plan':'Coding Plan'},
      {title:'窗口',dataIndex:'window'},
      {title:'额度',render:(_:unknown,row:Quota)=>row.plan==='coding'?`${row.used.toFixed(1)}% 已用`:progress(row)},
      {title:'重置时间',dataIndex:'reset_time',render:fmt},
    ]}/></Card>
  </Space>;
}
